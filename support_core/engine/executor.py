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
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.engine.errors import (
    EngineError,
    IncompatiblePackError,
    NodeError,
    StatePatchError,
)
from support_core.engine.hooks import (
    ConfirmDecision,
    EngineHooks,
    HandoffRequest,
    InterruptRequest,
)
from support_core.engine.hooks import ResumeOfferRequest as ResumeOfferHookRequest
from support_core.engine.hooks import SummaryRequest as SummaryHookRequest
from support_core.engine.interrupts import (
    INTERRUPT_RETURN_NODE,
    abandoned_notice,
    cancelled_notice,
    deferral_hint,
    deferral_notice,
    interrupts_allowed,
    interrupts_configured,
    resolve_intent,
    return_offer,
    switch_notice,
    unsure_hint,
    unsure_notice,
    workflow_intents,
)
from support_core.engine.locks import conversation_lock
from support_core.engine.runners import (
    DEFAULT_HANDOFF_MESSAGE,
    DESK_ACTION,
    HANDOFF_UNDELIVERED_MESSAGE,
    NO_TOOL_ACCESS,
    GateRunner,
    NodeRuntime,
    NodeToolAccess,
    build_runner,
    resolve_edge,
    tool_gateway_factory,
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
from support_core.graph.context import ConversationContext, CustomerContext
from support_core.graph.manifest import Channel, SuspendStatus
from support_core.graph.nodes import (
    ConfirmNode,
    GateNode,
    HandoffNode,
    LlmNode,
    NodeBase,
    ToolNode,
)
from support_core.graph.pack import Pack
from support_core.graph.routing import WorkflowIntent
from support_core.graph.schema import Graph
from support_core.llm.prompt import DeferredIntent, TranscriptMessage
from support_core.llm.service import LlmService
from support_core.llm.tool_loop import (
    ModelToolRunner,
    ReadOnlyToolGateway,
)
from support_core.storage import repositories as repo
from support_core.storage.models import Conversation, Run
from support_core.storage.repositories import ApprovalWrite, RunUpdate, StepWrite
from support_core.storage.session import make_session_factory
from support_core.tools.approval import hash_for
from support_core.tools.base import ToolError, ToolRefused
from support_core.tools.runtime import (
    CallSite,
    RegistryToolRunner,
    ToolCallResult,
    ToolRuntime,
)

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

WAIT_ACKNOWLEDGED = "wait_acknowledged"
"""Key on ``run.awaiting`` recording that the queued-message notice has been said once.

On ``awaiting`` rather than a column because it is a fact about *this* suspension: the next
handoff parks the run afresh and the customer is entitled to the sentence again."""

WAITING_ON_A_HUMAN_MESSAGE = (
    "Thank you - I have added that to the conversation, and it is with one of our people. "
    "I am not able to carry on myself until they have looked at it."
)
"""What a customer is told when their message is queued behind a human (review finding P6).

Said once per parking, and true of each clause: the message is a durable row, it is on the
handoff the desk reads, and the engine really will not act on it until somebody resumes. It
promises nothing about when, for the same reason
:data:`~support_core.engine.runners.DEFAULT_HANDOFF_MESSAGE` does not."""

DESK_PATCH_FORBIDDEN = frozenset(["identity_verified"])
"""State field names a desk ``resume`` patch may never write, whatever a graph declares.

One name so far, and it is the one that matters: DESIGN.md section 10 says
``identity_verified`` is set by the ``verify_identity`` sub-graph alone, through a tool.
``AppConfig`` is held to the same rule for a new conversation's starting context; this is that
rule on the other unauthenticated surface (review finding P1)."""


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
    turn_tool_calls: int
    outcome: TurnOutcome
    pending_event: ResumeEvent | None = None
    """The event that started this turn, until the node it belongs to consumes it.

    It is written into ``run.awaiting`` by every checkpoint while it is still pending, so a
    process that dies mid-turn does not lose the customer message that drove it: the message
    row is already claimed, and only the run row can say what it was for."""

    secondary_intents: list[dict[str, Any]] = field(default_factory=list)
    """Workflows the customer asked for that a graph would not stop for (DESIGN.md section 6.6
    step 5). Written by every checkpoint, like the frame stack, and surfaced to the root graph
    (section 19 step 15)."""

    notices: list[str] = field(default_factory=list)
    """Core-written sentences waiting to go out with the next checkpoint's messages.

    The engine says four things of its own (see :mod:`support_core.engine.interrupts`) and none
    of them belongs to a node, so they ride on the next node's checkpoint rather than inventing a
    step of their own: a message written outside a checkpoint is a message a crash can duplicate
    or lose, which is the one thing phase 2 exists to prevent."""

    def take_notices(self) -> list[str]:
        """Hand over the pending core sentences and forget them.

        Taken rather than read, and taken *inside* the call that composes a checkpoint, so a
        notice is emitted exactly once: it is written by the same transaction that records the
        step, and a crash before that transaction commits leaves the notice unsent and the run
        where it was, which is a state the re-entered turn produces again from scratch."""
        pending, self.notices = list(self.notices), []
        return pending

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
    turn_tool_calls: int
    awaiting: dict[str, Any] | None
    pack_fingerprint: str | None
    recovery_attempts: int = 0
    secondary_intents: list[dict[str, Any]] = field(default_factory=list)


def _author(stored: str) -> Literal["agent", "human"]:
    """Who a stored outbound row says wrote it. Anything unexpected is the agent."""
    return "human" if stored == "human" else "agent"


def _spent(gateways: Sequence[ReadOnlyToolGateway]) -> int:
    """Model-loop tool calls this node made, refusals included.

    Refusals count, as they do inside the gateway: "ask for the refund tool a thousand times" is
    not a way to buy an unbounded turn either (review finding V6).
    """
    return sum(len(gateway.calls) for gateway in gateways)


def _model_tools(node: NodeBase) -> tuple[str, ...]:
    """The tools this node offers the model, from the validated graph (DESIGN.md section 8.4).

    Read here and given to the runner, so the node's allow-list is enforced in the runtime as
    well as in the gateway (review finding R2). A node that is not an ``llm`` node offers the
    model nothing, so a model-loop call from one is refused whatever it asks for.
    """
    return tuple(node.tools) if isinstance(node, LlmNode) else ()


def _action_hasher(
    tools: ToolRuntime,
) -> Callable[[str, Mapping[str, Any]], tuple[str, dict[str, Any]]]:
    """A closure that hashes a proposed action and can do nothing else."""

    def hash_action(name: str, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        tool = tools.registry.get(name)
        if tool is None:
            known = ", ".join(tools.registry.names) or "none"
            msg = f"no tool named {name!r} is registered by this pack; it exports {known}"
            raise ToolRefused(msg)
        return hash_for(tool, args)

    return hash_action


def _suspend_detail(run: _RunRow) -> dict[str, Any]:
    """What the node recorded when it suspended, for the event that wakes it.

    A ``confirm`` node's approval hash lives here: the customer is answering the proposal that
    was *shown* to them, and re-deriving it from the state as it stands on resume would answer a
    different question after a gate redirect re-entered the frame in between (section 6.6).
    """
    detail = (run.awaiting or {}).get("detail")
    return dict(detail) if isinstance(detail, dict) else {}


def _snapshot(run: Run) -> _RunRow:
    return _RunRow(
        id=run.id,
        conversation_id=run.conversation_id,
        status=run.status,
        frames=[dict(frame) for frame in run.frames],
        checkpoint_seq=run.checkpoint_seq,
        next_frame_seq=run.next_frame_seq,
        turn_nodes=run.turn_nodes,
        turn_tool_calls=run.turn_tool_calls,
        awaiting=dict(run.awaiting) if run.awaiting else None,
        pack_fingerprint=run.pack_fingerprint,
        recovery_attempts=run.recovery_attempts,
        secondary_intents=[dict(intent) for intent in run.secondary_intents],
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
        tools: ToolRuntime | None = None,
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

        self.tools = tools or ToolRuntime(pack.registry, self.sessions, clock=self.hooks.clock)
        """The tool runtime (DESIGN.md section 8). The executor is the only thing that holds it:
        a node gets a :class:`~support_core.engine.runners.NodeToolAccess` built for it, and an
        ``llm`` node gets a :class:`~support_core.llm.tool_loop.ReadOnlyToolGateway`, neither of
        which can reach this object or widen what it will do."""

        self.tool_runner: ModelToolRunner | None = tool_runner
        """An override for the model-loop runner, for tests that need a specific one. ``None``
        means one is built per node from the registry, which is the ordinary case."""

        self._tool_risk = dict(pack.registry.risks)
        """Risk tiers for the model loop's cross-check, from the registry and not from the
        pack's YAML declarations (phase-1 deferred finding I)."""

        self._workflow_intents: tuple[WorkflowIntent, ...] | None = None
        """The root graph's declared edges as workflows (DESIGN.md section 6.6), derived once."""

    # -- entry points --------------------------------------------------------------------

    async def start_conversation(
        self,
        *,
        channel: Channel = "web_chat",
        customer_ref: str | None = None,
        context: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        channel_key: str | None = None,
    ) -> uuid.UUID:
        """Create a conversation and its run. Returns the conversation id.

        ``context`` is the :class:`~support_core.graph.context.ConversationContext` the pack's
        expressions read as ``ctx``; ``inputs`` seeds the entry graph's state (see
        :data:`ENTRY_INPUTS_KEY`); ``channel_key`` is the channel's own name for the conversation
        (DESIGN.md section 12), unique per channel, by which a reconnecting client or a mail
        thread finds it again.
        """
        stored = dict(context or {})
        if inputs:
            stored[ENTRY_INPUTS_KEY] = to_jsonable_python(inputs)
        async with self.sessions() as session, session.begin():
            conversation = await repo.create_conversation(
                session,
                channel=channel,
                customer_ref=customer_ref,
                context=stored,
                channel_key=channel_key,
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
        """The desk returning control, or closing (DESIGN.md sections 7.2, 13).

        Both go through the graph where a ``handoff`` node is waiting for them. DESIGN.md
        section 13 gives that node a ``closed`` edge as well as a ``resumed`` one, so "take over
        fully" is a decision the *pack* expresses - usually an ``end`` that finishes the
        conversation, but a pack that wants to say goodbye first can. Closing the run from
        underneath a waiting node would make the ``closed`` edge unreachable and leave the frame
        stack pointing at a node that never ran.

        A run parked by the engine's own failure routing has no node waiting on anything, and
        there ``close`` still means what it did in phase 2: end the conversation.
        """
        payload: dict[str, Any] = dict(patch or {})
        payload[DESK_ACTION] = "close" if close else "resume"
        if close and not await self._node_is_waiting(conversation_id):
            return await self._close(conversation_id)
        return await self._resume(
            conversation_id,
            ResumeEvent(kind="human", text=text, payload=payload),
            expected="waiting_human",
            patch=patch,
        )

    async def check_state_patch(
        self, conversation_id: uuid.UUID, patch: Mapping[str, Any] | None
    ) -> None:
        """Would :meth:`resume_human` accept this patch? Raises if not (review finding P1).

        For a caller that has to do something *before* the resume it cannot take back - the desk
        writes the human's parting message into the transcript first - so that a refused patch is
        an error the operator reads rather than a message the customer reads about a hand-back
        that never happened. Takes no lock and writes nothing; :meth:`resume_human` checks again
        inside the lock, and that check is the authoritative one.
        """
        if not patch:
            return
        run = await self._run_for(conversation_id)
        turn = self._turn_state(run, TurnOutcome(conversation_id=conversation_id, run_id=run.id))
        if not turn.frames:
            msg = "this run has no frame a state patch could apply to"
            raise StatePatchError(msg, status_code=409)
        await self._check_patch(turn, patch)

    async def _node_is_waiting(self, conversation_id: uuid.UUID) -> bool:
        """Whether a node suspended this run, as opposed to the engine parking it."""
        run = await self._run_for(conversation_id)
        return (run.awaiting or {}).get("kind") == "node"

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
        await self._acknowledge_the_wait(run)
        await self._flush_outbound(conversation_id)
        await self._maybe_summarize(conversation_id)
        outcome.status = run.status  # type: ignore[assignment]
        return outcome

    async def _acknowledge_the_wait(self, run: _RunRow) -> None:
        """Say something, once, to a customer whose message is queued behind a person (P6).

        A run parked ``waiting_human`` is not resumed by a customer message: the message stays
        ``pending`` and waits for the desk, which is the right refusal - a topic change must not
        smuggle a workflow past the person it was escalated to. What was wrong was the ending.
        The customer had just been told a person would pick this up, they answered, and nothing
        happened at all: on web chat a message into a void, and on email, which phase 7 adds, a
        silently swallowed reply.

        So core says one sentence - not a turn, no lock beyond the one already held, no
        checkpoint, because nothing executed - and says it once per parking, recorded on
        ``run.awaiting`` in the same shape everything else about a suspension is recorded. The
        desk sees the message itself on the handoff view; this is the customer's half.
        """
        if run.status != "waiting_human":
            return
        awaiting = dict(run.awaiting or {})
        if awaiting.get(WAIT_ACKNOWLEDGED):
            return
        async with self.sessions() as session, session.begin():
            if not await repo.pending_inbound(session, run.conversation_id, limit=1):
                return
            await repo.add_outbound(
                session, conversation_id=run.conversation_id, text_=WAITING_ON_A_HUMAN_MESSAGE
            )
            awaiting[WAIT_ACKNOWLEDGED] = True
            await repo.set_run_fields(
                session, run.id, awaiting=awaiting, updated_at=self.hooks.clock()
            )

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
            frame_seq, node_id = self._suspended_at(run, turn.frames)
            turn.pending_event = ResumeEvent(
                kind="customer_message",
                text=body,
                message_id=message_id,
                detail=_suspend_detail(run),
                target_frame_seq=frame_seq,
                target_node_id=node_id,
            )
            await self._interrupt(turn, run, ctx, body)
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
        turn.turn_tool_calls = 0
        claimed = await self._claim(turn, message_id)
        if not claimed:  # pragma: no cover - impossible while we hold the conversation lock
            return None
        await self.hooks.probe("after_claim", {"message_id": str(message_id)})
        return turn

    async def _interrupt(
        self, turn: _Turn, run: _RunRow, ctx: ConversationContext, body: str
    ) -> None:
        """DESIGN.md section 6.6, steps 2 to 5, applied to a message that arrived mid-workflow.

        Runs *before* the claim, on a message read without claiming it, which is the shape phase
        2's resolution built for exactly this (BACKLOG.md decisions log): the check is a model
        call and must not sit inside the transaction that claims the message.

        The four answers:

        ``continue`` and ``unclear``
            The suspended node resumes with the reply, which is what :meth:`_claim_turn` has
            already prepared. ``unclear`` is deliberately not a third behaviour: the node that
            asked the question is better placed to make sense of a confusing reply than a
            classifier that has already said it cannot.
        ``new_intent`` where the current graph allows interrupts
            Park the suspended frame and push the workflow. The message is *consumed by the
            check* - it is what said to switch - so no node is given it and nothing is put back
            on the queue. The interrupting frame is fresh, so its own gates run from its start
            node: an interrupt is not a way into an unverified action.
        ``new_intent`` where it does not
            Resume the suspended node with a hint, and record the intent so the root graph can
            offer it later (step 5, and section 19 steps 8 and 15).
        ``cancel``
            Unwind to the root frame. Allowed from every graph including a ``blocked_in`` one:
            refusing to let a customer stop is worse than any workflow it interrupts, and unlike
            an interrupt it reaches nothing new.
        """
        frames = list(turn.frames)
        if not frames or not interrupts_configured(self.pack.manifest):
            # A pack that says nothing about interrupts gets no model call: the only answer the
            # engine could act on is `continue`, which is what happens anyway.
            return
        event = turn.pending_event
        if event is not None and event.target_node_id == INTERRUPT_RETURN_NODE:
            # The customer is answering "shall we go back to X?" - that is not an interrupt, and
            # classifying it as one would read "no thanks" as a cancellation of the wrong thing.
            return
        frame = frames[-1]
        if frame.kind == "root":
            # Nothing is in progress to interrupt. DESIGN.md section 6.5 gives the root graph an
            # intent classifier it "loops back to after each sub-graph returns", so a customer
            # who changes the subject while the *root* frame is waiting is already handled by the
            # pack's own graph - and handled better, because that classifier knows every edge the
            # root declares and this check knows only the ones that lead to a workflow. Parking a
            # root frame would also mean offering it back ("shall we return to: is there anything
            # else?"), which is not a question anybody should be asked.
            return
        intents = self._intents()
        decision = await self.hooks.interrupt_check(
            InterruptRequest(
                message=body,
                ctx=ctx,
                frames=frames,
                current_graph=frame.graph_id,
                current_node=frame.node_id,
                question=self._question_asked(run),
                intents=[intent.as_tuple() for intent in intents],
                interruptible=interrupts_allowed(self.pack.manifest, frame.graph_id),
                window=[(m.author, m.text) for m in await self._history(turn.conversation_id)],
            )
        )
        if decision.kind not in {"cancel", "new_intent"}:
            return
        if decision.confidence < self.pack.manifest.llm.confidence_threshold:
            # Review finding P3. The check's confidence was collected, carried through three
            # layers and read by nobody, so a `cancel` at confidence 0.0 unwound the stack and a
            # `new_intent` at 0.0 parked a workflow. `llm.confidence_threshold` is the pack's own
            # answer to "how sure does a model have to be before the engine acts on it"
            # (DESIGN.md section 11.3) and there is no reason this decision is exempt: these are
            # the only two answers here that destroy anything. Below it the engine does not act
            # and does not pretend it understood - it says so, hints the node, and the node asks
            # its question again.
            self._unsure(turn)
            return
        if decision.kind == "cancel":
            self._cancel(turn)
            return
        intent = resolve_intent(intents, decision.graph or decision.label)
        if intent is None or intent.graph == frame.graph_id:
            # An intent the root graph does not declare is not a workflow this conversation can
            # reach, and one naming the graph we are already in is a continue however it was
            # spelled. Neither is guessed at.
            return
        if not interrupts_allowed(self.pack.manifest, frame.graph_id):
            self._defer(turn, intent, body, frame.graph_id)
            return
        self._switch(turn, intent)

    def _pending_intents(self, turn: _Turn, frame: Frame) -> tuple[DeferredIntent, ...]:
        """What a node in the *root* frame is shown of the customer's deferred requests.

            `root` loops to classify; the engine surfaces the recorded secondary intent. The
            model chooses `update_address`. - DESIGN.md section 19 step 15

        Surfaced, not acted on: the engine puts the customer's own words in front of the root
        graph's classifier as untrusted data, and the *model* chooses among the edges the graph
        declares, which is principle 2 exactly. The engine pushing the workflow itself would be
        the engine inventing a transition.

        Only in the root frame, because that is where section 19 puts it and because a
        classifier inside a workflow has no edge to a different workflow anyway; showing it a
        request it cannot act on would be noise at best.
        """
        if frame.kind != "root" or not turn.secondary_intents:
            return ()
        return tuple(
            DeferredIntent(
                label=str(intent.get("label") or intent.get("graph") or ""),
                graph=str(intent.get("graph") or ""),
                said=str(intent.get("said") or ""),
            )
            for intent in turn.secondary_intents
        )

    def _label_for(self, graph_id: str) -> str | None:
        """The root graph's edge label for a workflow, where it declares one (finding P8)."""
        intent = resolve_intent(self._intents(), graph_id)
        return intent.label if intent is not None else None

    def _intents(self) -> tuple[WorkflowIntent, ...]:
        if self._workflow_intents is None:
            self._workflow_intents = workflow_intents(self.pack)
        return self._workflow_intents

    def _question_asked(self, run: _RunRow) -> str | None:
        """The prompt the suspended node showed, where the suspension recorded one."""
        detail = _suspend_detail(run)
        prompt = detail.get("prompt")
        return str(prompt) if isinstance(prompt, str) else None

    def _switch(self, turn: _Turn, intent: WorkflowIntent) -> None:
        """Park the suspended frame and push the interrupting workflow (step 4)."""
        turn.frame.offer_return = True
        turn.pending_event = None  # the check consumed the message; no node is waiting for it
        turn.notices.append(switch_notice(intent))
        self._push(turn, GraphInvocation(graph=intent.graph, kind="interrupt"))

    def _defer(self, turn: _Turn, intent: WorkflowIntent, said: str, current: str) -> None:
        """Resume the current node with a hint and record the intent (step 5)."""
        if turn.pending_event is not None:
            turn.pending_event = turn.pending_event.model_copy(
                update={"hint": deferral_hint(intent, current)}
            )
        turn.notices.append(deferral_notice(intent, current))
        recorded = {
            "graph": intent.graph,
            "label": intent.label,
            "said": said,
            "at": self.hooks.clock().isoformat(),
        }
        if not any(item.get("graph") == intent.graph for item in turn.secondary_intents):
            turn.secondary_intents.append(recorded)

    def _unsure(self, turn: _Turn) -> None:
        """Resume the suspended node, saying the engine did not follow (review finding P3)."""
        if turn.pending_event is not None:
            turn.pending_event = turn.pending_event.model_copy(update={"hint": unsure_hint()})
        turn.notices.append(unsure_notice())

    def _cancel(self, turn: _Turn) -> None:
        """Unwind every frame above the root and acknowledge (step 2's ``cancel``).

        Popping is the whole of it: nothing durable is undone, because nothing durable was done
        by the frames being dropped - a consumed approval stays consumed and an executed tool
        stays executed, which is why the sentence core says claims only that it stopped.
        """
        turn.pending_event = None
        while len(turn.frames) > 1:
            self._pop(turn, turn.frame, {})
        turn.notices.append(cancelled_notice())

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
            await self._set(run.id, status="idle", turn_nodes=0, turn_tool_calls=0, awaiting=None)
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
                # Checked before anything at all is written, and raising leaves the run exactly
                # as it was: nothing in this method has touched the database yet, and the frame
                # stack is still the in-memory copy `_turn_state` built (review finding P1).
                await self._check_patch(turn, patch)
                turn.frame.state.update(to_jsonable_python(patch))
            # A run parked by the engine itself (a limit, a node failure, a timeout) has no
            # node waiting on an event: re-run the node the human unblocked.
            awaiting = run.awaiting or {}
            if awaiting.get("kind") == "node":
                frame_seq, node_id = self._suspended_at(run, turn.frames)
                turn.pending_event = event.model_copy(
                    update={
                        "target_frame_seq": frame_seq,
                        "target_node_id": node_id,
                        "detail": _suspend_detail(run),
                    }
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

    async def _check_patch(self, turn: _Turn, patch: Mapping[str, Any]) -> None:
        """Refuse a desk state patch the frame may not hold (review finding P1).

        DESIGN.md section 13 lets a human "hand back (``resume`` with optional state patch, for
        example marking an override as approved)". It does not say the patch is arbitrary, and
        treating it as arbitrary is what made one mistyped field unrecoverable: the key went into
        ``run.frames`` where no endpoint could remove it, every later entry to the frame failed
        the graph's state model, and the run was parked for ever under ``pack_incompatible`` -
        the wrong DESIGN.md section 7.3 reason, because the pack was never at fault.

        Three rules, checked against the frame the run is actually suspended in. Nothing has run
        since that suspension, so the suspended frame is the top of the stack (see
        :meth:`_suspended_at`), and it is the frame the patch is applied to.

        1. **Every field is one the graph declares.** The refusal names the offending fields and
           lists the ones that exist, because the person reading it is a desk operator who has
           mistyped something, not a programmer reading a traceback.
        2. **Never** :attr:`identity_verified`. DESIGN.md section 10 gives that to the
           ``verify_identity`` sub-graph alone, which sets it through a tool; ``AppConfig`` is
           already held to the same rule for a new conversation's context, and a desk with no
           authentication that could grant it would open every identity gate in the pack.
        3. **Not while an approval is live on this frame.** An unconsumed ``action_approval`` is
           a proposal the customer agreed to, computed from this frame's state. Editing that
           state underneath it either changes what they agreed to or produces a hash mismatch at
           the tool nobody at the desk can see. The desk's one route to an action is ``approve``,
           which copies the customer's own row (DESIGN.md section 8.2).
        """
        frame = turn.frame
        model = self._graph(frame).state.model
        declared = model.model_fields
        forbidden = [name for name in patch if name in DESK_PATCH_FORBIDDEN]
        unknown = [name for name in patch if name not in declared and name not in forbidden]
        problems: list[str] = []
        if forbidden:
            problems.append(
                f"a desk patch may not set {forbidden}: DESIGN.md section 10 gives "
                f"identity_verified to the verify_identity sub-graph alone, which sets it "
                f"through a tool"
            )
        if unknown:
            problems.append(
                f"graph {frame.graph_id!r} does not declare the state field(s) {unknown}; "
                f"it declares {sorted(declared)}"
            )
        if problems:
            raise StatePatchError("; ".join(problems))
        try:
            model.model_validate({**frame.state, **to_jsonable_python(dict(patch))})
        except ValidationError as exc:
            msg = f"the patch does not fit the state shape of graph {frame.graph_id!r}: {exc}"
            raise StatePatchError(msg) from exc
        async with self.sessions() as session, session.begin():
            live = await repo.live_approvals(session, turn.conversation_id)
        blocking = [
            approval
            for approval in live
            if approval.run_id == turn.run_id and approval.frame_seq == frame.frame_seq
        ]
        if blocking:
            names = sorted({approval.tool for approval in blocking})
            msg = (
                f"this frame holds an approval the customer has already given for {names} and "
                f"nothing has yet run; a state patch here would change what they agreed to. "
                f"Approve or let the action lapse first"
            )
            raise StatePatchError(msg, status_code=409)

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
            # As in the recovery sweep: a sink that is itself down must not stop the sweep
            # telling anybody about the conversations behind this one. The run is parked.
            with suppress(Exception):
                await self.hooks.handoff(
                    HandoffRequest(
                        conversation_id=conversation_id,
                        run_id=run.id,
                        reason="timeout",
                        detail=f"timed out in {run.status}",
                        frames=[Frame.model_validate(frame) for frame in run.frames],
                        node_id=str((run.awaiting or {}).get("node") or "") or None,
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

            if frame.offer_return:
                # An interrupted workflow the customer has to be offered back (section 6.6 step
                # 4). *After* the gate re-check above, deliberately: a gate that stopped holding
                # while this frame was parked pushes its redirect first, so returning to a
                # workflow is never a way back into one whose precondition has lapsed.
                if await self._offer_return(turn, ctx, frame):
                    check_gates = True
                    continue
                if turn.status != "running":
                    return turn.pending_event
                check_gates = False
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
            # The tool budget of DESIGN.md section 5.1 is per *turn*, so what this node may spend
            # is what the turn has left (review finding V6). ``opened`` collects the gateways the
            # node built, so the checkpoint can record what it actually spent.
            opened: list[ReadOnlyToolGateway] = []
            budget = max(
                0, self.pack.manifest.limits.max_tool_calls_per_turn - turn.turn_tool_calls
            )
            site = CallSite(
                conversation_id=turn.conversation_id,
                run_id=turn.run_id,
                frame_seq=frame.frame_seq,
                node_id=frame.node_id,
                step_id=sid,
                customer=ctx.customer,
                channel=ctx.channel,
            )
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
                tool_gateway=tool_gateway_factory(
                    self.tool_runner
                    or RegistryToolRunner(self.tools, site, allowed=_model_tools(node)),
                    tool_risk=self._tool_risk,
                    max_calls=budget,
                    step_id=sid,
                    record=opened.append,
                ),
                tools=self._tool_access(node, site),
                pending_intents=self._pending_intents(turn, frame),
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
                self._check_result(turn, frame, node, result)
            except ToolError as exc:
                # A pack-registered node type is arbitrary Python and may call the tool runtime
                # without translating its refusal. A refusal is a run-time failure of the node,
                # which DESIGN.md section 7.3 routes; letting it escape the loop would turn "the
                # runtime said no" into a dead turn.
                reason = "tool_refused" if isinstance(exc, ToolRefused) else "tool_failed"
                refused = NodeError(f"{frame.node_id}: {exc}", reason=reason)
                turn.turn_tool_calls += _spent(opened)
                if delivering:
                    turn.pending_event = None
                if not await self._route_error(turn, graph, frame, node, sid, started, refused):
                    return turn.pending_event
                check_gates = False
                continue
            except NodeError as exc:
                turn.turn_tool_calls += _spent(opened)
                if delivering:
                    turn.pending_event = None
                if not await self._route_error(turn, graph, frame, node, sid, started, exc):
                    return turn.pending_event
                check_gates = False
                continue
            turn.turn_tool_calls += _spent(opened)
            if delivering:
                turn.pending_event = None
            await self.hooks.probe("after_node", {"step_id": sid, "node_id": frame.node_id})
            check_gates = await self._advance(turn, ctx, graph, frame, node, result, sid, started)
        return turn.pending_event

    def _tool_access(self, node: NodeBase, site: CallSite) -> NodeToolAccess:
        """What this node may do with the tool runtime (DESIGN.md section 8.2's table).

        The decision is made here, from the *validated graph's* declaration of what the node is,
        and it is expressed as closures that already carry the answer. A node cannot argue with
        it, because there is nothing on ``rt`` to argue with: a node the graph does not declare
        as ``type: tool`` holds an ``invoke`` that refuses everything, and a ``tool`` node holds
        one that can call exactly the tool it declared, with exactly the approval binding the
        graph gave it.

        ``hash_action`` is granted to every node, because it runs nothing: it turns a proposed
        action into ``sha256(tool_name + canonical_json(args))``. A ``confirm`` node needs it,
        and a node that computes a hash of something it cannot call has achieved nothing.
        """
        hasher = _action_hasher(self.tools)
        if not isinstance(node, ToolNode):
            return NodeToolAccess(
                invoke=NO_TOOL_ACCESS.invoke, complete=NO_TOOL_ACCESS.complete, hash_action=hasher
            )
        allowed = (node.tool,)
        requires_approval = node.requires_approval

        async def invoke(name: str, args: Mapping[str, Any]) -> ToolCallResult:
            return await self.tools.invoke(
                tool_name=name,
                args=args,
                site=site,
                caller="tool_node",
                allowed=allowed,
                requires_approval=requires_approval,
            )

        async def complete(name: str, payload: Mapping[str, Any], key: str) -> ToolCallResult:
            if name not in allowed:  # pragma: no cover - the node passes its own tool
                msg = f"{site.node_id}: {name!r} is not this node's tool"
                raise ToolRefused(msg)
            return await self.tools.complete_async(
                tool_name=name, site=site, payload=payload, key=key
            )

        return NodeToolAccess(
            invoke=invoke, complete=complete, hash_action=hasher, declared=allowed
        )

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
        approval = self._approval_write(turn, frame, node, node_id, result)
        customer = self._customer_write(node, result, ctx)
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
        # A node that got through is a node that is not failing, whatever it did last time. The
        # counter that bounds DESIGN.md section 7.3's retries counts *consecutive* failures, so
        # an ordinary loop through a node is not a retry (see :meth:`_route_error`).
        frame.errors.pop(node_id, None)
        detail = result.suspend.detail if result.suspend else None
        outbound = [message.text for message in result.outbound]
        if result.suspend is not None and isinstance(node, HandoffNode):
            # DESIGN.md section 13's packet, built and delivered *before* the checkpoint that
            # parks the run. After it would leave a window in which the run is waiting_human and
            # nobody has been told - which no sweep looks for, because a suspended run is not a
            # stalled one. Before it, a crash re-executes the node, and delivery is idempotent
            # under (run, step).
            detail = dict(detail or {})
            queued = await self._tell_a_human(
                turn,
                node_id,
                node.reason,
                detail.get("detail"),
                sid,
                next_steps=list(node.next_steps),
            )
            detail["queued"] = queued
            if not queued:
                # DESIGN.md section 14: never promise an escalation that did not happen. The
                # pack's own sentence is written for the case where somebody was paged - the
                # sample pack's says "I have passed this to a billing specialist ... They will
                # reply here" - and saying it when no sink took the packet is exactly the
                # forbidden promise this phase's pack change was made to remove. Core replaces
                # it with one that claims only what is true (review finding P2). The run is
                # still parked and the packet is still in the trace: what changed is that
                # nobody was told, and the customer is not told otherwise.
                outbound = [HANDOFF_UNDELIVERED_MESSAGE]
            turn.outcome.handoff_reason = node.reason
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
            outbound=[*turn.take_notices(), *outbound],
            suspend_detail=detail,
            suspend_status=result.suspend.status if result.suspend else None,
            suspend_node=node_id if result.suspend else None,
            suspend_frame_seq=frame.frame_seq if result.suspend else None,
            channel=ctx.channel,
            approval=approval,
            customer_context=customer,
        )
        return stack_changed

    def _approval_write(
        self, turn: _Turn, frame: Frame, node: NodeBase, node_id: str, result: NodeResult
    ) -> ApprovalWrite | None:
        """Turn a ``confirm`` node's proposal into the row the checkpoint writes (8.2).

        Only a node the graph declares as ``type: confirm`` may produce one. A pack-registered
        custom node type returning an ``approval`` is a pack trying to authorise its own actions,
        and it is a node error rather than a silently ignored field: silently ignoring it would
        let a pack believe it had a working confirmation.
        """
        if result.approval is None:
            return None
        assert isinstance(node, ConfirmNode)  # _check_result refused anything else
        return ApprovalWrite(
            conversation_id=turn.conversation_id,
            run_id=turn.run_id,
            frame_seq=frame.frame_seq,
            node_id=node_id,
            step_id=step_id(turn.run_id, frame.frame_seq, node_id, frame.attempts.get(node_id, 0)),
            tool=result.approval.tool,
            args=dict(result.approval.args),
            args_hash=result.approval.args_hash,
            approved_by=result.approval.approved_by,
            approved_at=self.hooks.clock(),
        )

    def _customer_write(
        self, node: NodeBase, result: NodeResult, ctx: ConversationContext
    ) -> dict[str, Any] | None:
        """Apply a tool's ``ctx.customer`` change (DESIGN.md section 19 step 9).

        In memory *and* in the checkpoint. In memory because a gate re-check later in the same
        turn has to see it - that is the whole point of ``verify_otp`` - and in the checkpoint
        because a turn that ends here must not leave the effect durable and the context not.
        """
        if result.customer_patch is None:
            return None
        assert isinstance(node, ToolNode)  # _check_result refused anything else
        customer = dict(result.customer_patch)
        ctx.customer = CustomerContext.model_validate(customer)
        return customer

    async def _offer_return(self, turn: _Turn, ctx: ConversationContext, frame: Frame) -> bool:
        """Ask whether to go back to a parked workflow, and act on the answer (6.6 step 4).

            When it ends, the engine asks the customer whether to return to the interrupted
            workflow, then resumes or abandons it. - DESIGN.md section 6.6

        A core step rather than a node: no graph declares it, and no pack should have to. It
        writes a trace step like anything else, under the reserved node id
        :data:`~support_core.engine.interrupts.INTERRUPT_RETURN_NODE`, so a conversation that
        did this is visible in the trace and its checkpoint is one transaction like every other.

        Returns whether the frame stack changed - which happens only on "no", where the parked
        frame is popped and its caller carries on with no outputs from it.
        """
        node_id = INTERRUPT_RETURN_NODE
        attempt = frame.attempts.get(node_id, 0)
        sid = step_id(turn.run_id, frame.frame_seq, node_id, attempt)
        # Advanced now, not per outcome: every path below writes exactly one checkpoint, and a
        # step id is unique per row (``uq_trace_step_step_id``). Making the offer and reading the
        # answer are two steps of one conversation, and so is each re-offer after an unreadable
        # reply; the trace shows them as the separate things they are.
        frame.attempts[node_id] = attempt + 1
        started = self.hooks.clock()
        event = turn.pending_event
        answering = (
            event is not None
            and event.target_frame_seq == frame.frame_seq
            and event.target_node_id == node_id
        )
        # The pack's own word for this workflow, not the graph file's (review finding P8): the
        # root graph's edge label is what a customer was offered in the first place, so "shall we
        # go back to update address?" reads from the same vocabulary as everything else they were
        # told. It falls back to the graph id for a parked frame the root declares no edge to.
        label = self._label_for(frame.graph_id)
        offer = return_offer(frame.graph_id, label)
        if not answering:
            await self._record_offer(turn, frame, sid, started, offer, edge="offer")
            return False

        assert event is not None
        turn.pending_event = None
        try:
            decision = await self.hooks.resume_offer(
                ResumeOfferHookRequest(
                    workflow=frame.graph_id,
                    offer=str(event.detail.get("prompt") or offer),
                    reply=event.text or "",
                    ctx=ctx,
                    window=[(m.author, m.text) for m in await self._history(turn.conversation_id)],
                )
            )
        except Exception:
            # This is not a node, so a failure here has no ``on_error`` edge and would otherwise
            # take the turn down - which for a *reading of a yes-or-no answer* is the worst
            # available outcome, because asking again costs one message. Unreadable is the answer
            # this reader gives when it cannot read something, and a reader that fell over could
            # not read it either. The structured implementation already catches its own provider
            # failures; this is for everything else.
            decision = ConfirmDecision(answer="unclear")
        if decision.answer == "unclear":
            # The same answer a confirmation gives an unreadable reply, for the same reason: the
            # cost of asking again is one message and the cost of guessing is a workflow either
            # abandoned or forced on somebody.
            await self._record_offer(turn, frame, sid, started, offer, edge="offer")
            return False
        frame.offer_return = False
        if decision.answer == "no":
            turn.notices.append(abandoned_notice(frame.graph_id, label))
            self._pop(turn, frame, {})
            await self._checkpoint(
                turn,
                step=StepWrite(
                    run_id=turn.run_id,
                    step_id=sid,
                    seq=turn.seq + 1,
                    node_id=node_id,
                    edge="abandoned",
                    started_at=started,
                    ended_at=self.hooks.clock(),
                ),
            )
            return True
        await self._checkpoint(
            turn,
            step=StepWrite(
                run_id=turn.run_id,
                step_id=sid,
                seq=turn.seq + 1,
                node_id=node_id,
                edge="resumed",
                started_at=started,
                ended_at=self.hooks.clock(),
            ),
        )
        return False

    async def _record_offer(
        self, turn: _Turn, frame: Frame, sid: str, started: datetime, offer: str, *, edge: str
    ) -> None:
        """Put the offer to the customer and suspend on it."""
        turn.status = "waiting_customer"
        await self._checkpoint(
            turn,
            step=StepWrite(
                run_id=turn.run_id,
                step_id=sid,
                seq=turn.seq + 1,
                node_id=INTERRUPT_RETURN_NODE,
                edge=edge,
                started_at=started,
                ended_at=self.hooks.clock(),
            ),
            outbound=[*turn.take_notices(), offer],
            suspend_status="waiting_customer",
            suspend_node=INTERRUPT_RETURN_NODE,
            suspend_frame_seq=frame.frame_seq,
            suspend_detail={
                "node": INTERRUPT_RETURN_NODE,
                "kind": "interrupt_return",
                "graph": frame.graph_id,
                "prompt": offer,
            },
        )

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

        Returns whether the loop should continue. Two tiers, which is what 7.3 now says: the
        middle one - "otherwise the frame's ``on_error`` graph" - was recorded as unimplemented
        by three phases running, and phase 6's review (finding P7) asked for the argument to be
        finished rather than repeated. It was: DESIGN.md 7.3 is amended to two tiers, because
        nothing here can raise a frame-level error a node-level edge could not catch and the
        fall-through reaches a real packet carrying the failure's own reason.
        """
        on_error = getattr(node, "on_error", None)
        node_id = frame.node_id
        frame.errors[node_id] = frame.errors.get(node_id, 0) + 1
        cap = self.pack.manifest.limits.max_node_errors
        if isinstance(on_error, str) and frame.errors[node_id] >= cap:
            # The run-time half of the shape phase 4's resolution left open: an ``on_error`` edge
            # that leads back to its own node retries for as long as the node keeps failing, and
            # a WRITE tool that needs no approval repeats its side effect once per failure. The
            # count is consecutive and lives in the frame, so an ordinary loop is untouched and a
            # crash does not reset it. Named ``limit_exceeded`` because that is DESIGN.md section
            # 7.3's word for a limit, and the detail says which one.
            await self._handoff(
                turn,
                node_id,
                "limit_exceeded",
                f"{node_id} failed {frame.errors[node_id]} times in a row "
                f"(max_node_errors={cap}); the last failure was: {exc}",
                started=started,
                sid=sid,
            )
            return False
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
        """Give up on the turn and park the run for a human (DESIGN.md sections 7.3, 13).

        Every failure the engine can route ends here, with the reason it was given: the limits of
        7.3, a node error carrying its own diagnosis (``llm_unavailable``, ``tool_failed`` and
        the rest), a timeout, a pack the run no longer fits, a recovery that gave up. Phase 2
        made that one call to one hook; phase 6 makes the hook build a real packet, so the
        difference between a conversation that reached a ``handoff`` node and one that fell over
        on the way to it is the ``reason`` field and nothing else.

        The packet is delivered *before* the checkpoint that parks the run, for the reason given
        in :meth:`_advance`: the other order has a window in which the run says a human is
        needed and no human has been told, and nothing sweeps for that.

        **The customer is told too** (phase W review finding W6). The turn that raises the
        handoff used to end in complete silence: the reviewer sent a message that failed a node,
        and the run went to ``waiting_human`` with zero outbound messages - the socket carried a
        status change and nothing else. The *next* message on that conversation is answered
        (phase 6's finding P6), which made the silence look deliberate; it was not. A web chat
        client can at least render a status pill, and an email customer gets nothing at all after
        asking a question. The sentence is the same one a ``handoff`` node says, chosen by the
        same rule: what a human was actually told decides which of the two is true.
        """
        frame = turn.frame
        attempt = frame.attempts.get(node_id, 0)
        step = sid or step_id(turn.run_id, frame.frame_seq, node_id, attempt)
        frame.attempts[node_id] = attempt + 1
        turn.status = "waiting_human"
        now = self.hooks.clock()
        queued = await self._tell_a_human(turn, node_id, reason, detail, step)
        turn.notices.append(DEFAULT_HANDOFF_MESSAGE if queued else HANDOFF_UNDELIVERED_MESSAGE)
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
            outbound=turn.take_notices(),
            suspend_status="waiting_human",
            suspend_detail={"reason": reason, "detail": detail, "queued": queued},
            suspend_node=node_id,
            suspend_frame_seq=frame.frame_seq,
            handoff_reason=reason,
        )
        turn.outcome.handoff_reason = reason

    async def _tell_a_human(
        self,
        turn: _Turn,
        node_id: str,
        reason: str,
        detail: str | None,
        sid: str,
        *,
        next_steps: Sequence[str] = (),
    ) -> bool:
        """Hand the failure to the handoff hook. Returns whether a human was actually told.

        A hook that raises must not take the turn down with it: the run is about to be parked
        durably either way, and a customer waiting for a person who was never paged is recoverable
        from the queue, while a turn that died mid-checkpoint is another crash to re-enter. Core's
        own :class:`~support_core.handoff.service.HandoffService` never raises; this guard is for
        a pack's or a deployment's.

        The answer used to be "the hook did not raise", which is not the same fact: the service
        swallowed every sink failure, so a queue that was down still reported a page. It is now
        the hook's own answer, and a ``handoff`` node reads it before deciding what to promise
        the customer (review finding P2).
        """
        try:
            return bool(
                await self.hooks.handoff(
                    HandoffRequest(
                        conversation_id=turn.conversation_id,
                        run_id=turn.run_id,
                        reason=reason,
                        detail=detail,
                        frames=list(turn.frames),
                        node_id=node_id,
                        step_id=sid,
                        next_steps=list(next_steps),
                    )
                )
            )
        except Exception:
            return False

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
        # A workflow that is now running is no longer one the customer is still waiting to be
        # offered (DESIGN.md section 6.6 step 5). Cleared however the frame was pushed - by the
        # root graph choosing it from the surfaced list, by an interrupt, or by a gate redirect -
        # because what the record is for is that the request is not dropped, and it has not been.
        turn.secondary_intents = [
            intent for intent in turn.secondary_intents if intent.get("graph") != graph.id
        ]

    def _pop(self, turn: _Turn, frame: Frame, outputs: dict[str, Any]) -> None:
        """Pop a frame and map its outputs into the caller (DESIGN.md section 6.2, ``end``)."""
        turn.frames.pop()
        if not turn.frames:
            turn.status = "done"
            # The root frame has ended, so there is no root graph left to offer anything to. A
            # deferred intent that survived into the next conversation would be offered to a
            # customer who has moved on, out of a conversation they cannot see.
            turn.secondary_intents = []
            return
        caller = turn.frame
        clean = to_jsonable_python(outputs)
        for state_field, output_name in frame.outputs_into.items():
            caller.state[state_field] = clean.get(output_name)
        if frame.return_node is not None:
            caller.node_id = frame.return_node

    def _check_result(self, turn: _Turn, frame: Frame, node: NodeBase, result: NodeResult) -> None:
        """Everything a node's result must satisfy before the executor acts on any of it.

        Called inside the loop's ``try``, so a violation is routed like any other node failure
        (DESIGN.md section 7.3) instead of ending the turn. Two of the three checks are about a
        node claiming a power its *declared type* does not have, which is the only defence
        against a pack-registered node type (section 6.2) writing its own authorisation:

        * only a node the graph declares ``type: confirm`` may record an ``ActionApproval``;
        * only a node it declares ``type: tool`` may carry a tool's ``ctx.customer`` change out;
        * a sub-graph's outputs must fit the caller's state (phase-2 review finding R12).
        """
        if result.approval is not None and not isinstance(node, ConfirmNode):
            msg = (
                f"{frame.node_id}: only a 'confirm' node may record an ActionApproval, and this "
                f"is a {node.type!r} node (DESIGN.md section 8.2)"
            )
            raise NodeError(msg)
        if result.customer_patch is not None and not isinstance(node, ToolNode):
            msg = (
                f"{frame.node_id}: a {node.type!r} node tried to change ctx.customer; the "
                f"context is read-only to nodes (DESIGN.md section 6.1) and only a tool may ask "
                f"the engine to change it"
            )
            raise NodeError(msg)
        self._check_outputs(turn, frame, result)

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
        approval: ApprovalWrite | None = None,
        customer_context: dict[str, Any] | None = None,
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
            turn_tool_calls=turn.turn_tool_calls,
            updated_at=now,
            suspended_at=suspended_at,
            timeout_at=timeout_at,
            awaiting=awaiting,
            pack_fingerprint=self.pack.pin.fingerprint,
            secondary_intents=list(turn.secondary_intents),
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
                approval=approval,
                customer_context=customer_context,
                before_commit=before_commit,
            )
        turn.seq = step.seq
        turn.turn_nodes += 1
        turn.outcome.steps.append(step.step_id)
        turn.outcome.outbound.extend(outbound)
        await self.hooks.probe("after_checkpoint", {"step_id": step.step_id})
        if outbound:
            await self._flush_outbound(turn.conversation_id)

    async def deliver_pending(self, conversation_id: uuid.UUID) -> None:
        """Hand any undelivered outbound rows to the channel, outside a turn.

        The desk's ``reply`` (DESIGN.md section 13) is the caller: a person typing into a parked
        conversation writes a durable row and then wants it delivered, and that is exactly what
        the end of a turn does. Public because it is not a turn and must not take the lock.
        """
        await self._flush_outbound(conversation_id)

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
            await self.hooks.send(
                conversation_id,
                [OutboundMessage(text=m.text, author=_author(m.author)) for m in waiting],
            )
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
        turn.turn_tool_calls = 0
        async with self.sessions() as session, session.begin():
            # One transaction, not two: a crash between counting the turn and marking the run
            # running would count a turn that never started, and "every K turns" would drift.
            await repo.set_run_fields(
                session,
                turn.run_id,
                status="running",
                turn_nodes=0,
                turn_tool_calls=0,
                awaiting=self._with_turn_event(turn, None),
                timeout_at=None,
                suspended_at=None,
                frames=[frame.model_dump(mode="json") for frame in turn.frames],
                next_frame_seq=turn.next_frame_seq,
                updated_at=self.hooks.clock(),
            )
            await repo.count_turn(session, turn.conversation_id)

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
                    secondary_intents=list(turn.secondary_intents),
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
            turn_tool_calls=run.turn_tool_calls,
            outcome=outcome,
            secondary_intents=[dict(intent) for intent in run.secondary_intents],
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
