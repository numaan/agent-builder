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

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from support_core.storage.models import Conversation, Message, Run, TraceStep


@dataclass(slots=True)
class RunUpdate:
    """The durable execution state after one node (DESIGN.md section 7.1)."""

    run_id: uuid.UUID
    status: str
    frames: list[dict[str, Any]]
    checkpoint_seq: int
    next_frame_seq: int
    turn_nodes: int
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


async def create_conversation(
    session: AsyncSession,
    *,
    channel: str,
    customer_ref: str | None = None,
    context: dict[str, Any] | None = None,
) -> Conversation:
    conversation = Conversation(
        channel=channel, customer_ref=customer_ref, context=context or {}, status="open"
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
                suspended_at=None,
                timeout_at=None,
                awaiting=run.awaiting,
                pack_fingerprint=run.pack_fingerprint,
                updated_at=run.updated_at,
            )
        )
        if before_commit is not None:
            await before_commit()
        return True


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


async def pending_count(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM message WHERE conversation_id = :c AND status = 'pending'"),
        {"c": conversation_id},
    )
    return int(result.scalar_one())


async def write_checkpoint(
    session: AsyncSession,
    *,
    run: RunUpdate,
    step: StepWrite,
    conversation_id: uuid.UUID,
    outbound: Sequence[str] = (),
    before_commit: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """The checkpoint of DESIGN.md section 7.1, in one transaction.

    The frame stack, the trace step and the messages the node produced commit together or not
    at all. A process that dies before the commit leaves nothing behind and re-executes the
    node under the *same* step id; a process that dies after it finds the stack already
    advanced. There is no third state.

    ``before_commit`` runs inside the transaction, immediately before it commits. The engine
    passes the probe hook, which is how a test kills a turn mid-transaction.
    """
    async with session.begin():
        await session.execute(
            update(Run)
            .where(Run.id == run.run_id)
            .values(
                status=run.status,
                frames=run.frames,
                checkpoint_seq=run.checkpoint_seq,
                next_frame_seq=run.next_frame_seq,
                turn_nodes=run.turn_nodes,
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
        for body in outbound:
            # created_at is left to the server default. It orders the customer's transcript,
            # and a conversation is single-writer, so successive checkpoints get increasing
            # transaction timestamps whichever process wrote them - which an engine clock,
            # injectable and therefore skewable, could not promise across a crash and a resume.
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction="outbound",
                    author="agent",
                    text=body,
                    status="pending_send",
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
        .order_by(Message.created_at, Message.id)
    )
    return list(result.scalars())


async def trace(session: AsyncSession, run_id: uuid.UUID) -> list[TraceStep]:
    """Every step of a run in execution order.

    Ordered by ``seq``, not by ``started_at``: two steps written in one transaction would share
    a server-side timestamp, which is why ``seq`` exists (phase 0 review finding F5).
    """
    result = await session.execute(
        select(TraceStep).where(TraceStep.run_id == run_id).order_by(TraceStep.seq)
    )
    return list(result.scalars())


async def step_exists(session: AsyncSession, step_id: str) -> bool:
    result = await session.execute(select(TraceStep.id).where(TraceStep.step_id == step_id))
    return result.first() is not None


async def stalled_runs(session: AsyncSession, cutoff: datetime, limit: int = 100) -> list[Run]:
    """Runs left ``running`` by a process that died before its turn finished."""
    result = await session.execute(
        select(Run)
        .where(Run.status == "running", Run.updated_at < cutoff)
        .order_by(Run.updated_at)
        .limit(limit)
    )
    return list(result.scalars())


async def due_runs(session: AsyncSession, now: datetime, limit: int = 100) -> list[Run]:
    """Suspended runs whose per-status timeout has expired (DESIGN.md section 7.2)."""
    result = await session.execute(
        select(Run)
        .where(Run.timeout_at.is_not(None), Run.timeout_at <= now)
        .order_by(Run.timeout_at)
        .limit(limit)
    )
    return list(result.scalars())
