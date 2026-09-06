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
    """
    message = Message(
        conversation_id=conversation_id,
        direction="inbound",
        author=author,
        text=text_,
        status="pending",
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
        .order_by(Message.created_at, Message.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


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


async def mark_sent(session: AsyncSession, message_ids: Sequence[uuid.UUID]) -> None:
    """Mark outbound rows delivered. Phase 7's channel adapters own the delivery itself."""
    if not message_ids:
        return
    await session.execute(update(Message).where(Message.id.in_(message_ids)).values(status="sent"))


async def pending_outbound(session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "outbound",
            Message.status == "pending_send",
        )
        .order_by(Message.created_at, Message.ordinal, Message.id)
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
    """
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
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
    """
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


async def due_runs(session: AsyncSession, now: datetime, limit: int = 100) -> list[Run]:
    """Suspended runs whose per-status timeout has expired (DESIGN.md section 7.2)."""
    result = await session.execute(
        select(Run)
        .where(Run.timeout_at.is_not(None), Run.timeout_at <= now)
        .order_by(Run.timeout_at)
        .limit(limit)
    )
    return list(result.scalars())
