"""The handoff packet. Implements DESIGN.md section 13's ``HandoffPacket`` field for field.

    The `handoff` node builds a `HandoffPacket` ... The packet is pushed to the queue named in
    `pack.yaml` through a `HandoffSink`. - DESIGN.md section 13

The point of a packet, as opposed to a transcript, is that a human should be able to act without
reading the conversation. So every field here answers a question a person picking this up asks in
the first thirty seconds: *why is this mine* (``reason``), *what happened* (``summary``), *do I
know who this is* (``identity_verified``, ``customer``), *where did it stop*
(``workflow``/``node``/``state_snapshot``), *what has already been done to this account*
(``actions_taken``), *is something half-done* (``pending_action``), *what should I do*
(``suggested_next_steps``) - and only then, if they want it, *what was said*
(``transcript_url``).

Two fields are deliberately narrow.

``actions_taken`` is every WRITE and HIGH tool call of the conversation, and nothing else. READ
calls are not omitted to save space: a human taking over needs to know what was *changed*, and a
list that mixes twenty lookups with one refund buries the refund.

``citations`` is DESIGN.md section 13's field and is always empty here, because retrieval is
phase 5 and is deferred. It is on the model rather than left out so that phase 5 fills a shape
rather than inventing one: :func:`support_core.handoff.builder.build_packet` takes ``citations``
and puts them here, and the day a retriever exists the summary can be grounded in the same
passages the customer was answered from. An empty list is the honest value for a system that
retrieved nothing.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CustomerRef(BaseModel):
    """Who the conversation is with, as much as is known (DESIGN.md sections 10, 13)."""

    model_config = ConfigDict(extra="forbid")

    ref: str | None = None
    name: str | None = None
    email: str | None = None
    locale: str = "en"


class ActionRecord(BaseModel):
    """One thing the agent did, or is about to do, to the customer's account.

    Built from a ``tool_call`` row (DESIGN.md section 17), which is the durable record of every
    invocation, so a packet cannot claim an action the ledger does not have.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    risk: str
    status: str
    """``succeeded``, ``failed``, ``running``, ``indeterminate``, ``refused``,
    ``awaiting_callback`` - or ``proposed`` for a :attr:`HandoffPacket.pending_action`, which has
    no row yet because it has not been called."""

    args: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    node_id: str | None = None
    idempotency_key: str | None = None
    approved_by: str | None = None
    at: datetime | None = None

    def one_line(self) -> str:
        """A human-readable line, for the summary prompt and for a desk that shows a list."""
        where = f" at {self.node_id}" if self.node_id else ""
        failed = f" ({self.error})" if self.error else ""
        return f"{self.tool} [{self.risk}] {self.status}{where}{failed}"


class Passage(BaseModel):
    """A knowledge passage a claim rests on (DESIGN.md sections 9.2, 13).

    The seam for phase 5. Nothing produces one yet and :attr:`HandoffPacket.citations` is
    therefore always empty; the shape is here so a retriever fills it rather than redefining it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str | None = None
    version: str | None = None
    text: str | None = None


class HandoffPacket(BaseModel):
    """DESIGN.md section 13, verbatim in its fields and additive in three of them.

    ``conversation_id``, ``run_id`` and ``created_at`` are not in the design's class. They are
    here because a packet is delivered to something outside this process - a webhook, a queue
    row a desk reads back hours later - and a payload that cannot say which conversation it is
    about is not actionable. ``queue`` and ``sla_minutes`` come from ``pack.yaml`` (section 5.1)
    for the same reason: the sink that receives it needs to know where it was addressed.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    summary: str
    """Written by the escalation model (DESIGN.md sections 5.1, 13). Falls back to a plain
    factual line assembled from the packet's own fields when no model is configured or the model
    call fails: a handoff that cannot happen because a summariser is down is the worst possible
    trade, and a human would rather have the facts than nothing."""

    identity_verified: bool
    customer: CustomerRef
    workflow: str
    """The graph the conversation stopped in."""

    node: str
    state_snapshot: dict[str, Any] = Field(default_factory=dict)
    """The stopped frame's state. The whole frame stack is in ``frames`` for an engineer; this is
    the one frame a human needs."""

    actions_taken: list[ActionRecord] = Field(default_factory=list)
    pending_action: ActionRecord | None = None
    """An action that was proposed and approved but has not run, or one that ran and left an
    unknown outcome. This is the field that stops two people refunding the same charge."""

    suggested_next_steps: list[str] = Field(default_factory=list)
    citations: list[Passage] = Field(default_factory=list)
    transcript_url: str

    conversation_id: uuid.UUID
    run_id: uuid.UUID | None = None
    step_id: str | None = None
    """The step that raised this handoff (DESIGN.md section 7.1's deterministic step id), where
    one did. It is what makes delivery idempotent: the packet is built before the checkpoint that
    records the step commits, and a re-executed node must not queue a second packet."""

    queue: str = ""
    sla_minutes: int | None = None
    detail: str | None = None
    """The engine's own words about the failure, where there are any: the exception text of a
    node error, the limit that was hit. Not for the customer, and not the summary."""

    frames: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None
