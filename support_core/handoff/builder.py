"""Building a packet out of durable state. Implements DESIGN.md section 13.

Everything in a :class:`~support_core.handoff.packet.HandoffPacket` is read back from the
database rather than accumulated as the conversation runs, for the reason phase 2 spent a whole
review on: a handoff often happens because a process died, and a packet assembled from what this
process happens to remember would be least complete exactly when it matters most. The frame
stack, the tool ledger, the approvals and the transcript are all rows; this module joins them.

The summary is the one part a model writes, and DESIGN.md section 5.1 says which model: the
pack's ``escalation_model``. It is also the one part that is allowed to fail. A summariser that
is down must not stop a handoff - the customer is already waiting for a person - so the fallback
is a factual line built from the packet's own fields, and the packet says which it got.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from support_core.graph.context import ConversationContext
from support_core.handoff.packet import ActionRecord, CustomerRef, HandoffPacket, Passage
from support_core.storage import repositories as repo
from support_core.storage.models import ActionApproval, ToolCall
from support_core.tools.risk import Risk

if TYPE_CHECKING:  # pragma: no cover - a type-only import, to keep one import edge one way
    # ``support_core.engine`` imports this package (the executor is what raises a handoff), so
    # importing the engine back at run time would be a cycle: the engine's ``__init__`` would be
    # half-built when this module asked it for a name. A frame is used here only for its shape.
    from support_core.engine.types import Frame

SIDE_EFFECTING = frozenset({Risk.WRITE.value, Risk.HIGH.value})
"""The tiers ``actions_taken`` lists (DESIGN.md section 13: "every WRITE/HIGH tool call this
conversation"). READ calls are left out on purpose: a human taking over needs to see what was
*changed*, and twenty lookups around one refund hide the refund."""

TRANSCRIPT_IN_PACKET = 40
"""Messages the summary is written from. Not the whole conversation: the packet carries a
``transcript_url`` for that, and a summariser given four hundred turns writes a worse summary
than one given the last forty."""

Gathered = tuple[
    list["ActionRecord"], "ActionRecord | None", "ConversationContext | None", list[tuple[str, str]]
]
"""What :func:`gather` reads back: the ledger, the pending action, the context, the talk."""

UNFINISHED = frozenset({"running", "awaiting_callback", "indeterminate"})
"""Statuses that mean "this may or may not have happened". Exactly what a second person must not
repeat, so a call in one of them becomes the packet's ``pending_action``."""


@dataclass(slots=True)
class PacketRequest:
    """What the engine knows at the moment it gives up, or a ``handoff`` node fires."""

    conversation_id: uuid.UUID
    run_id: uuid.UUID | None
    reason: str
    detail: str | None = None
    frames: "Sequence[Frame]" = ()
    node_id: str | None = None
    """Where it stopped, when the caller knows better than the top frame does - a node that
    failed has not moved the stack yet, so the two agree, but a timeout sweep has no frame."""

    step_id: str | None = None
    suggested_next_steps: Sequence[str] = ()
    citations: Sequence[Passage] = ()
    """The seam for phase 5 (see :mod:`support_core.handoff.packet`). Always empty today."""

    extra_state: dict[str, Any] = field(default_factory=dict)


def _action(call: ToolCall) -> ActionRecord:
    return ActionRecord(
        tool=call.tool,
        risk=call.risk,
        status=call.status,
        args=dict(call.args or {}),
        result=dict(call.result) if isinstance(call.result, dict) else None,
        error=call.error,
        node_id=call.node_id,
        idempotency_key=call.idempotency_key,
        at=call.finished_at or call.created_at,
    )


def _proposed(approval: ActionApproval) -> ActionRecord:
    """An approved action that has not been called: the customer said yes and nothing ran."""
    return ActionRecord(
        tool=approval.tool,
        risk="unknown",
        status="proposed",
        args=dict(approval.args or {}),
        node_id=approval.node_id,
        approved_by=approval.approved_by,
        at=approval.approved_at,
    )


def fallback_summary(
    packet_reason: str, detail: str | None, actions: Sequence[ActionRecord]
) -> str:
    """What the packet says when no model wrote its summary.

    Facts only, and visibly not a narrative, so nobody mistakes it for one. It names the reason,
    the engine's own detail, and what was done to the account - which is the minimum a person
    needs before they open the transcript.
    """
    lines = [f"No model summary was available. Reason: {packet_reason}."]
    if detail:
        lines.append(f"Engine detail: {detail}")
    if actions:
        lines.append("Actions already taken: " + "; ".join(a.one_line() for a in actions))
    else:
        lines.append("No write or high-risk tool call has been made in this conversation.")
    return " ".join(lines)


def default_next_steps(reason: str, pending: ActionRecord | None) -> list[str]:
    """A short, honest checklist keyed on why the conversation arrived (DESIGN.md section 13).

    Core writes these rather than a model, because a suggestion is a claim about what this system
    can do, and a model that invents "issue a goodwill credit" is describing a workflow the pack
    does not have. A pack that wants richer ones puts them on its ``handoff`` node.
    """
    steps: list[str] = []
    if pending is not None and pending.status == "proposed":
        steps.append(
            f"An approved {pending.tool} has not run. Decide whether to run it or to tell the "
            f"customer it will not happen; do not assume it did."
        )
    elif pending is not None:
        steps.append(
            f"{pending.tool} is {pending.status}: confirm with the system of record whether it "
            f"took effect before doing anything that would repeat it."
        )
    match reason:
        case "llm_unavailable" | "engine_error":
            steps.append("The failure is ours, not the customer's. Answer them directly.")
        case "low_confidence" | "llm_invalid_output" | "model_requested_handoff":
            steps.append("The agent could not read what the customer wanted. Ask them.")
        case "tool_failed" | "tool_refused":
            steps.append("Check the system of record before retrying the action by hand.")
        case "limit_exceeded":
            steps.append("The conversation went round in circles. Read the last few turns first.")
        case "timeout":
            steps.append("The customer stopped replying. Decide whether to follow up or close.")
        case "pack_incompatible":
            steps.append("The workflow changed under a live conversation. This needs an engineer.")
        case _:
            steps.append("Read the summary, then the transcript if it is not enough.")
    steps.append("Reply here, hand the conversation back with `resume`, or `close` it.")
    return steps


async def gather(sessions: Any, request: PacketRequest) -> "Gathered":
    """Read the durable half of a packet: the ledger, the pending action, the context, the talk."""
    actions: list[ActionRecord] = []
    pending: ActionRecord | None = None
    context: ConversationContext | None = None
    transcript: list[tuple[str, str]] = []
    async with sessions() as session, session.begin():
        conversation = await repo.get_conversation(session, request.conversation_id)
        if conversation is not None:
            raw = dict(conversation.context or {})
            raw.pop("inputs", None)
            raw["conversation_id"] = str(conversation.id)
            raw["summary"] = conversation.summary
            customer = dict(raw.get("customer") or {})
            customer.setdefault("ref", conversation.customer_ref)
            raw["customer"] = customer
            try:
                context = ConversationContext.model_validate(raw)
            except Exception:
                # A packet is built when things have already gone wrong; a context that will not
                # validate is one more fact for the human, not a reason to deliver nothing.
                context = None
            rows = await repo.transcript(session, conversation.id, TRANSCRIPT_IN_PACKET)
            transcript = [(row.author, row.text) for row in rows]
        if request.run_id is not None:
            calls = await repo.tool_calls_for_run(session, request.run_id)
            actions = [_action(call) for call in calls if call.risk in SIDE_EFFECTING]
            unfinished = [a for a in actions if a.status in UNFINISHED]
            pending = unfinished[-1] if unfinished else None
            if pending is None:
                live = await repo.live_approvals(session, request.conversation_id)
                if live:
                    pending = _proposed(live[-1])
    return actions, pending, context, transcript


def assemble(
    request: PacketRequest,
    *,
    actions: Sequence[ActionRecord],
    pending: ActionRecord | None,
    context: ConversationContext | None,
    summary: str,
    queue: str,
    sla_minutes: int | None,
    transcript_url: str,
    now: datetime,
) -> HandoffPacket:
    """Put the packet together. Pure: everything it needs has already been read."""
    frames = list(request.frames)
    top = frames[-1] if frames else None
    customer = context.customer if context is not None else None
    steps = list(request.suggested_next_steps) or default_next_steps(request.reason, pending)
    return HandoffPacket(
        reason=request.reason,
        summary=summary,
        identity_verified=bool(customer.identity_verified) if customer is not None else False,
        customer=CustomerRef(
            ref=customer.ref if customer else None,
            name=customer.name if customer else None,
            email=customer.email if customer else None,
            locale=customer.locale if customer else "en",
        ),
        workflow=top.graph_id if top is not None else "",
        node=request.node_id or (top.node_id if top is not None else ""),
        state_snapshot=dict(top.state) if top is not None else dict(request.extra_state),
        actions_taken=list(actions),
        pending_action=pending,
        suggested_next_steps=steps,
        citations=list(request.citations),
        transcript_url=transcript_url,
        conversation_id=request.conversation_id,
        run_id=request.run_id,
        step_id=request.step_id,
        queue=queue,
        sla_minutes=sla_minutes,
        detail=request.detail,
        frames=[frame.model_dump(mode="json") for frame in frames],
        created_at=now,
    )
