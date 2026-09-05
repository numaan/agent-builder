"""The turn loop. Implements DESIGN.md section 7.1, with 7.2 (suspension and resumption) and
the parts of 7.3 (failure handling) that do not need a tool runtime or a model.

Section 7.1's pseudocode, line by line, and where each line is::

    lock conversation                     locks.conversation_lock, taken in on_inbound/_resume
    load Run                              _load
    guardrails.inbound(message)           phase 7; deliberately not stubbed
    append message to history             repositories.enqueue_inbound, before the lock
    if waiting_customer: interrupt_check  hooks.interrupt_check (defaults to "continue")
    elif idle: push root frame            _start_root_frame
    loop until suspend or end or limits   _loop
        result = node.run / node.resume   runners
        apply patch; trace step; checkpt  _checkpoint, one transaction
        guardrails.outbound               phase 7
        send outbound                     hooks.send, after the commit
        advance                           _advance
    release lock

The single invariant everything else rests on: **the executor holds no authoritative state.**
Frames, status, sequence numbers and the per-turn counter live in the ``run`` row and are
written with the trace step in one transaction. The in-memory :class:`_Turn` is a cache of the
last committed checkpoint, so a process that dies at any point - before a node, after a node,
inside the checkpoint transaction, or after it - resumes by re-reading the row.
"""

import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.engine.errors import (
    EngineError,
    IncompatiblePackError,
    NodeError,
)
from support_core.engine.hooks import EngineHooks, HandoffRequest
from support_core.engine.hooks import SummaryRequest as SummaryHookRequest
from support_core.engine.locks import conversation_lock
from support_core.engine.runners import (
    GateRunner,
    NodeRuntime,
    build_runner,
    resolve_edge,
)
from support_core.engine.types import (
    SUSPEND_STATUSES,
    Frame,
    GraphInvocation,
    NodeResult,
    OutboundMessage,
    ResumeEvent,
    TurnOutcome,
    step_id,
)
from support_core.graph.context import ConversationContext
from support_core.graph.manifest import Channel, SuspendStatus
from support_core.graph.nodes import GateNode, NodeBase
from support_core.graph.pack import Pack
from support_core.graph.schema import Graph
from support_core.llm.prompt import TranscriptMessage
from support_core.llm.service import LlmService
from support_core.llm.tool_loop import ModelToolRunner, UnavailableToolRunner
from support_core.storage import repositories as repo
from support_core.storage.models import Conversation, Run
from support_core.storage.repositories import RunUpdate, StepWrite
from support_core.storage.session import make_session_factory

ENTRY_INPUTS_KEY = "inputs"
"""Key inside ``conversation.context`` holding the entry graph's ``inputs``.

DESIGN.md section 6.4 lets a graph declare ``inputs`` and section 17 gives the conversation a
``context`` JSONB, but nothing says how the *entry* graph is invoked, because in the design's
own example the root graph takes none. A channel that knows something up front - a web chat
widget that opened on a charge, an email whose subject named an order - puts it here, and the
root frame is seeded from it under the same rule the validator enforces for every other graph
(``graph.input_not_in_state``): an input lands in the state field of the same name."""

TURN_EVENT_KEY = "turn_event"
"""Key in ``run.awaiting`` holding the event that drove the turn in flight, until the node it
belongs to consumes it. Without it, a process that died between claiming a customer message and
delivering it would leave the message claimed and the run with no idea what it was for."""

MAX_RECOVERY_ATTEMPTS = 3
"""Failed recovery passes before a stalled run is parked for a human (review finding R3).

Three because a transient cause - the database being restarted under the sweep, a channel that
is briefly down - deserves more than one try, and a permanent one deserves an operator rather
than an unbounded retry loop."""

RESUMABLE_BY_CUSTOMER = frozenset(["idle", "done", "waiting_customer"])
"""Statuses in which a customer message starts or continues a turn. A run waiting for a human,
a tool or a timer keeps its queue: the message is stored and stays ``pending`` until the thing
it is waiting for arrives, because resuming it early would drop that wait on the floor."""


@dataclass(slots=True)
class _Turn:
    """The last committed checkpoint, in memory. Never the source of truth."""

    run_id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    frames: list[Frame]
    seq: int
    next_frame_seq: int
    turn_nodes: int
    outcome: TurnOutcome
    pending_event: ResumeEvent | None = None
    """The event that started this turn, until the node it belongs to consumes it.

    It is written into ``run.awaiting`` by every checkpoint while it is still pending, so a
    process that dies mid-turn does not lose the customer message that drove it: the message
    row is already claimed, and only the run row can say what it was for."""

    @property
    def frame(self) -> Frame:
        return self.frames[-1]


@dataclass(slots=True)
class _RunRow:
    """A snapshot of the ``run`` row; the ORM object never leaves its session."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    frames: list[dict[str, Any]]
    checkpoint_seq: int
    next_frame_seq: int
    turn_nodes: int
    awaiting: dict[str, Any] | None
    pack_fingerprint: str | None
    recovery_attempts: int = 0


def _snapshot(run: Run) -> _RunRow:
    return _RunRow(
        id=run.id,
        conversation_id=run.conversation_id,
        status=run.status,
        frames=[dict(frame) for frame in run.frames],
        checkpoint_seq=run.checkpoint_seq,
        next_frame_seq=run.next_frame_seq,
        turn_nodes=run.turn_nodes,
        awaiting=dict(run.awaiting) if run.awaiting else None,
        pack_fingerprint=run.pack_fingerprint,
        recovery_attempts=run.recovery_attempts,
    )


class Executor:
    """Runs conversations of one pack against one database (DESIGN.md sections 4.1, 7.1).

    Stateless between calls apart from the pack and the runner cache, so any number of
    processes may share a database: the advisory lock, not the object, is what makes a
    conversation single-writer.
    """

    def __init__(
        self,
        pack: Pack,
        engine: AsyncEngine,
        *,
        hooks: EngineHooks | None = None,
        lock_wait_seconds: float = 30.0,
        llm: LlmService | None = None,
        tool_runner: ModelToolRunner | None = None,
    ) -> None:
        self.pack = pack
        self.engine = engine
        self.hooks = hooks or EngineHooks()
        self.lock_wait_seconds = lock_wait_seconds
        self.sessions = make_session_factory(engine)
        self._runners: dict[tuple[str, str], Any] = {}
        self.llm = llm
        """The LLM layer (DESIGN.md section 11). ``None`` is a legitimate configuration - the
        engine's own durability tests run without one - and an ``llm`` node then fails as a node
        error naming what is missing, rather than pretending."""

        self.tool_runner: ModelToolRunner = tool_runner or UnavailableToolRunner()
        """Phase 4's tool runtime. Whatever is injected here is wrapped by a
        :class:`~support_core.llm.tool_loop.ReadOnlyToolGateway` before any node can reach it
        (see :meth:`~support_core.engine.runners.NodeRuntime.tool_gateway`), so the risk policy
        of DESIGN.md section 8.2 is not something phase 4 can choose to apply."""

        self._tool_risk = {name: spec.risk for name, spec in pack.tools.tools.items()}

    # -- entry points --------------------------------------------------------------------

    async def start_conversation(
        self,
        *,
        channel: Channel = "web_chat",
        customer_ref: str | None = None,
        context: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        """Create a conversation and its run. Returns the conversation id.

        ``context`` is the :class:`~support_core.graph.context.ConversationContext` the pack's
        expressions read as ``ctx``; ``inputs`` seeds the entry graph's state (see
        :data:`ENTRY_INPUTS_KEY`).
        """
        stored = dict(context or {})
        if inputs:
            stored[ENTRY_INPUTS_KEY] = to_jsonable_python(inputs)
        async with self.sessions() as session, session.begin():
            conversation = await repo.create_conversation(
                session, channel=channel, customer_ref=customer_ref, context=stored
            )
            await repo.create_run(
                session,
                conversation_id=conversation.id,
                pack_version=self.pack.manifest.version,
                pack_fingerprint=self.pack.pin.fingerprint,
            )
            return conversation.id

    async def on_inbound(self, conversation_id: uuid.UUID, text: str) -> TurnOutcome:
        """Accept a customer message and run whatever turns it makes possible.

        The message row is written *before* the lock is attempted, always with
        ``status = pending``: that is what makes it durable regardless of which process ends up
        processing it, and what gives the pending queue its order (DESIGN.md section 17).
        """
        async with self.sessions() as session, session.begin():
            conversation = await repo.get_conversation(session, conversation_id)
            if conversation is None:
                msg = f"no conversation {conversation_id}"
                raise EngineError(msg)
            await repo.enqueue_inbound(session, conversation_id=conversation_id, text_=text)

        async with conversation_lock(
            self.engine, conversation_id, wait_seconds=self.lock_wait_seconds
        ) as acquired:
            if not acquired:
                return TurnOutcome(conversation_id=conversation_id, queued=True)
            return await self._drain(conversation_id)

    async def drain(self, conversation_id: uuid.UUID) -> TurnOutcome:
        """Process whatever the conversation has queued, under its lock.

        The same body ``on_inbound`` runs after storing its message. A poller, a recovery
        worker, or a caller that put a message back on the queue uses this to pick it up.
        """
        async with conversation_lock(
            self.engine, conversation_id, wait_seconds=self.lock_wait_seconds
        ) as acquired:
            if not acquired:
                return TurnOutcome(conversation_id=conversation_id, queued=True)
            return await self._drain(conversation_id)

    async def resume_human(
        self,
        conversation_id: uuid.UUID,
        *,
        text: str | None = None,
        patch: dict[str, Any] | None = None,
        close: bool = False,
    ) -> TurnOutcome:
        """The desk returning control, or closing (DESIGN.md section 7.2, "Desk API")."""
        if close:
            return await self._close(conversation_id)
        return await self._resume(
            conversation_id,
            ResumeEvent(kind="human", text=text, payload=patch or {}),
            expected="waiting_human",
            patch=patch,
        )

    async def resume_async_tool(
        self, conversation_id: uuid.UUID, *, payload: dict[str, Any] | None = None
    ) -> TurnOutcome:
        """A long-running tool calling back (DESIGN.md section 7.2)."""
        return await self._resume(
            conversation_id,
            ResumeEvent(kind="async_tool", payload=payload or {}),
            expected="waiting_async_tool",
        )

    async def resume_timer(self, conversation_id: uuid.UUID) -> TurnOutcome:
        """The scheduler firing a follow-up (DESIGN.md section 7.2)."""
        return await self._resume(
            conversation_id, ResumeEvent(kind="timer"), expected="waiting_timer"
        )

    async def recover_stalled(
        self, older_than: timedelta = timedelta(minutes=5), limit: int = 100
    ) -> list[TurnOutcome]:
        """Re-enter turns whose process died (DESIGN.md section 7.3, "engine crash mid-node").

        A crashed turn leaves the run ``running`` and its advisory lock released. The next
        message, resume or :meth:`drain` picks it up - but a conversation nobody touches again
        would sit there for ever, and its pending queue with it, because a run that is not
        suspended has no ``timeout_at`` for :meth:`sweep_timeouts` to find. A scheduler calls
        this; ``older_than`` keeps it away from turns that are merely slow.
        """
        cutoff = self.hooks.clock() - older_than
        async with self.sessions() as session, session.begin():
            stalled = [_snapshot(run) for run in await repo.stalled_runs(session, cutoff, limit)]
        outcomes: list[TurnOutcome] = []
        for run in stalled:
            async with conversation_lock(
                self.engine, run.conversation_id, wait_seconds=0
            ) as acquired:
                if not acquired:
                    continue  # somebody is working on it after all
                outcomes.append(await self._recover_one(run))
        return outcomes

    async def _recover_one(self, run: _RunRow) -> TurnOutcome:
        """One conversation's recovery, whatever happens to it (review finding R3).

        A sweep is a batch: a conversation core cannot get through - a node type this phase
        cannot execute, the ``EngineError`` phase 6's ``new_intent`` raises, a ``send`` hook
        that is down - must not stop the conversations behind it in the batch, and must not be
        retried by every sweep from now until someone notices. After
        :data:`MAX_RECOVERY_ATTEMPTS` failures the run is parked ``waiting_human`` with reason
        ``engine_error``, which is DESIGN.md section 7.3's answer to a failure the engine cannot
        route: a human looks at it.
        """
        try:
            outcome = await self._drain(run.conversation_id)
        except Exception as exc:
            # Deliberately every exception: what must not escape is precisely the failure
            # nothing else in the engine knows how to route.
            return await self._recovery_failed(run, exc)
        if run.recovery_attempts:
            await self._set(run.id, recovery_attempts=0)
        return outcome

    async def _recovery_failed(self, run: _RunRow, exc: Exception) -> TurnOutcome:
        outcome = TurnOutcome(conversation_id=run.conversation_id, run_id=run.id)
        outcome.status = run.status  # type: ignore[assignment]
        async with self.sessions() as session, session.begin():
            attempts = await repo.count_recovery_attempt(session, run.id)
        detail = f"{type(exc).__name__}: {exc}"
        if attempts < MAX_RECOVERY_ATTEMPTS:
            return outcome
        now = self.hooks.clock()
        await self._set(
            run.id,
            status="waiting_human",
            timeout_at=None,
            suspended_at=now,
            awaiting={"kind": "handoff", "reason": "engine_error", "detail": detail},
            updated_at=now,
        )
        outcome.status = "waiting_human"
        outcome.handoff_reason = "engine_error"
        frames = [Frame.model_validate(frame) for frame in run.frames]
        # A handoff sink that is itself down must not re-poison the batch it is being told
        # about; the run is already parked durably, which is the part that matters.
        with suppress(Exception):
            await self.hooks.handoff(
                HandoffRequest(
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                    reason="engine_error",
                    detail=f"recovery failed {attempts} times: {detail}",
                    frames=frames,
                )
            )
        return outcome

    async def sweep_timeouts(self, now: datetime | None = None) -> list[TurnOutcome]:
        """Apply expired per-status timeouts (DESIGN.md section 7.2).

        Called by a scheduler; there is no thread inside the engine. Each conversation is
        handled under its own lock, and a run whose timeout moved while we waited is skipped.
        """
        moment = now or self.hooks.clock()
        async with self.sessions() as session, session.begin():
            due = [_snapshot(run) for run in await repo.due_runs(session, moment)]
        outcomes: list[TurnOutcome] = []
        for run in due:
            async with conversation_lock(
                self.engine, run.conversation_id, wait_seconds=self.lock_wait_seconds
            ) as acquired:
                if not acquired:
                    continue
                outcomes.append(await self._apply_timeout(run.conversation_id, moment))
        return outcomes

    # -- turn management -----------------------------------------------------------------

    async def _drain(self, conversation_id: uuid.UUID) -> TurnOutcome:
        """Process the pending queue in order. Called with the conversation lock held."""
        outcome = TurnOutcome(conversation_id=conversation_id)
        conversation = await self._conversation(conversation_id)
        run = await self._run_for(conversation_id)
        outcome.run_id = run.id

        if run.status == "running":
            # A process died mid-turn: nothing else could hold the lock we now hold. Finish
            # that turn from its last checkpoint before touching the queue, so messages stay
            # in order (DESIGN.md section 7.3, "Engine crash mid-node").
            run = await self._continue_interrupted(conversation, run, outcome)

        while run.status in RESUMABLE_BY_CUSTOMER:
            turn = await self._claim_turn(conversation, run, outcome)
            if turn is None:
                break
            outcome.messages_processed += 1
            run = await self._turn(conversation, turn)
        await self._flush_outbound(conversation_id)
        await self._maybe_summarize(conversation_id)
        outcome.status = run.status  # type: ignore[assignment]
        return outcome

    async def _claim_turn(
        self, conversation: Conversation, run: _RunRow, outcome: TurnOutcome
    ) -> _Turn | None:
        """Claim the next customer message and start its turn, or ``None`` if the queue is empty.

        The claim and the run row that records what was claimed commit **together**
        (:func:`~support_core.storage.repositories.claim_and_begin_turn`). Everything that has
        to happen before the claim and must not happen inside its transaction - deciding whether
        this message resumes a suspended node or starts a fresh root frame, and the interrupt
        check of DESIGN.md section 6.6, which from phase 6 is a model call - happens here,
        against the message read without claiming it. A process that dies at any point in this
        method therefore leaves the message ``pending``; once the transaction commits, the run
        is ``running`` and carries the event, which every recovery path can see.
        """
        peeked = await self._peek(conversation.id)
        if peeked is None:
            return None
        message_id, body = peeked
        ctx = self._context(conversation)
        turn = self._turn_state(run, outcome)

        if run.status == "waiting_customer":
            decision = await self.hooks.interrupt_check(ctx, body, list(turn.frames))
            if decision.kind != "continue":
                msg = (
                    f"interrupt check returned {decision.kind!r}: pushing a new intent, "
                    "cancelling and the return-to-workflow prompt are DESIGN.md section 6.6 "
                    "and arrive in phase 6"
                )
                raise EngineError(msg)
            frame_seq, node_id = self._suspended_at(run, turn.frames)
            turn.pending_event = ResumeEvent(
                kind="customer_message",
                text=body,
                message_id=message_id,
                target_frame_seq=frame_seq,
                target_node_id=node_id,
            )
        else:
            # An idle or finished run: this message starts a new root frame rather than
            # resuming a node, so there is no event for a node to consume.
            turn.frames = []
            self._push(
                turn,
                GraphInvocation(
                    graph=self.pack.manifest.entry_graph,
                    kind="root",
                    inputs=dict(conversation.context or {}).get(ENTRY_INPUTS_KEY) or {},
                ),
            )

        turn.status = "running"
        turn.turn_nodes = 0
        claimed = await self._claim(turn, message_id)
        if not claimed:  # pragma: no cover - impossible while we hold the conversation lock
            return None
        await self.hooks.probe("after_claim", {"message_id": str(message_id)})
        return turn

    async def _turn(self, conversation: Conversation, turn: _Turn) -> _RunRow:
        """One customer message (DESIGN.md section 7.1, the body of ``on_inbound``)."""
        undelivered = await self._loop(turn, self._context(conversation))
        await self._requeue(turn, undelivered)
        return await self._run_for(conversation.id)

    async def _continue_interrupted(
        self, conversation: Conversation, run: _RunRow, outcome: TurnOutcome
    ) -> _RunRow:
        """Re-enter a turn whose process died. The current node runs again under its own id."""
        turn = self._turn_state(run, outcome)
        stored = (run.awaiting or {}).get(TURN_EVENT_KEY)
        turn.pending_event = ResumeEvent.model_validate(stored) if stored else None
        if not turn.frames:
            # A run marked in flight with nothing on the stack. Whatever put it there, the
            # message that drove it goes back on the queue rather than down with it.
            await self._requeue(turn, turn.pending_event)
            await self._set(run.id, status="idle", turn_nodes=0, awaiting=None)
            return await self._run_for(conversation.id)
        undelivered = await self._loop(turn, self._context(conversation))
        await self._requeue(turn, undelivered)
        return await self._run_for(conversation.id)

    async def _requeue(self, turn: _Turn, event: ResumeEvent | None) -> None:
        """Put back a customer message no node consumed (see :meth:`_loop`).

        The message going back on the queue and the run forgetting it are one transaction. Any
        other order leaves a window in which a crash would either lose the message or deliver
        it twice.
        """
        if event is None or event.message_id is None:
            return
        async with self.sessions() as session, session.begin():
            await repo.requeue(session, event.message_id)
            await repo.forget_turn_event(session, turn.run_id)

    async def _resume(
        self,
        conversation_id: uuid.UUID,
        event: ResumeEvent,
        *,
        expected: SuspendStatus,
        patch: dict[str, Any] | None = None,
    ) -> TurnOutcome:
        outcome = TurnOutcome(conversation_id=conversation_id)
        async with conversation_lock(
            self.engine, conversation_id, wait_seconds=self.lock_wait_seconds
        ) as acquired:
            if not acquired:
                outcome.queued = True
                return outcome
            conversation = await self._conversation(conversation_id)
            run = await self._run_for(conversation_id)
            outcome.run_id = run.id
            if run.status != expected:
                msg = f"run is {run.status!r}, not {expected!r}; nothing to resume"
                raise EngineError(msg)
            turn = self._turn_state(run, outcome)
            if patch:
                turn.frame.state.update(to_jsonable_python(patch))
            # A run parked by the engine itself (a limit, a node failure, a timeout) has no
            # node waiting on an event: re-run the node the human unblocked.
            awaiting = run.awaiting or {}
            if awaiting.get("kind") == "node":
                frame_seq, node_id = self._suspended_at(run, turn.frames)
                turn.pending_event = event.model_copy(
                    update={"target_frame_seq": frame_seq, "target_node_id": node_id}
                )
            else:
                turn.pending_event = None
            await self._begin_turn(turn)
            undelivered = await self._loop(turn, self._context(conversation))
            await self._requeue(turn, undelivered)
            await self._flush_outbound(conversation_id)
            await self._maybe_summarize(conversation_id)
            outcome.status = (await self._run_for(conversation_id)).status  # type: ignore[assignment]
        return outcome

    async def _close(self, conversation_id: uuid.UUID) -> TurnOutcome:
        async with conversation_lock(
            self.engine, conversation_id, wait_seconds=self.lock_wait_seconds
        ) as acquired:
            if not acquired:
                return TurnOutcome(conversation_id=conversation_id, queued=True)
            return await self._close_locked(conversation_id)

    async def _close_locked(self, conversation_id: uuid.UUID) -> TurnOutcome:
        """Close the conversation. The caller already holds the lock."""
        now = self.hooks.clock()
        run = await self._run_for(conversation_id)
        async with self.sessions() as session, session.begin():
            await repo.set_run_fields(
                session,
                run.id,
                status="done",
                awaiting=None,
                timeout_at=None,
                suspended_at=None,
                updated_at=now,
            )
            await repo.close_conversation(session, conversation_id, when=now)
        return TurnOutcome(conversation_id=conversation_id, run_id=run.id, status="done")

    async def _apply_timeout(self, conversation_id: uuid.UUID, now: datetime) -> TurnOutcome:
        conversation = await self._conversation(conversation_id)
        run = await self._run_for(conversation_id)
        outcome = TurnOutcome(conversation_id=conversation_id, run_id=run.id)
        outcome.status = run.status  # type: ignore[assignment]
        if run.status not in SUSPEND_STATUSES:
            return outcome
        rule = self.pack.manifest.timeouts.rule(
            run.status,  # type: ignore[arg-type]
            self._channel(conversation),
        )
        if rule.action == "close":
            return await self._close_locked(conversation_id)
        if rule.action == "handoff":
            await self._set(
                run.id,
                status="waiting_human",
                timeout_at=None,
                suspended_at=now,
                awaiting={"kind": "handoff", "reason": "timeout", "from": run.status},
                updated_at=now,
            )
            await self.hooks.handoff(
                HandoffRequest(
                    conversation_id=conversation_id,
                    run_id=run.id,
                    reason="timeout",
                    detail=f"timed out in {run.status}",
                    frames=[Frame.model_validate(frame) for frame in run.frames],
                )
            )
            outcome.status = "waiting_human"
            outcome.handoff_reason = "timeout"
            return outcome
        # action "none": clear the deadline so the sweep does not see it again.
        await self._set(run.id, timeout_at=None, updated_at=now)
        return outcome

    # -- the loop ------------------------------------------------------------------------

    async def _loop(self, turn: _Turn, ctx: ConversationContext) -> ResumeEvent | None:
        """Run nodes until the run suspends, ends, or hits a limit (DESIGN.md section 7.1).

        Returns the resume event if no node ever consumed it, which happens when the frame's
        gates fire on entry and the redirect suspends before the waiting node runs. The caller
        puts the message back on the queue rather than dropping it, so nothing a customer sent
        is lost by a gate firing.
        """
        check_gates = True
        while turn.status == "running":
            frame = turn.frame
            try:
                graph = self._graph(frame)
                state = self._state(graph, frame)
            except IncompatiblePackError as exc:
                await self._handoff(turn, frame.node_id, "pack_incompatible", str(exc))
                return turn.pending_event

            if check_gates:
                check_gates = False
                try:
                    gate_id = self._failed_gate(graph, frame, state, ctx)
                except NodeError as exc:
                    await self._handoff(turn, frame.node_id, exc.reason, str(exc))
                    return turn.pending_event
                if gate_id is not None:
                    await self._run_gate_recheck(turn, graph, frame, gate_id)
                    check_gates = True
                    continue

            limit = self.pack.manifest.limits.max_nodes_per_turn
            if turn.turn_nodes >= limit:
                await self._handoff(
                    turn,
                    frame.node_id,
                    "limit_exceeded",
                    f"max_nodes_per_turn ({limit}) reached",
                )
                return turn.pending_event

            node = graph.nodes[frame.node_id]
            attempt = frame.attempts.get(frame.node_id, 0)
            sid = step_id(turn.run_id, frame.frame_seq, frame.node_id, attempt)
            runtime = NodeRuntime(
                graph=graph,
                frame=frame,
                step_id=sid,
                run_id=turn.run_id,
                conversation_id=turn.conversation_id,
                hooks=self.hooks,
                environment=self.pack.environment,
                llm=self.llm,
                history=await self._history(turn.conversation_id),
                tool_runner=self.tool_runner,
                tool_risk=self._tool_risk,
                max_tool_calls=self.pack.manifest.limits.max_tool_calls_per_turn,
            )
            runner = self._runner(graph, frame.node_id, node)
            started = self.hooks.clock()
            await self.hooks.probe("before_node", {"step_id": sid, "node_id": frame.node_id})
            event = turn.pending_event
            # The event belongs to the node that suspended and to no other, and which node that
            # is comes from the event itself rather than from the stack (finding R2): a gate
            # firing on entry pushes a redirect whose first node has not been waiting for
            # anything, and handing it a resume event would call ``resume`` on a node that never
            # suspended - which after a crash is what "the stack top" would name.
            delivering = (
                event is not None
                and event.target_frame_seq == frame.frame_seq
                and event.target_node_id == frame.node_id
            )
            try:
                if delivering and event is not None:
                    result = await runner.resume(state, ctx, runtime, event)
                else:
                    result = await runner.run(state, ctx, runtime)
                self._check_outputs(turn, frame, result)
            except NodeError as exc:
                if delivering:
                    turn.pending_event = None
                if not await self._route_error(turn, graph, frame, node, sid, started, exc):
                    return turn.pending_event
                check_gates = False
                continue
            if delivering:
                turn.pending_event = None
            await self.hooks.probe("after_node", {"step_id": sid, "node_id": frame.node_id})
            check_gates = await self._advance(turn, ctx, graph, frame, node, result, sid, started)
        return turn.pending_event

    async def _advance(
        self,
        turn: _Turn,
        ctx: ConversationContext,
        graph: Graph,
        frame: Frame,
        node: NodeBase,
        result: NodeResult,
        sid: str,
        started: datetime,
    ) -> bool:
        """Apply one node's result to the stack and checkpoint it. Returns "stack changed"."""
        node_id = frame.node_id
        patch = to_jsonable_python(result.state_patch)
        frame.state.update(patch)
        edge: str | None
        stack_changed = False

        if result.suspend is not None:
            edge = None
            turn.status = result.suspend.status
        elif result.push_graph is not None:
            edge = "push"
            self._push(turn, result.push_graph)
            stack_changed = True
        elif result.pop:
            edge = "pop"
            self._pop(turn, frame, result.outputs)
            stack_changed = True
        else:
            edge = result.next_edge or "next"
            frame.node_id = resolve_edge(node, result.next_edge)

        passed_a_gate = (
            isinstance(node, GateNode) and result.push_graph is None and result.suspend is None
        )
        if passed_a_gate and node_id not in frame.passed_gates:
            frame.passed_gates.append(node_id)

        frame.attempts[node_id] = frame.attempts.get(node_id, 0) + 1
        await self._checkpoint(
            turn,
            step=StepWrite(
                run_id=turn.run_id,
                step_id=sid,
                seq=turn.seq + 1,
                node_id=node_id,
                edge=edge,
                state_patch=patch,
                llm_response=result.llm_response,
                started_at=started,
                ended_at=self.hooks.clock(),
            ),
            outbound=[message.text for message in result.outbound],
            suspend_detail=result.suspend.detail if result.suspend else None,
            suspend_status=result.suspend.status if result.suspend else None,
            suspend_node=node_id if result.suspend else None,
            suspend_frame_seq=frame.frame_seq if result.suspend else None,
            channel=ctx.channel,
        )
        return stack_changed

    async def _run_gate_recheck(
        self, turn: _Turn, graph: Graph, frame: Frame, gate_id: str
    ) -> None:
        """Re-push a gate's redirect because its predicate stopped holding.

        DESIGN.md section 6.6: "Gates fire on every entry to a frame, so an interrupt cannot be
        used to reach an unverified action." The redirect returns to the node the frame was
        *actually* at, not to the node after the gate, so nothing between the gate and here is
        re-executed - and the gate stays in ``passed_gates``, so entering the frame again
        checks it again.
        """
        gate = graph.nodes[gate_id]
        assert isinstance(gate, GateNode)
        started = self.hooks.clock()
        attempt = frame.attempts.get(gate_id, 0)
        sid = step_id(turn.run_id, frame.frame_seq, gate_id, attempt)
        self._push(
            turn,
            GraphInvocation(graph=gate.redirect, kind="gate_redirect", return_node=frame.node_id),
        )
        frame.attempts[gate_id] = attempt + 1
        await self._checkpoint(
            turn,
            step=StepWrite(
                run_id=turn.run_id,
                step_id=sid,
                seq=turn.seq + 1,
                node_id=gate_id,
                edge="redirect",
                started_at=started,
                ended_at=self.hooks.clock(),
            ),
        )

    async def _route_error(
        self,
        turn: _Turn,
        graph: Graph,
        frame: Frame,
        node: NodeBase,
        sid: str,
        started: datetime,
        exc: NodeError,
    ) -> bool:
        """DESIGN.md section 7.3: the node's ``on_error`` edge if it declares one, else handoff.

        Returns whether the loop should continue. The middle tier of 7.3 - "otherwise the
        frame's ``on_error`` graph" - has no place in the graph schema phase 1 delivered, so it
        is not silently invented here; the fall-through is straight to handoff.
        """
        on_error = getattr(node, "on_error", None)
        node_id = frame.node_id
        if not isinstance(on_error, str):
            # ``reason`` is the node's own diagnosis where it has one: DESIGN.md section 7.3
            # names ``llm_unavailable``, and phase 3 distinguishes it from a model that answered
            # with something the graph does not allow. Whoever picks the conversation up needs
            # to know which happened.
            await self._handoff(turn, node_id, exc.reason, str(exc), started=started, sid=sid)
            return False
        frame.node_id = on_error
        frame.attempts[node_id] = frame.attempts.get(node_id, 0) + 1
        await self._checkpoint(
            turn,
            step=StepWrite(
                run_id=turn.run_id,
                step_id=sid,
                seq=turn.seq + 1,
                node_id=node_id,
                edge="on_error",
                error=str(exc),
                started_at=started,
                ended_at=self.hooks.clock(),
            ),
        )
        return True

    async def _handoff(
        self,
        turn: _Turn,
        node_id: str,
        reason: str,
        detail: str,
        *,
        started: datetime | None = None,
        sid: str | None = None,
    ) -> None:
        """Give up on the turn and park the run for a human (DESIGN.md section 7.3).

        The handoff *node*, the packet and the queue sinks are phase 6; what phase 2 owns is
        that every failure path ends in a durable ``waiting_human`` run and one call to the
        hook, so phase 6 has one place to attach to.
        """
        frame = turn.frame
        attempt = frame.attempts.get(node_id, 0)
        step = sid or step_id(turn.run_id, frame.frame_seq, node_id, attempt)
        frame.attempts[node_id] = attempt + 1
        turn.status = "waiting_human"
        now = self.hooks.clock()
        await self._checkpoint(
            turn,
            step=StepWrite(
                run_id=turn.run_id,
                step_id=step,
                seq=turn.seq + 1,
                node_id=node_id,
                edge=None,
                error=f"{reason}: {detail}",
                started_at=started or now,
                ended_at=now,
            ),
            suspend_status="waiting_human",
            suspend_detail={"reason": reason, "detail": detail},
            suspend_node=node_id,
            suspend_frame_seq=frame.frame_seq,
            handoff_reason=reason,
        )
        turn.outcome.handoff_reason = reason
        await self.hooks.handoff(
            HandoffRequest(
                conversation_id=turn.conversation_id,
                run_id=turn.run_id,
                reason=reason,
                detail=detail,
                frames=list(turn.frames),
            )
        )

    # -- frame stack ---------------------------------------------------------------------

    def _push(self, turn: _Turn, invocation: GraphInvocation) -> None:
        """Push a frame. Sub-graph calls, gate redirects and (phase 6) interrupts all land here.

        The pushed frame carries the return node, so the caller's own ``node_id`` is untouched
        until the pop: a crash right after a push resumes inside the callee, which is where the
        stack says execution is.
        """
        graph = self.pack.graphs.get(invocation.graph)
        if graph is None:
            msg = f"no graph {invocation.graph!r} in pack {self.pack.id!r}"
            raise IncompatiblePackError(msg)
        state = {
            name: value
            for name, value in to_jsonable_python(invocation.inputs).items()
            if name in graph.state.model.model_fields
        }
        turn.frames.append(
            Frame(
                frame_seq=turn.next_frame_seq,
                graph_id=graph.id,
                node_id=graph.start,
                state=state,
                kind=invocation.kind,
                return_node=invocation.return_node,
                outputs_into=dict(invocation.outputs_into),
            )
        )
        turn.next_frame_seq += 1

    def _pop(self, turn: _Turn, frame: Frame, outputs: dict[str, Any]) -> None:
        """Pop a frame and map its outputs into the caller (DESIGN.md section 6.2, ``end``)."""
        turn.frames.pop()
        if not turn.frames:
            turn.status = "done"
            return
        caller = turn.frame
        clean = to_jsonable_python(outputs)
        for state_field, output_name in frame.outputs_into.items():
            caller.state[state_field] = clean.get(output_name)
        if frame.return_node is not None:
            caller.node_id = frame.return_node

    def _check_outputs(self, turn: _Turn, frame: Frame, result: NodeResult) -> None:
        """Refuse an output mapping the caller's state has no field for (finding R12).

        Checked before the pop rather than after it, so it is routed like any other node failure
        (DESIGN.md section 7.3) and names the mapping. Writing the value anyway produces an
        ``IncompatiblePackError`` one node later, which is reported to the operator as
        ``pack_incompatible`` - the wrong diagnosis for a mistake in a ``subgraph`` node's
        ``outputs``.
        """
        if not result.pop or not frame.outputs_into or len(turn.frames) < 2:
            return
        caller = turn.frames[-2]
        graph = self.pack.graphs.get(caller.graph_id)
        if graph is None:  # pragma: no cover - _graph raises on the caller's next entry
            return
        unknown = sorted(set(frame.outputs_into) - set(graph.state.model.model_fields))
        if unknown:
            msg = (
                f"{frame.node_id}: {frame.graph_id} returns into {unknown}, which "
                f"{caller.graph_id} does not declare in its state"
            )
            raise NodeError(msg)

    def _failed_gate(
        self, graph: Graph, frame: Frame, state: BaseModel, ctx: ConversationContext
    ) -> str | None:
        """The first gate this frame passed whose predicate no longer holds.

        Run on every entry to a frame - a resume, a return from a pushed frame, and (phase 6) a
        return from an interrupt - which is DESIGN.md section 6.6's "gates fire on every entry
        to a frame". Without it, suspending after a gate and coming back later would walk
        straight into the protected node with the precondition gone.
        """
        for gate_id in frame.passed_gates:
            node = graph.nodes.get(gate_id)
            if not isinstance(node, GateNode):  # pragma: no cover - the pack is validated
                continue
            runner = self._runner(graph, gate_id, node)
            assert isinstance(runner, GateRunner)
            if runner.check(state, ctx).push_graph is not None:
                return gate_id
        return None

    # -- persistence ---------------------------------------------------------------------

    async def _checkpoint(
        self,
        turn: _Turn,
        *,
        step: StepWrite,
        outbound: Sequence[str] = (),
        suspend_status: SuspendStatus | None = None,
        suspend_detail: dict[str, Any] | None = None,
        suspend_node: str | None = None,
        suspend_frame_seq: int | None = None,
        handoff_reason: str | None = None,
        channel: Channel | None = None,
    ) -> None:
        """One node, one transaction (DESIGN.md section 7.1)."""
        now = step.ended_at
        awaiting: dict[str, Any] | None = None
        timeout_at: datetime | None = None
        suspended_at: datetime | None = None
        if suspend_status is not None:
            suspended_at = now
            kind = "handoff" if handoff_reason else "node"
            # ``frame_seq`` beside ``node``: together they are the address the next resume event
            # is delivered to, and it has to survive in durable state rather than be re-derived
            # from a stack that may have moved (finding R2).
            awaiting = {
                "kind": kind,
                "status": suspend_status,
                "node": suspend_node,
                "frame_seq": suspend_frame_seq,
            }
            if handoff_reason:
                awaiting["reason"] = handoff_reason
            if suspend_detail:
                awaiting["detail"] = suspend_detail
            rule = self.pack.manifest.timeouts.rule(suspend_status, channel)
            if rule.seconds is not None:
                timeout_at = now + timedelta(seconds=rule.seconds)

        awaiting = self._with_turn_event(turn, awaiting)

        update = RunUpdate(
            run_id=turn.run_id,
            status=turn.status,
            frames=[frame.model_dump(mode="json") for frame in turn.frames],
            checkpoint_seq=step.seq,
            next_frame_seq=turn.next_frame_seq,
            turn_nodes=turn.turn_nodes + 1,
            updated_at=now,
            suspended_at=suspended_at,
            timeout_at=timeout_at,
            awaiting=awaiting,
            pack_fingerprint=self.pack.pin.fingerprint,
        )

        async def before_commit() -> None:
            await self.hooks.probe("checkpoint_before_commit", {"step_id": step.step_id})

        async with self.sessions() as session:
            await repo.write_checkpoint(
                session,
                run=update,
                step=step,
                conversation_id=turn.conversation_id,
                outbound=outbound,
                before_commit=before_commit,
            )
        turn.seq = step.seq
        turn.turn_nodes += 1
        turn.outcome.steps.append(step.step_id)
        turn.outcome.outbound.extend(outbound)
        await self.hooks.probe("after_checkpoint", {"step_id": step.step_id})
        if outbound:
            await self._flush_outbound(turn.conversation_id)

    async def _flush_outbound(self, conversation_id: uuid.UUID) -> None:
        """Hand every undelivered outbound row to the channel and mark it sent.

        Delivery happens after the commit, so a crash between the two leaves the rows
        ``pending_send`` and the next checkpoint - or the next turn - retries them. That is
        at-least-once towards the channel, which is the safe direction: phase 7's adapters own
        de-duplication at the transport.
        """
        async with self.sessions() as session, session.begin():
            waiting = await repo.pending_outbound(session, conversation_id)
            if not waiting:
                return
            await self.hooks.send(conversation_id, [OutboundMessage(text=m.text) for m in waiting])
            await repo.mark_sent(session, [m.id for m in waiting])

    async def _history(self, conversation_id: uuid.UUID) -> tuple[TranscriptMessage, ...]:
        """The turn window of DESIGN.md section 10, read from the database every time.

        Not cached across the turn on purpose. The window is part of what a prompted node sees,
        so it is part of what the node decides with, and phase 2's rule is that anything a turn
        depends on comes from durable state - a node re-executed after a crash must see the same
        window it saw before, which is the set of committed messages and nothing else.
        """
        if self.llm is None:
            return ()
        limit = self.pack.manifest.memory.window_messages
        async with self.sessions() as session, session.begin():
            rows = await repo.recent_messages(session, conversation_id, limit)
            return tuple(TranscriptMessage(author=row.author, text=row.text) for row in rows)

    async def _maybe_summarize(self, conversation_id: uuid.UUID) -> None:
        """Refresh the rolling summary if K turns have passed (DESIGN.md section 10).

        Called with the conversation lock still held, after the turn has settled, so the summary
        covers what actually happened. Everything it decides with is durable: ``turn_count`` and
        ``summary_turn`` are columns, and the summary and its marker are written together.

        A failing summariser is swallowed deliberately. The summary is prompt *context* - no
        node reads it to decide anything, and ``ctx.summary`` being stale or absent changes no
        durable outcome - so letting a model call that failed take a completed turn down with it
        would trade a real thing for a nice-to-have.
        """
        every = self.pack.manifest.memory.summarize_every_turns
        if not every:
            return
        async with self.sessions() as session, session.begin():
            conversation = await repo.get_conversation(session, conversation_id)
            if conversation is None:  # pragma: no cover - the caller just used it
                return
            turn_count = conversation.turn_count
            previous = conversation.summary
            if turn_count - conversation.summary_turn < every:
                return
            window = [
                (row.author, row.text)
                for row in await repo.recent_messages(
                    session, conversation_id, self.pack.manifest.memory.window_messages
                )
            ]
        try:
            summary = await self.hooks.summarize(
                SummaryHookRequest(
                    conversation_id=conversation_id,
                    turn_count=turn_count,
                    previous=previous,
                    transcript=window,
                )
            )
        except Exception:
            return
        if not summary:
            return
        async with self.sessions() as session, session.begin():
            await repo.write_summary(session, conversation_id, summary=summary, at_turn=turn_count)

    async def _begin_turn(self, turn: _Turn) -> None:
        """Mark the run running and reset the per-turn counter (DESIGN.md section 7.3).

        A resume is a turn too, for DESIGN.md section 10's "every K turns": the counter is
        advanced in the same transaction that marks the run running, for the same reason the
        claim advances it for a customer message.
        """
        turn.status = "running"
        turn.turn_nodes = 0
        async with self.sessions() as session, session.begin():
            await repo.count_turn(session, turn.conversation_id)
        await self._set(
            turn.run_id,
            status="running",
            turn_nodes=0,
            awaiting=self._with_turn_event(turn, None),
            timeout_at=None,
            suspended_at=None,
            frames=[frame.model_dump(mode="json") for frame in turn.frames],
            next_frame_seq=turn.next_frame_seq,
            updated_at=self.hooks.clock(),
        )

    def _with_turn_event(
        self, turn: _Turn, awaiting: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Add the undelivered turn event to what the run says it is waiting for."""
        if turn.pending_event is None:
            return awaiting
        merged = dict(awaiting or {})
        merged[TURN_EVENT_KEY] = turn.pending_event.model_dump(mode="json")
        return merged

    async def _set(self, run_id: uuid.UUID, **values: Any) -> None:
        async with self.sessions() as session, session.begin():
            await repo.set_run_fields(session, run_id, **values)

    async def _peek(self, conversation_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
        async with self.sessions() as session, session.begin():
            message = await repo.peek_next_pending(session, conversation_id)
            if message is None:
                return None
            return message.id, message.text

    async def _claim(self, turn: _Turn, message_id: uuid.UUID) -> bool:
        """Claim a message and begin its turn in one transaction (finding R1)."""

        async def before_commit() -> None:
            await self.hooks.probe("claim_before_commit", {"message_id": str(message_id)})

        async with self.sessions() as session:
            return await repo.claim_and_begin_turn(
                session,
                message_id=message_id,
                conversation_id=turn.conversation_id,
                run=repo.TurnStart(
                    run_id=turn.run_id,
                    frames=[frame.model_dump(mode="json") for frame in turn.frames],
                    next_frame_seq=turn.next_frame_seq,
                    awaiting=self._with_turn_event(turn, None),
                    pack_fingerprint=self.pack.pin.fingerprint,
                    updated_at=self.hooks.clock(),
                ),
                before_commit=before_commit,
            )

    def _suspended_at(self, run: _RunRow, frames: list[Frame]) -> tuple[int, str]:
        """Where the run suspended, from durable state (independent review finding R2).

        The suspend checkpoint records the frame and node it suspended at in ``run.awaiting``,
        which is the same place - and written in the same transaction - as the frame stack
        itself. Deriving the target from the stack *top* instead works only until something
        pushes a frame during the turn, and a gate re-check does exactly that while the reply is
        still undelivered. ``frames[-1]`` is the fall-back for a run suspended by an older
        version of the engine, whose ``awaiting`` has no ``frame_seq``; it is the same frame,
        because nothing has run since the suspension.
        """
        awaiting = run.awaiting or {}
        node_id = awaiting.get("node")
        frame_seq = awaiting.get("frame_seq")
        if isinstance(node_id, str) and isinstance(frame_seq, int):
            return frame_seq, node_id
        top = frames[-1]
        return top.frame_seq, top.node_id

    async def _conversation(self, conversation_id: uuid.UUID) -> Conversation:
        async with self.sessions() as session, session.begin():
            conversation = await repo.get_conversation(session, conversation_id)
            if conversation is None:
                msg = f"no conversation {conversation_id}"
                raise EngineError(msg)
            session.expunge(conversation)
            return conversation

    async def _run_for(self, conversation_id: uuid.UUID) -> _RunRow:
        async with self.sessions() as session, session.begin():
            run = await repo.load_run(session, conversation_id)
            if run is None:
                run = await repo.create_run(
                    session,
                    conversation_id=conversation_id,
                    pack_version=self.pack.manifest.version,
                    pack_fingerprint=self.pack.pin.fingerprint,
                )
            return _snapshot(run)

    # -- helpers -------------------------------------------------------------------------

    def _turn_state(self, run: _RunRow, outcome: TurnOutcome) -> _Turn:
        return _Turn(
            run_id=run.id,
            conversation_id=run.conversation_id,
            status="running",
            frames=[Frame.model_validate(frame) for frame in run.frames],
            seq=run.checkpoint_seq,
            next_frame_seq=run.next_frame_seq,
            turn_nodes=run.turn_nodes,
            outcome=outcome,
        )

    def _graph(self, frame: Frame) -> Graph:
        graph = self.pack.graphs.get(frame.graph_id)
        if graph is None:
            msg = (
                f"the run is inside graph {frame.graph_id!r}, which the loaded pack "
                f"{self.pack.id!r} {self.pack.manifest.version} does not define"
            )
            raise IncompatiblePackError(msg)
        return graph

    def _state(self, graph: Graph, frame: Frame) -> BaseModel:
        """Rebuild the frame's state model.

        A frame stored under an older pack version whose state shape has changed fails here,
        which is DESIGN.md section 6.7's "if none exists and the shapes differ, the
        conversation is handed off". The migration hook itself is phase 9.
        """
        try:
            return graph.state.model.model_validate(frame.state)
        except ValidationError as exc:
            msg = (
                f"the stored state of frame {frame.frame_seq} ({graph.id}.{frame.node_id}) does "
                f"not fit the state shape of the loaded pack: {exc}"
            )
            raise IncompatiblePackError(msg) from exc

    def _runner(self, graph: Graph, node_id: str, node: NodeBase) -> Any:
        key = (graph.id, node_id)
        runner = self._runners.get(key)
        if runner is None:
            runner = build_runner(node_id, node)
            self._runners[key] = runner
        return runner

    def _channel(self, conversation: Conversation) -> Channel:
        return "email" if conversation.channel == "email" else "web_chat"

    def _context(self, conversation: Conversation) -> ConversationContext:
        """The read-only ``ctx`` every node and expression sees (DESIGN.md sections 6.1, 10)."""
        raw = dict(conversation.context or {})
        raw.pop(ENTRY_INPUTS_KEY, None)
        customer = dict(raw.get("customer") or {})
        customer.setdefault("ref", conversation.customer_ref)
        raw["customer"] = customer
        raw["conversation_id"] = str(conversation.id)
        raw["channel"] = self._channel(conversation)
        raw["summary"] = conversation.summary
        return ConversationContext.model_validate(raw)
