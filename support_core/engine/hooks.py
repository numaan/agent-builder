"""The seams the executor leaves for later phases. Implements the hook points DESIGN.md
sections 7.1 to 7.3 name but whose implementations belong to phases 3, 4, 6 and 7.

Every hook has a default that does the least surprising thing and says so, so that phase 2 can
run whole conversations end to end without pretending to have an LLM, a tool runtime or a
channel. Nothing here calls a model, executes a tool, or sends anything: PLAN.md's standing
rule (no WRITE or HIGH tool without an ``ActionApproval``) holds trivially in phase 2 because
no code path invokes a tool at all.

Replacing a hook is the supported way for a later phase to arrive:

============================  ===========  ==================================================
hook                          owner phase  default
============================  ===========  ==================================================
``interrupt_check``           6            ``continue`` - resume the suspended node
``extract_slots``             3            the whole reply fills the first declared slot
``confirm_decision``          4            an explicit yes or no word, else ``unclear``
``handoff``                   6            record nothing; the run still suspends for a human
``send``                      7            do not deliver; rows stay ``pending_send``
``summarize``                 3            no summary; ``conversation.summary`` is left alone
``probe``                     -            nothing (tests use it to kill a turn mid-flight)
============================  ===========  ==================================================

Phase 3 replaces the *default* of ``extract_slots`` rather than the ``ask`` node, which is what
reviews/phase-2.md asked for, and adds ``summarize`` for DESIGN.md section 10's rolling summary.
``SlotRequest`` is why the extractor's signature grew: structured extraction (DESIGN.md section
6.2) needs the declared *type* of each slot, and a list of names cannot carry one.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from support_core.engine.types import Frame, OutboundMessage
from support_core.graph.context import ConversationContext


class InterruptDecision(BaseModel):
    """The result of the interrupt check of DESIGN.md section 6.6."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["continue", "new_intent", "cancel", "unclear"] = "continue"
    graph: str | None = None
    """The workflow to push, for ``new_intent``."""


class HandoffRequest(BaseModel):
    """Everything the engine knows when it gives up on a turn (DESIGN.md sections 7.3, 13)."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    run_id: uuid.UUID
    reason: str
    """``limit_exceeded``, ``node_error``, ``timeout`` or ``pack_incompatible`` in phase 2;
    phase 6 adds ``llm_unavailable`` and the rest of section 7.3."""

    detail: str | None = None
    frames: list[Frame] = Field(default_factory=list)


class InterruptCheck(Protocol):
    async def __call__(
        self, ctx: ConversationContext, message: str, frames: Sequence[Frame]
    ) -> InterruptDecision: ...


class SlotRequest(BaseModel):
    """Everything an ``ask`` node knows when the customer replies (DESIGN.md section 6.2)."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    node_id: str
    graph_id: str
    slots: list[str]
    prompt: str
    """The question the node asked, so the extractor knows what the reply is answering."""

    reply: str
    state_model: type[BaseModel]
    """The frame's state model, which carries each slot's declared type. A name-only signature
    cannot express ``amount: float | None``, and an extractor that does not know the type can
    only guess at it."""

    state: dict[str, Any] = Field(default_factory=dict)
    window: list[tuple[str, str]] = Field(default_factory=list)
    """``(author, text)`` for the recent messages before the reply, oldest first: what the
    customer is answering may only be readable in the light of what came before it."""

    ctx: ConversationContext


class SlotExtractor(Protocol):
    async def __call__(self, request: SlotRequest) -> dict[str, Any]: ...


class ConfirmRequest(BaseModel):
    """A customer's reply to a proposed action (DESIGN.md sections 6.2, 8.2)."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    graph_id: str
    prompt: str
    """The proposal exactly as it was shown to the customer."""

    reply: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    """The canonical arguments of the action, so a classifier can see what was proposed."""

    window: list[tuple[str, str]] = Field(default_factory=list)
    ctx: ConversationContext


class ConfirmDecision(BaseModel):
    """Yes, no, or neither. There is no fourth answer and no default."""

    model_config = ConfigDict(extra="forbid")

    answer: Literal["yes", "no", "unclear"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ConfirmChecker(Protocol):
    async def __call__(self, request: ConfirmRequest) -> ConfirmDecision: ...


AFFIRMATIVE: frozenset[str] = frozenset(
    {
        "yes",
        "yes please",
        "yep",
        "yeah",
        "y",
        "ok",
        "okay",
        "sure",
        "please",
        "please do",
        "go ahead",
        "do it",
        "confirm",
        "confirmed",
        "approved",
        "proceed",
    }
)
NEGATIVE: frozenset[str] = frozenset(
    {"no", "nope", "n", "cancel", "stop", "do not", "don't", "dont", "no thanks", "not yet"}
)


async def keyword_confirm(request: ConfirmRequest) -> ConfirmDecision:
    """The default reading of a confirm reply: an explicit word, or ``unclear``.

    DESIGN.md section 6.2 requires "an explicit yes", so this matches the *whole* reply against a
    closed list rather than looking for a "yes" somewhere inside it - "no, not the yes one"
    contains one. Anything else is ``unclear``, which the ``confirm`` node turns into asking
    again rather than into a decision either way: guessing "no" throws away a customer's
    intention and guessing "yes" moves their money.

    :class:`~support_core.llm.wiring.StructuredConfirmClassifier` replaces it wherever a provider
    is configured. This stays because it is what lets the engine's own tests run without a model,
    and because a pack that wants a deterministic confirmation can keep it.
    """
    reply = " ".join(request.reply.lower().strip().strip(".!,").split())
    if reply in AFFIRMATIVE:
        return ConfirmDecision(answer="yes")
    if reply in NEGATIVE:
        return ConfirmDecision(answer="no")
    return ConfirmDecision(answer="unclear")


class SummaryRequest(BaseModel):
    """A conversation due for a new rolling summary (DESIGN.md section 10)."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    turn_count: int
    previous: str | None = None
    transcript: list[tuple[str, str]] = Field(default_factory=list)
    """``(author, text)`` for the recent window, oldest first."""


class Summarizer(Protocol):
    async def __call__(self, request: SummaryRequest) -> str | None: ...


class HandoffHook(Protocol):
    async def __call__(self, request: HandoffRequest) -> None: ...


class ChannelSend(Protocol):
    async def __call__(
        self, conversation_id: uuid.UUID, messages: Sequence[OutboundMessage]
    ) -> None: ...


class Probe(Protocol):
    async def __call__(self, point: str, detail: dict[str, Any]) -> None: ...


async def continue_interrupt_check(
    ctx: ConversationContext, message: str, frames: Sequence[Frame]
) -> InterruptDecision:
    """Always ``continue``: the suspended node resumes (BACKLOG.md phase 2).

    The real check is a structured LLM call classifying the message against the root graph's
    edges (DESIGN.md section 6.6) and belongs to phase 6.
    """
    return InterruptDecision(kind="continue")


async def first_slot_extractor(request: SlotRequest) -> dict[str, Any]:
    """Put the whole reply into the first declared slot.

    Deterministic and obviously not the real thing: DESIGN.md section 6.2 says an ``ask`` node
    "extracts slots via structured output", which is
    :class:`~support_core.llm.wiring.StructuredSlotExtractor`. This default stays because it is
    what lets the engine's own tests - suspension, resumption, crash recovery - run without a
    model, and because a pack that wants a deterministic single-slot ask can use it.
    """
    return {request.slots[0]: request.reply} if request.slots else {}


async def no_summary(request: SummaryRequest) -> str | None:
    """Write no summary. ``conversation.summary`` keeps whatever it had.

    The real one is a model call (DESIGN.md section 10) and is wired by whoever builds the LLM
    layer; the engine only knows *when* a summary is due, which it reads from durable columns.
    """
    return None


async def no_handoff(request: HandoffRequest) -> None:
    """Record nothing. The run still suspends ``waiting_human``, which is the part of
    DESIGN.md section 7.3 phase 2 owns; the packet, the queue and the sinks are phase 6."""


async def no_send(conversation_id: uuid.UUID, messages: Sequence[OutboundMessage]) -> None:
    """Deliver nothing. The rows are already durable with ``status = pending_send``; phase 7's
    channel adapters deliver them and mark them ``sent``."""


async def no_probe(point: str, detail: dict[str, Any]) -> None:
    """Do nothing."""


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class EngineHooks:
    """One place to override everything the engine defers to another phase."""

    interrupt_check: InterruptCheck = field(default=continue_interrupt_check)
    extract_slots: SlotExtractor = field(default=first_slot_extractor)
    confirm_decision: ConfirmChecker = field(default=keyword_confirm)
    handoff: HandoffHook = field(default=no_handoff)
    send: ChannelSend = field(default=no_send)
    summarize: Summarizer = field(default=no_summary)
    probe: Probe = field(default=no_probe)
    clock: Callable[[], datetime] = utc_now
    """Every timestamp the engine writes comes from here, never from the database default:
    ``now()`` is transaction start, so two steps checkpointed in one transaction would share
    it and replay could not order them (phase 0 review finding F5)."""


ProbeResult = Awaitable[None]
