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
* **giving up is safe.** The message stays ``pending`` and in order; the next inbound message,
  or phase 7's scheduled ``recover_stalled``, drains it. What is not safe is retrying for ever,
  which is why the attempt is bounded by a budget.

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


@dataclass(slots=True)
class DrainQueue:
    """Drains conversations whose lock somebody else held when their message arrived."""

    executor: Executor
    workers: int = 4
    first_delay: float = 0.02
    max_delay: float = 1.0
    budget_seconds: float = 60.0
    on_error: DrainErrorHandler = _ignore
    on_drained: DrainDoneHandler | None = None

    _queue: "asyncio.Queue[uuid.UUID]" = field(init=False, repr=False)
    _known: set[uuid.UUID] = field(init=False, default_factory=set, repr=False)
    _again: set[uuid.UUID] = field(init=False, default_factory=set, repr=False)
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
            try:
                await self._drain_until_taken(conversation_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A conversation the engine could not get through must not stop the worker: the
                # run is durable, and the next message or phase 7's recovery sweep re-enters it.
                self.stats.failed += 1
                self.on_error(conversation_id, exc)
            finally:
                self._known.discard(conversation_id)
                self._in_flight -= 1
                self._queue.task_done()
                if conversation_id in self._again:
                    self._again.discard(conversation_id)
                    self.submit(conversation_id)
                if self._queue.empty() and self._in_flight == 0 and not self._known:
                    self._idle.set()

    async def _drain_until_taken(self, conversation_id: uuid.UUID) -> None:
        """Call ``drain`` until it is the one holding the lock, or the budget runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.budget_seconds
        delay = self.first_delay
        while True:
            outcome = await self.executor.drain(conversation_id)
            if not outcome.queued:
                self.stats.drained += 1
                if self.on_drained is not None:
                    await self.on_drained(conversation_id)
                return
            if loop.time() >= deadline:
                # Somebody else has held the lock for the whole budget. They drain the queue in
                # order, so the message is not lost; it is only not ours to run.
                self.stats.gave_up += 1
                return
            self.stats.retries += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.max_delay)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DrainQueue(workers={self.workers}, queued={self._queue.qsize()}, {self.stats})"
