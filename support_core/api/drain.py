"""The caller that makes ``lock_wait_seconds=0`` safe. Implements the customer half of phase 2
review finding R7, against DESIGN.md sections 7.1 and 4.1.

    A burst on one conversation stalls a connection per waiter. Measured with five OS
    processes: one drained all five messages while the other four blocked on the advisory lock
    for the whole turn, each holding a connection... Add a queue-and-return mode
    (``lock_wait_seconds=0`` plus a poller calling ``drain``) and make it the default for
    channel webhooks. - reviews/phase-2.md, finding R7

Both halves of the mechanism already existed: :func:`~support_core.engine.locks.conversation_lock`
returns without waiting, :meth:`~support_core.engine.executor.Executor.on_inbound` then answers
``queued=True`` with the message durably ``pending``, and
:meth:`~support_core.engine.executor.Executor.drain` is the entry point for whoever picks it up.
What was missing is somebody to pick it up. This is that somebody, and it is deliberately small:

* it holds **conversation ids**, never messages. Everything about the message is already in the
  database; a queue that held work would be a second, lossy source of truth.
* it is **idempotent per conversation**. Ten messages arriving on one conversation while its
  lock is held are one thing to do, not ten, because ``drain`` processes the whole pending queue
  in order.
* **giving up is safe, but it is not free.** The message stays ``pending`` and in order; the
  next inbound message, or phase 7's scheduled ``recover_stalled``, drains it. What is not safe
  is retrying for ever, which is why the attempt is bounded by a budget - and what is not
  acceptable is giving up *silently on the first try*, which is what left 42 messages
  permanently pending in security review finding S2. A conversation the worker could not drain,
  because somebody held the lock, because this process was at its turn bound, or because the
  drain raised, gets :attr:`DrainQueue.rounds` budgets before the worker stops coming back.

The scheduler that calls ``recover_stalled`` and ``sweep_timeouts`` is still phase 7's. This is
only the caller an HTTP handler needs so that it never holds a connection open waiting for a
lock.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from support_core.engine.executor import Executor

DrainDoneHandler = Callable[[uuid.UUID], Awaitable[None]]
"""Called after a drain that held the lock, so whoever is watching the conversation can be told
what it looks like now. A web chat client learns from it that the turn the drain worker ran left
the conversation waiting for a confirmation; an email adapter has nothing to do with it."""

DrainErrorHandler = Callable[[uuid.UUID, Exception], None]
"""Told about a drain that raised. The default swallows it: a failing conversation must not take
the worker down, and the run it failed on is durable and recoverable."""


def _ignore(
    conversation_id: uuid.UUID, error: Exception
) -> None:  # pragma: no cover - deliberately silent
    return None


@dataclass(slots=True)
class DrainStats:
    """What the worker has done, for tests and for phase 7's metrics."""

    submitted: int = 0
    drained: int = 0
    retries: int = 0
    gave_up: int = 0
    failed: int = 0
    coalesced: int = 0
    """Submissions that joined a conversation already queued or in flight."""

    requeued: int = 0
    """Conversations put back because a message arrived while they were being drained."""

    abandoned: int = 0
    """Conversations the worker stopped coming back to, having used every round.

    Not the same as :attr:`gave_up`, which counts one round ending. A non-zero
    ``abandoned`` means messages are sitting ``pending`` that only phase 7's scheduled
    ``recover_stalled`` will now pick up, and is the number worth an alert."""


@dataclass(slots=True)
class DrainQueue:
    """Drains conversations whose lock somebody else held when their message arrived."""

    executor: Executor
    workers: int = 4
    first_delay: float = 0.02
    max_delay: float = 1.0
    budget_seconds: float = 60.0
    rounds: int = 3
    """How many budgets a conversation gets before the worker stops coming back to it.

    Security review finding S2 left 42 messages permanently ``pending``, and the worker's part in
    that was that a conversation whose budget ran out - or whose drain raised - was simply
    forgotten. A round is a whole budget of backing-off attempts, so three rounds is minutes of
    trying, which covers a flood of turns finishing and a database that was briefly unreachable.
    Past that it is not a transient condition and the durable answer is phase 7's scheduled
    ``recover_stalled``, which this must not become a private imitation of."""

    errors_per_round: int = 5
    """Failures in one round before the round is abandoned early.

    A busy lock is not a failure and does not count here; this is for a drain that *raised*.
    Retrying costs a database round trip, so a conversation the engine genuinely cannot get
    through should not spend a whole budget failing."""

    on_error: DrainErrorHandler = _ignore
    on_drained: DrainDoneHandler | None = None

    _queue: "asyncio.Queue[uuid.UUID]" = field(init=False, repr=False)
    _known: set[uuid.UUID] = field(init=False, default_factory=set, repr=False)
    _again: set[uuid.UUID] = field(init=False, default_factory=set, repr=False)
    _rounds: dict[uuid.UUID, int] = field(init=False, default_factory=dict, repr=False)
    _tasks: list[asyncio.Task[None]] = field(init=False, default_factory=list, repr=False)
    _idle: asyncio.Event = field(init=False, repr=False)
    _in_flight: int = field(init=False, default=0, repr=False)
    stats: DrainStats = field(init=False, default_factory=DrainStats)

    def __post_init__(self) -> None:
        self._queue = asyncio.Queue()
        self._idle = asyncio.Event()
        self._idle.set()

    async def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._work(), name=f"support-drain-{index}")
            for index in range(self.workers)
        ]

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def submit(self, conversation_id: uuid.UUID) -> None:
        """Ask for this conversation to be drained. Cheap, synchronous and idempotent."""
        self.stats.submitted += 1
        if conversation_id in self._known:
            # A message that arrives while this conversation is being drained is usually picked
            # up by the drain already running - it claims until the pending queue is empty. Not
            # always, though: a row that commits after the last claim found nothing and before
            # the lock is released would be left behind. Remembering that somebody asked again
            # costs one set entry and closes that window (the same window phase 2 closed for
            # waiting callers by making them wait).
            self._again.add(conversation_id)
            self.stats.coalesced += 1
            return
        self._enqueue(conversation_id)

    def _enqueue(self, conversation_id: uuid.UUID) -> None:
        self._known.add(conversation_id)
        self._idle.clear()
        self._queue.put_nowait(conversation_id)

    async def wait_until_idle(self, timeout: float = 30.0) -> bool:
        """Block until nothing is queued or in flight. For tests and for shutdown."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def _work(self) -> None:
        while True:
            conversation_id = await self._queue.get()
            self._in_flight += 1
            taken = True  # so `finally` has an answer even if the body never reached one
            try:
                taken = await self._drain_until_taken(conversation_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - `_drain_until_taken` catches its own
                # A conversation the engine could not get through must not stop the worker: the
                # run is durable, and the next message or phase 7's recovery sweep re-enters it.
                self.stats.failed += 1
                self.on_error(conversation_id, exc)
                taken = True  # nothing this loop can usefully retry
            finally:
                self._known.discard(conversation_id)
                self._in_flight -= 1
                self._queue.task_done()
                if not taken and self._another_round(conversation_id):
                    self._again.add(conversation_id)
                if conversation_id in self._again:
                    self._again.discard(conversation_id)
                    self.stats.requeued += 1
                    self._enqueue(conversation_id)
                else:
                    self._rounds.pop(conversation_id, None)
                if self._queue.empty() and self._in_flight == 0 and not self._known:
                    self._idle.set()

    def _another_round(self, conversation_id: uuid.UUID) -> bool:
        """Whether a conversation the worker could not drain gets another budget.

        The counter is per conversation and is cleared the moment a drain succeeds, so a busy
        conversation that is drained on every third attempt never runs out of rounds; only one
        that is never drained does.
        """
        used = self._rounds.get(conversation_id, 0) + 1
        self._rounds[conversation_id] = used
        if used < self.rounds:
            return True
        self.stats.abandoned += 1
        return False

    async def _drain_until_taken(self, conversation_id: uuid.UUID) -> bool:
        """Call ``drain`` until it is the one holding the lock, or the budget runs out.

        Returns whether the conversation was drained. ``False`` means the caller should come
        back to it: the customer's message is still ``pending``, and the reason it is - somebody
        else holding the lock, this process being at its turn bound, or a drain that raised - is
        the kind of thing that stops being true.

        A drain that **raises** is retried here rather than dropped (security review finding
        S2). The failure that matters is a pool timeout, which is transient by definition and
        which used to end with the message pending and nobody coming back for it; the errors that
        are not transient are bounded by :attr:`errors_per_round` and then by :attr:`rounds`.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.budget_seconds
        delay = self.first_delay
        errors = 0
        while True:
            try:
                outcome = await self.executor.drain(conversation_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.failed += 1
                self.on_error(conversation_id, exc)
                errors += 1
                if errors >= self.errors_per_round:
                    return False
            else:
                if not outcome.queued:
                    self.stats.drained += 1
                    self._rounds.pop(conversation_id, None)
                    if self.on_drained is not None:
                        await self.on_drained(conversation_id)
                    return True
            if loop.time() >= deadline:
                # Somebody else has held the lock for the whole budget, or this process has been
                # at its turn bound for it. They drain the queue in order, so the message is not
                # lost; it is only not ours to run yet.
                self.stats.gave_up += 1
                return False
            self.stats.retries += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_delay)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DrainQueue(workers={self.workers}, queued={self._queue.qsize()}, {self.stats})"
