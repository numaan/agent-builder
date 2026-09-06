"""Every SQL statement the engine runs. Implements the storage half of DESIGN.md sections 7.1,
7.2 and 17.

The executor holds no authoritative state of its own: everything that decides what happens next
lives in ``conversation``, ``message`` and ``run``, and this module is the only place that reads
or writes it. That is what makes "a process that dies at any point resumes to the same outcome"
a property of the schema rather than of the executor's control flow.

:func:`write_checkpoint` is the one that matters. DESIGN.md section 7.1: "Every ``checkpoint``
writes the full frame stack and the trace step in one transaction." It writes three things -
the run row, the trace step, and the messages the node produced - and it writes them together,
so there is no state in which a step happened but the stack does not know it.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from support_core.storage.models import (
    ActionApproval,
    Conversation,
    Handoff,
    Message,
    Run,
    ToolCall,
    TraceStep,
)


@dataclass(slots=True)
class RunUpdate:
    """The durable execution state after one node (DESIGN.md section 7.1)."""

    run_id: uuid.UUID
    status: str
    frames: list[dict[str, Any]]
    checkpoint_seq: int
    next_frame_seq: int
    turn_nodes: int
    turn_tool_calls: int
    updated_at: datetime
    suspended_at: datetime | None = None
    timeout_at: datetime | None = None
    awaiting: dict[str, Any] | None = None
    pack_fingerprint: str | None = None
    secondary_intents: list[dict[str, Any]] = field(default_factory=list)
    """Workflows the customer asked for that a ``blocked_in`` graph would not stop for
    (DESIGN.md section 6.6). Rewritten by every checkpoint, like the frame stack, because it is
    changed by the same acts that change the stack."""


@dataclass(slots=True)
class TurnStart:
    """The run row the moment a turn begins (DESIGN.md section 7.1, before the first node).

    Written by :func:`claim_and_begin_turn` in the same transaction as the claim. It carries no
    ``checkpoint_seq``: starting a turn executes no node, so the last committed checkpoint is
    still the last committed checkpoint.
    """

    run_id: uuid.UUID
    frames: list[dict[str, Any]]
    next_frame_seq: int
    updated_at: datetime
    awaiting: dict[str, Any] | None = None
    pack_fingerprint: str | None = None
    status: str = "running"
    secondary_intents: list[dict[str, Any]] = field(default_factory=list)
    """The interrupt check runs before the claim (see :func:`claim_and_begin_turn`), so a
    secondary intent it recorded has to commit with the claim: a process that dies in between
    must not leave a customer's deferred request on the floor."""


@dataclass(slots=True)
class ApprovalWrite:
    """One ``action_approval`` row, as a ``confirm`` node's checkpoint writes it (8.2, 17)."""

    conversation_id: uuid.UUID
    run_id: uuid.UUID
    frame_seq: int
    node_id: str
    step_id: str
    tool: str
    args: dict[str, Any]
    args_hash: str
    approved_by: str
    approved_at: datetime


@dataclass(slots=True)
class ToolCallStart:
    """The ``tool_call`` row written *before* the tool runs (DESIGN.md sections 8.1, 17)."""

    idempotency_key: str
    run_id: uuid.UUID
    step_id: str
    node_id: str
    tool: str
    risk: str
    args: dict[str, Any]
    created_at: datetime
    status: str = "running"
    error: str | None = None
    """Set only for a ``refused`` row: an attempt that never got as far as running."""

    finished_at: datetime | None = None


@dataclass(slots=True)
class StepWrite:
    """One ``trace_step`` row (DESIGN.md sections 7.1, 17)."""

    run_id: uuid.UUID
    step_id: str
    seq: int
    node_id: str
    started_at: datetime
    ended_at: datetime
    edge: str | None = None
    state_patch: dict[str, Any] = field(default_factory=dict)
    llm_response: dict[str, Any] | None = None
    error: str | None = None


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Conversation | None:
    return await session.get(Conversation, conversation_id)


async def conversation_by_channel_key(
    session: AsyncSession, *, channel: str, channel_key: str
) -> Conversation | None:
    """The conversation a channel knows by its own name (DESIGN.md section 12).

    A web chat session key across a reconnect, a mail thread across days: the durable key, never
    a connection. ``uq_conversation_channel_key`` makes the answer unique per channel.
    """
    result = await session.execute(
        select(Conversation).where(
            Conversation.channel == channel, Conversation.channel_key == channel_key
        )
    )
    return result.scalar_one_or_none()


async def create_conversation(
    session: AsyncSession,
    *,
    channel: str,
    customer_ref: str | None = None,
    context: dict[str, Any] | None = None,
    channel_key: str | None = None,
) -> Conversation:
    conversation = Conversation(
        channel=channel,
        channel_key=channel_key,
        customer_ref=customer_ref,
        context=context or {},
        status="open",
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def close_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, *, when: datetime
) -> None:
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(status="closed", closed_at=when)
    )


async def load_run(session: AsyncSession, conversation_id: uuid.UUID) -> Run | None:
    """The conversation's run. There is at most one (``uq_run_conversation``)."""
    result = await session.execute(select(Run).where(Run.conversation_id == conversation_id))
    return result.scalar_one_or_none()


async def create_run(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    pack_version: str,
    pack_fingerprint: str,
) -> Run:
    run = Run(
        conversation_id=conversation_id,
        pack_version=pack_version,
        pack_fingerprint=pack_fingerprint,
        status="idle",
        frames=[],
        checkpoint_seq=0,
        turn_nodes=0,
        turn_tool_calls=0,
    )
    session.add(run)
    await session.flush()
    return run


async def set_run_fields(session: AsyncSession, run_id: uuid.UUID, **values: Any) -> None:
    """Update the run outside a checkpoint (turn start, timeout sweep, crash re-entry)."""
    await session.execute(update(Run).where(Run.id == run_id).values(**values))


async def enqueue_inbound(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    text_: str,
    author: str = "customer",
) -> Message:
    """Store an inbound message as ``pending`` before trying for the conversation lock.

    DESIGN.md section 17: "Inbound messages that arrive while locked are stored in ``message``
    with ``status = pending`` and processed in order when the lock frees." Writing the row
    *first*, always, is what makes the message durable no matter which process ends up
    processing it, and what gives the queue its order.

    The order is *claimed*, not measured (phase W review finding W4). The ``UPDATE ...
    RETURNING`` below takes the conversation's row lock, so two callers arriving at the same
    instant are serialised here and leave with distinct, increasing numbers; the queue is then
    drained in the order the rows were made durable. It used to be ordered by ``created_at`` -
    the transaction start timestamp, identical to the microsecond for a simultaneous pair - with
    a random UUID breaking the tie, and the reviewer reversed two messages that way and parked
    the conversation for good.

    The cost is that concurrent messages *on one conversation* serialise for the length of this
    transaction, which writes one row and commits. Messages on different conversations touch
    different rows and do not meet.
    """
    seq = await session.scalar(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(inbound_seq=Conversation.inbound_seq + 1)
        .returning(Conversation.inbound_seq)
    )
    message = Message(
        conversation_id=conversation_id,
        direction="inbound",
        author=author,
        text=text_,
        status="pending",
        queue_seq=seq,
    )
    session.add(message)
    await session.flush()
    return message


async def peek_next_pending(session: AsyncSession, conversation_id: uuid.UUID) -> Message | None:
    """The oldest pending inbound message, without claiming it.

    The engine reads the message before it claims it, because everything it must decide before
    the claim - whether this is a resume or a new root frame, and (from phase 6) what the
    interrupt check says - needs the text, and none of it may happen inside the transaction that
    claims. Safe under the conversation's advisory lock, which is the only thing that may claim.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "inbound",
            Message.status == "pending",
        )
        .order_by(Message.queue_seq, Message.created_at, Message.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def pending_inbound(
    session: AsyncSession, conversation_id: uuid.UUID, limit: int = 50
) -> list[Message]:
    """Every inbound message still waiting for a turn, oldest first (review finding P6).

    :func:`peek_next_pending` answers "what does the engine run next"; this answers "what has the
    customer said that nobody has looked at", which is a different question with one caller: the
    desk. A run parked ``waiting_human`` queues customer messages by design - resuming it early
    would drop the wait on the floor - and until this they were durable and invisible, which on
    email, where a customer *will* reply to "a specialist will pick this up", is a swallowed
    reply.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "inbound",
            Message.status == "pending",
        )
        .order_by(Message.queue_seq, Message.created_at, Message.id)
        .limit(limit)
    )
    return list(result.scalars())


async def claim_and_begin_turn(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    run: TurnStart,
    conversation_id: uuid.UUID | None = None,
    before_commit: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Claim one inbound message and start the turn that will consume it, in one transaction.

    This is the durable act that makes a customer message impossible to lose. Claiming marks
    the row ``received``, which takes it out of the pending queue for ever; the run row is the
    only place that can then say what it was claimed for. Doing the two in separate
    transactions leaves a window - the claim committed, the run still ``waiting_customer`` and
    unaware - in which a dead process loses the message with no way back, because a ``received``
    row is indistinguishable from one a node has already consumed (independent review finding
    R1). Committing both together means a crash either leaves the message pending, or leaves a
    ``running`` run carrying the event, and both are recoverable.

    Returns whether the message was still pending. ``False`` means somebody else claimed it and
    nothing was written; under the conversation's advisory lock that cannot happen, and the
    ``status = 'pending'`` predicate on the claim is defence in depth against the day it can.
    """
    async with session.begin():
        claimed = await session.execute(
            text(
                "UPDATE message SET status = 'received' "
                "WHERE id = :id AND status = 'pending' RETURNING id"
            ),
            {"id": message_id},
        )
        if claimed.first() is None:
            return False
        await session.execute(
            update(Run)
            .where(Run.id == run.run_id)
            .values(
                status=run.status,
                frames=run.frames,
                next_frame_seq=run.next_frame_seq,
                turn_nodes=0,
                turn_tool_calls=0,
                suspended_at=None,
                timeout_at=None,
                awaiting=run.awaiting,
                pack_fingerprint=run.pack_fingerprint,
                secondary_intents=run.secondary_intents,
                updated_at=run.updated_at,
            )
        )
        if conversation_id is not None:
            # The turn counter of DESIGN.md section 10 advances with the claim, not beside it.
            await count_turn(session, conversation_id)
        if before_commit is not None:
            await before_commit()
        return True


async def count_turn(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    """Record that a turn has begun and return the conversation's new turn count.

    DESIGN.md section 10 updates the conversation summary "every K turns", so K has to be
    counted somewhere that survives a crash. It is a column, incremented in the *same*
    transaction that starts the turn, for the reason phase 2's two must-fix findings were both
    about: a counter in memory is a counter that a dead process takes with it, and one written
    in its own transaction has a window where a turn is started but not counted.
    """
    result = await session.execute(
        text(
            "UPDATE conversation SET turn_count = turn_count + 1 "
            "WHERE id = :id RETURNING turn_count"
        ),
        {"id": conversation_id},
    )
    return int(result.scalar_one())


async def recent_messages(
    session: AsyncSession, conversation_id: uuid.UUID, limit: int = 12
) -> list[Message]:
    """The turn window of DESIGN.md section 10: the last ``limit`` messages, oldest first.

    Read from the ``message`` table rather than kept in memory, so the window a node sees after
    a crash is the window it saw before one. Only committed messages exist, which is exactly the
    set a re-executed step would have seen.
    """
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.status != "pending")
        .order_by(Message.created_at.desc(), Message.ordinal.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))


async def write_summary(
    session: AsyncSession, conversation_id: uuid.UUID, *, summary: str, at_turn: int
) -> None:
    """Store a rolling summary and the turn it covers (DESIGN.md section 10).

    ``summary_turn`` is what makes "every K turns" idempotent: a crash between writing the
    summary and the next turn simply leaves the pair consistent, and nothing a turn *depends on*
    is read from either column.
    """
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(summary=summary, summary_turn=at_turn)
    )


async def requeue(session: AsyncSession, message_id: uuid.UUID) -> None:
    """Put a claimed inbound message back on the queue.

    Used when a turn ends without the node that was waiting for the message ever seeing it -
    a gate re-check pushed a redirect that suspended, for instance. The row keeps its
    ``created_at``, so it is still the oldest pending message and order is preserved.
    """
    await session.execute(update(Message).where(Message.id == message_id).values(status="pending"))


async def forget_turn_event(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Drop the in-flight turn event from ``run.awaiting`` (see the executor's requeue)."""
    await session.execute(
        text("UPDATE run SET awaiting = awaiting - 'turn_event' WHERE id = :id"),
        {"id": run_id},
    )


async def count_recovery_attempt(session: AsyncSession, run_id: uuid.UUID) -> int:
    """Record one failed recovery pass over this run and return the new total (finding R3)."""
    result = await session.execute(
        text(
            "UPDATE run SET recovery_attempts = recovery_attempts + 1 "
            "WHERE id = :id RETURNING recovery_attempts"
        ),
        {"id": run_id},
    )
    return int(result.scalar_one())


async def write_checkpoint(
    session: AsyncSession,
    *,
    run: RunUpdate,
    step: StepWrite,
    conversation_id: uuid.UUID,
    outbound: Sequence[str] = (),
    approval: ApprovalWrite | None = None,
    customer_context: dict[str, Any] | None = None,
    before_commit: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """The checkpoint of DESIGN.md section 7.1, in one transaction.

    The frame stack, the trace step and the messages the node produced commit together or not
    at all. A process that dies before the commit leaves nothing behind and re-executes the
    node under the *same* step id; a process that dies after it finds the stack already
    advanced. There is no third state.

    Two things phase 4 adds to the same transaction, for the same reason:

    * ``approval`` - the ``ActionApproval`` a ``confirm`` node's "yes" produced. An approval
      that committed without its checkpoint would be an authorisation for a step the run does
      not believe happened; one that did not commit with it would be a confirmed action with no
      authorisation. Neither is a state this system should be able to reach.
    * ``customer_context`` - the ``ctx.customer`` a tool asked to change (DESIGN.md section 19
      step 9). The tool's own effect is already durable in its ``tool_call`` row; committing the
      context beside the step keeps a gate from being re-evaluated against a context the step
      that changed it never finished writing.

    ``before_commit`` runs inside the transaction, immediately before it commits. The engine
    passes the probe hook, which is how a test kills a turn mid-transaction.
    """
    async with session.begin():
        if approval is not None:
            await record_approval(session, approval)
        if customer_context is not None:
            await patch_customer_context(session, conversation_id, customer_context)
        await session.execute(
            update(Run)
            .where(Run.id == run.run_id)
            .values(
                status=run.status,
                frames=run.frames,
                checkpoint_seq=run.checkpoint_seq,
                next_frame_seq=run.next_frame_seq,
                turn_nodes=run.turn_nodes,
                turn_tool_calls=run.turn_tool_calls,
                suspended_at=run.suspended_at,
                timeout_at=run.timeout_at,
                awaiting=run.awaiting,
                pack_fingerprint=run.pack_fingerprint,
                secondary_intents=run.secondary_intents,
                updated_at=run.updated_at,
            )
        )
        session.add(
            TraceStep(
                run_id=step.run_id,
                step_id=step.step_id,
                seq=step.seq,
                node_id=step.node_id,
                edge=step.edge,
                state_patch=step.state_patch,
                llm_response=step.llm_response,
                error=step.error,
                started_at=step.started_at,
                ended_at=step.ended_at,
            )
        )
        for ordinal, body in enumerate(outbound):
            # created_at is left to the server default. It orders the customer's transcript,
            # and a conversation is single-writer, so successive checkpoints get increasing
            # transaction timestamps whichever process wrote them - which an engine clock,
            # injectable and therefore skewable, could not promise across a crash and a resume.
            # Within one checkpoint every row shares that timestamp, so `ordinal` is what
            # orders the messages one node produced (review finding R4).
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction="outbound",
                    author="agent",
                    text=body,
                    status="pending_send",
                    ordinal=ordinal,
                )
            )
        if before_commit is not None:
            await before_commit()


async def add_outbound(
    session: AsyncSession, *, conversation_id: uuid.UUID, text_: str, author: str = "agent"
) -> Message:
    """Write an outbound message from outside a turn (DESIGN.md sections 12, 13).

    The human desk's ``reply`` is the only caller. It is ``pending_send`` like any other outbound
    row, so it is delivered by the same code and appears in the same transcript - the difference
    is the author, which is what lets a reader tell a person's sentences from a model's.

    Deliberately not part of a checkpoint: nothing executed, so there is no step to record. A
    message written by a human while a run is parked is a fact about the conversation, not about
    the run.
    """
    message = Message(
        conversation_id=conversation_id,
        direction="outbound",
        author=author,
        text=text_,
        status="pending_send",
        ordinal=0,
    )
    session.add(message)
    await session.flush()
    return message


async def mark_sent(session: AsyncSession, message_ids: Sequence[uuid.UUID]) -> None:
    """Mark outbound rows delivered. Phase 7's channel adapters own the delivery itself."""
    if not message_ids:
        return
    await session.execute(update(Message).where(Message.id.in_(message_ids)).values(status="sent"))


async def pending_outbound(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    """Committed outbound messages nobody has delivered yet, claimed for this transaction.

    ``FOR UPDATE SKIP LOCKED`` is what makes "claimed" true (phase W review finding W5).
    :meth:`~support_core.engine.executor.Executor.deliver_pending` deliberately runs *outside*
    the conversation lock - a desk reply is not a turn and must not wait for one - so two
    transactions could read the same ``pending_send`` rows and hand both copies to the adapter
    before either marked them ``sent``. On web chat that is a duplicated bubble; on phase 7's
    email adapter it is a duplicated email, which is a customer complaint. Skipping locked rows
    rather than waiting for them is right here: a row another transaction is already delivering
    is a row this one has nothing to do about, and it will be ``sent`` by the time anyone looks
    again.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "outbound",
            Message.status == "pending_send",
        )
        .order_by(Message.created_at, Message.ordinal, Message.id)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars())


async def transcript(
    session: AsyncSession, conversation_id: uuid.UUID, limit: int = 200
) -> list[Message]:
    """The conversation as the customer saw it, oldest first, newest ``limit`` messages.

    Distinct from :func:`recent_messages`, which is the prompt window of DESIGN.md section 10
    and therefore excludes anything not committed into the conversation's history. This is what
    a channel shows a client that reconnected: it includes a customer message still ``pending``
    in the queue, because the customer typed it and would otherwise watch it disappear.

    An outbound row that has not been ``sent`` is **not** included (phase W review finding W10).
    Today that changes nothing, because delivery is the only thing that can leave a row
    ``pending_send``. It stops being nothing the moment phase 7 puts DESIGN.md section 14's
    outbound guardrail at send time: this path reads rows straight out of the table, so a
    reconnecting client would be shown the text the guardrail had just refused to deliver, and
    the phase's promise - a customer never sees a message a guardrail would have stopped - would
    hold for the push path and not for the reconnect path. Closing it now means the seam is
    already in the right place when the guardrail arrives.
    """
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            (Message.direction != "outbound") | (Message.status == "sent"),
        )
        .order_by(Message.created_at.desc(), Message.ordinal.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed(list(result.scalars())))


async def trace(session: AsyncSession, run_id: uuid.UUID) -> list[TraceStep]:
    """Every step of a run in execution order.

    Ordered by ``seq``, not by ``started_at``: two steps written in one transaction would share
    a server-side timestamp, which is why ``seq`` exists (phase 0 review finding F5).
    """
    result = await session.execute(
        select(TraceStep).where(TraceStep.run_id == run_id).order_by(TraceStep.seq)
    )
    return list(result.scalars())


async def stalled_runs(session: AsyncSession, cutoff: datetime, limit: int = 100) -> list[Run]:
    """Runs left ``running`` by a process that died before its turn finished."""
    result = await session.execute(
        select(Run)
        .where(Run.status == "running", Run.updated_at < cutoff)
        .order_by(Run.updated_at)
        .limit(limit)
    )
    return list(result.scalars())


# -- the tool runtime (DESIGN.md sections 8.1, 8.2, 17) ------------------------------------


async def record_approval(session: AsyncSession, approval: ApprovalWrite) -> uuid.UUID:
    """Store a customer's approval of one proposed action (DESIGN.md section 8.2).

    Idempotent under ``(run_id, step_id)``: a ``confirm`` node whose step re-executes after a
    crash records the same approval, not a second one that a later call could also spend.

    **A new proposal supersedes the frame's earlier live ones for the same node** (review finding
    P5). A ``confirm`` node can legitimately run twice in one frame - returning to a workflow
    parked by an interrupt re-enters at the node it was suspended in, and it re-presents its
    proposal rather than assuming the old answer - and each run wrote a live row, so the invariant
    "one live approval per proposal" quietly stopped holding. Nothing was over-authorised in the
    sample pack, because ``consume_approval`` spends exactly one and no edge returns to the tool
    node in the same frame; a graph whose ``on_error`` did return there would have found the spare
    waiting. The supersede is scoped by ``approved_by``, so the *human* half of a
    ``requires_human_approval`` pair does not cancel the customer half it was written to
    countersign; it is in the same statement batch as the insert, and therefore in the same
    checkpoint transaction, so no window exists in which both are live or neither is.
    """
    await session.execute(
        text(
            "UPDATE action_approval SET consumed_at = :approved_at "
            "WHERE run_id = :run_id AND frame_seq = :frame_seq AND node_id = :node_id "
            "  AND approved_by = :approved_by AND step_id <> :step_id "
            "  AND consumed_at IS NULL"
        ),
        {
            "run_id": approval.run_id,
            "frame_seq": approval.frame_seq,
            "node_id": approval.node_id,
            "approved_by": approval.approved_by,
            "step_id": approval.step_id,
            "approved_at": approval.approved_at,
        },
    )
    result = await session.execute(
        text(
            "INSERT INTO action_approval "
            "(conversation_id, run_id, frame_seq, node_id, step_id, tool, args, args_hash, "
            " approved_by, approved_at) "
            "VALUES (:conversation_id, :run_id, :frame_seq, :node_id, :step_id, :tool, "
            "        CAST(:args AS jsonb), :args_hash, :approved_by, :approved_at) "
            # DO NOTHING, spelled as a no-op update so RETURNING still yields the row
            # (review finding R5). The previous version updated `args_hash` and nothing else,
            # which could leave the hash describing one action and the `args` column - the one
            # a human reads instead of reversing a sha256 - describing another. A confirm step
            # re-executed under the same (run_id, step_id) computes the same proposal from the
            # same checkpointed state anyway, so there is nothing to update; and if it somehow
            # did not, the row that was written when the customer was asked is the honest one.
            "ON CONFLICT (run_id, step_id) DO UPDATE SET step_id = action_approval.step_id "
            "RETURNING id"
        ),
        {
            "conversation_id": approval.conversation_id,
            "run_id": approval.run_id,
            "frame_seq": approval.frame_seq,
            "node_id": approval.node_id,
            "step_id": approval.step_id,
            "tool": approval.tool,
            "args": json.dumps(approval.args),
            "args_hash": approval.args_hash,
            "approved_by": approval.approved_by,
            "approved_at": approval.approved_at,
        },
    )
    return uuid.UUID(str(result.scalar_one()))


async def consume_approval(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    frame_seq: int,
    node_id: str,
    tool: str,
    args_hash: str,
    approved_by: Sequence[str],
    now: datetime,
    tool_call_id: uuid.UUID | None = None,
) -> ActionApproval | None:
    """Take one live approval for this exact action, atomically, or return ``None``.

    Every clause is a defence and they are independent:

    * ``args_hash`` is DESIGN.md section 8.2's binding - a model that confirms one amount and
      calls with another does not match;
    * ``run_id`` and ``frame_seq`` bind the approval to the *invocation* it was given in.
      ``frame_seq`` is monotonic and never reused, so an approval from an earlier trip through
      the same graph cannot authorise a later one however identical the arguments;
    * ``node_id`` is the ``confirm`` node the tool node named in ``requires_approval``;
    * ``consumed_at IS NULL`` makes it single use (phase-0 review finding N1). The update is the
      claim: two callers racing cannot both win, because the row is locked by the first.

    ``FOR UPDATE SKIP LOCKED`` rather than plain ``FOR UPDATE``: a caller that would block on a
    row somebody else is spending should look at the next candidate, not wait for a decision it
    will lose anyway.
    """
    result = await session.execute(
        text(
            "UPDATE action_approval SET consumed_at = :now, "
            "  consumed_by_tool_call_id = :tool_call_id "
            "WHERE id = ("
            "  SELECT id FROM action_approval"
            "   WHERE conversation_id = :conversation_id AND run_id = :run_id"
            "     AND frame_seq = :frame_seq AND node_id = :node_id"
            "     AND tool = :tool AND args_hash = :args_hash"
            "     AND approved_by = ANY(:approved_by) AND consumed_at IS NULL"
            "   ORDER BY approved_at, id"
            "   FOR UPDATE SKIP LOCKED"
            "   LIMIT 1) "
            "RETURNING id"
        ),
        {
            "now": now,
            "tool_call_id": tool_call_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "frame_seq": frame_seq,
            "node_id": node_id,
            "tool": tool,
            "args_hash": args_hash,
            "approved_by": list(approved_by),
        },
    )
    row = result.first()
    if row is None:
        return None
    return await session.get(ActionApproval, row[0])


async def live_approvals(
    session: AsyncSession, conversation_id: uuid.UUID, *, tool: str | None = None
) -> list[ActionApproval]:
    """Unconsumed approvals in a conversation. For tests and, later, the desk view."""
    query = select(ActionApproval).where(
        ActionApproval.conversation_id == conversation_id,
        ActionApproval.consumed_at.is_(None),
    )
    if tool is not None:
        query = query.where(ActionApproval.tool == tool)
    result = await session.execute(query.order_by(ActionApproval.approved_at))
    return list(result.scalars())


async def get_tool_call(session: AsyncSession, idempotency_key: str) -> ToolCall | None:
    result = await session.execute(
        select(ToolCall).where(ToolCall.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


async def start_tool_call(session: AsyncSession, start: ToolCallStart) -> ToolCall:
    """Claim the idempotency key *before* the tool runs (DESIGN.md sections 7.1, 8.1).

    The row is what makes at-most-once possible: a process that dies between this insert and
    the result leaves a ``running`` row, which is a call whose outcome nobody knows. The
    alternative - write the row after the call - cannot distinguish "never ran" from "ran and
    we never heard", which for a refund is the whole question.
    """
    call = ToolCall(
        idempotency_key=start.idempotency_key,
        run_id=start.run_id,
        step_id=start.step_id,
        node_id=start.node_id,
        tool=start.tool,
        args=start.args,
        risk=start.risk,
        status=start.status,
        error=start.error,
        created_at=start.created_at,
        updated_at=start.created_at,
        finished_at=start.finished_at,
        attempts=1,
    )
    session.add(call)
    await session.flush()
    return call


async def finish_tool_call(
    session: AsyncSession,
    tool_call_id: uuid.UUID,
    *,
    status: str,
    when: datetime,
    result: dict[str, Any] | None = None,
    context_patch: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    await session.execute(
        update(ToolCall)
        .where(ToolCall.id == tool_call_id)
        .values(
            status=status,
            result=result,
            context_patch=context_patch,
            error=error,
            finished_at=when,
            updated_at=when,
        )
    )


async def reenter_tool_call(
    session: AsyncSession, tool_call_id: uuid.UUID, *, status: str, when: datetime
) -> None:
    """Record another pass over an existing key: a retry, or a refusal to retry."""
    await session.execute(
        update(ToolCall)
        .where(ToolCall.id == tool_call_id)
        .values(status=status, attempts=ToolCall.attempts + 1, updated_at=when)
    )


async def set_tool_call_approval(
    session: AsyncSession, tool_call_id: uuid.UUID, approval_id: uuid.UUID
) -> None:
    await session.execute(
        update(ToolCall).where(ToolCall.id == tool_call_id).values(approval_id=approval_id)
    )


async def tool_calls_for_run(session: AsyncSession, run_id: uuid.UUID) -> list[ToolCall]:
    """Every tool call of a run, in order. The join phase-0 finding F6 asked for."""
    result = await session.execute(
        select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at, ToolCall.id)
    )
    return list(result.scalars())


async def patch_customer_context(
    session: AsyncSession, conversation_id: uuid.UUID, customer: dict[str, Any]
) -> None:
    """Replace ``conversation.context->'customer'`` (DESIGN.md sections 10, 19 step 9).

    Surgical rather than a whole-document write: ``context`` also carries the entry graph's
    inputs and whatever a channel put there, none of which a tool has any business rewriting.
    """
    await session.execute(
        text(
            "UPDATE conversation "
            "SET context = jsonb_set(COALESCE(context, '{}'::jsonb), '{customer}', "
            "                        CAST(:customer AS jsonb), true) "
            "WHERE id = :id"
        ),
        {"customer": json.dumps(customer), "id": conversation_id},
    )


# -- human handoff (DESIGN.md section 13) --------------------------------------------------


@dataclass(slots=True)
class HandoffWrite:
    """One ``handoff`` row, as the Postgres queue sink writes it (DESIGN.md section 13)."""

    conversation_id: uuid.UUID
    run_id: uuid.UUID | None
    packet: dict[str, Any]
    queue: str
    reason: str
    graph_id: str | None = None
    node_id: str | None = None
    step_id: str | None = None
    sla_due_at: datetime | None = None


async def create_handoff(session: AsyncSession, write: HandoffWrite) -> Handoff:
    """Put a packet on a queue. The row *is* the queue (DESIGN.md section 13's Postgres sink).

    Idempotent under ``(run_id, step_id)`` where the caller has a step: the packet is built and
    delivered *before* the checkpoint that records the step commits, so a process that dies in
    that window re-executes the node - and a person must not be paged twice for one conversation.
    A handoff with no step (a timeout sweep, a recovery that gave up) is inserted every time,
    because there is nothing to say it is the same one.
    """
    if write.run_id is not None and write.step_id is not None:
        existing = await session.execute(
            select(Handoff).where(Handoff.run_id == write.run_id, Handoff.step_id == write.step_id)
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            return found
    row = Handoff(
        conversation_id=write.conversation_id,
        run_id=write.run_id,
        packet=write.packet,
        queue=write.queue,
        reason=write.reason,
        graph_id=write.graph_id,
        node_id=write.node_id,
        step_id=write.step_id,
        sla_due_at=write.sla_due_at,
        status="open",
    )
    session.add(row)
    await session.flush()
    return row


async def get_handoff(session: AsyncSession, handoff_id: uuid.UUID) -> Handoff | None:
    return await session.get(Handoff, handoff_id)


async def list_handoffs(
    session: AsyncSession,
    *,
    queue: str | None = None,
    status: str | None = "open",
    conversation_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[Handoff]:
    """The desk's queue view (DESIGN.md section 13), oldest first so the SLA is honoured."""
    query = select(Handoff)
    if queue is not None:
        query = query.where(Handoff.queue == queue)
    if status is not None:
        query = query.where(Handoff.status == status)
    if conversation_id is not None:
        query = query.where(Handoff.conversation_id == conversation_id)
    result = await session.execute(query.order_by(Handoff.created_at, Handoff.id).limit(limit))
    return list(result.scalars())


async def open_handoff_for(session: AsyncSession, conversation_id: uuid.UUID) -> Handoff | None:
    """The newest open handoff on a conversation, which is the one a desk action means."""
    result = await session.execute(
        select(Handoff)
        .where(Handoff.conversation_id == conversation_id, Handoff.status == "open")
        .order_by(Handoff.created_at.desc(), Handoff.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def resolve_handoff(
    session: AsyncSession,
    handoff_id: uuid.UUID,
    *,
    status: str,
    when: datetime,
    human_id: str | None = None,
) -> None:
    """Mark a queued packet dealt with: ``resumed`` (handed back) or ``closed`` (taken over)."""
    values: dict[str, Any] = {"status": status, "resolved_at": when, "updated_at": when}
    if human_id is not None:
        values["human_id"] = human_id
    await session.execute(update(Handoff).where(Handoff.id == handoff_id).values(**values))


async def record_human_approval(
    session: AsyncSession, *, template: ActionApproval, now: datetime
) -> uuid.UUID:
    """Write the *human* half of a ``requires_human_approval`` pair (DESIGN.md section 8.2).

    Phase 4 built the check and left it unsatisfiable on purpose: the runtime consumes a customer
    approval and then a second row with ``approved_by = 'human'``, and until this desk existed
    nothing could write one, so a tool that declared the flag always refused. This is the only
    thing in the system that writes one, and it copies every binding field from the customer's own
    row rather than taking them from a request - the desk approves *the action that was proposed*,
    and a desk that could name its own tool, arguments or frame would be a way to authorise
    something the customer never saw.

    The row it mirrors must be bound to a run, a frame and a confirm node, because those are what
    :func:`consume_approval` matches on; an unbound row could never be consumed and writing its
    twin would only look like an approval.
    """
    if template.run_id is None or template.frame_seq is None or not template.node_id:
        msg = (
            "the customer approval to mirror is not bound to a run, a frame and a confirm node, "
            "so a human approval of it could never be consumed"
        )
        raise ValueError(msg)
    return await record_approval(
        session,
        ApprovalWrite(
            conversation_id=template.conversation_id,
            run_id=template.run_id,
            frame_seq=template.frame_seq,
            node_id=template.node_id,
            # A distinct step id: ``uq_action_approval_run_step`` is what stops a re-executed
            # confirm node recording a second approval, and the human's row is a second row on
            # purpose. It is not a step any node ran, so it is named after the one it mirrors.
            step_id=f"{template.step_id}#human:{uuid.uuid4().hex[:8]}",
            tool=template.tool,
            args=dict(template.args or {}),
            args_hash=template.args_hash,
            approved_by="human",
            approved_at=now,
        ),
    )


async def due_runs(session: AsyncSession, now: datetime, limit: int = 100) -> list[Run]:
    """Suspended runs whose per-status timeout has expired (DESIGN.md section 7.2)."""
    result = await session.execute(
        select(Run)
        .where(Run.timeout_at.is_not(None), Run.timeout_at <= now)
        .order_by(Run.timeout_at)
        .limit(limit)
    )
    return list(result.scalars())
