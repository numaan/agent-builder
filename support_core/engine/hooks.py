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
``resume_offer``              6            an explicit yes or no word, else ``unclear``
``extract_slots``             3            the whole reply fills the first declared slot
``confirm_decision``          4            an explicit yes or no word, else ``unclear``
``handoff``                   6            record nothing, report False; the run still suspends
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

    label: str | None = None
    """The root graph's edge label that names that workflow, for the record the engine keeps of
    a *deferred* intent: a person reading it wants the pack's own word for the thing the
    customer asked for, not the graph file's."""

    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class InterruptRequest(BaseModel):
    """What the engine knows when a message arrives mid-workflow (DESIGN.md section 6.6)."""

    model_config = ConfigDict(extra="forbid")

    message: str
    ctx: ConversationContext
    frames: list[Frame] = Field(default_factory=list)
    current_graph: str = ""
    current_node: str = ""
    question: str | None = None
    """The question the suspended node asked, which is what ``continue`` means."""

    intents: list[tuple[str, str, str | None]] = Field(default_factory=list)
    """``(label, graph, description)`` for each workflow the root graph declares an edge to.
    DESIGN.md section 6.6: "Available intents are the root graph's declared edges.\""""

    interruptible: bool = True
    """Whether ``pack.yaml``'s ``interrupts`` lets this graph be interrupted. The check is told
    so it can explain itself; the engine decides regardless of what comes back."""

    window: list[tuple[str, str]] = Field(default_factory=list)


class HandoffRequest(BaseModel):
    """Everything the engine knows when it gives up on a turn (DESIGN.md sections 7.3, 13)."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    run_id: uuid.UUID
    reason: str
    """DESIGN.md section 7.3's vocabulary and the reasons phases 3, 4 and 6 added to it:
    ``limit_exceeded``, ``node_error``, ``timeout``, ``pack_incompatible``, ``engine_error``,
    ``llm_unavailable``, ``llm_invalid_output``, ``low_confidence``, ``model_requested_handoff``,
    ``tool_refused``, ``tool_failed``, and whatever a pack's ``handoff`` node declares."""

    detail: str | None = None
    frames: list[Frame] = Field(default_factory=list)
    node_id: str | None = None
    """Where it stopped. The frame stack usually says, but a timeout sweep has no frame and a
    handoff node knows better than the stack does at the moment it fires."""

    step_id: str | None = None
    """The step that raised it, which is what makes delivery idempotent across a crash."""

    next_steps: list[str] = Field(default_factory=list)
    """What the pack's ``handoff`` node suggests the human does, overriding core's
    reason-keyed checklist (DESIGN.md section 13's ``suggested_next_steps``). Empty for a
    handoff the engine raised: core does not know a pack's desk."""


class InterruptCheck(Protocol):
    async def __call__(self, request: InterruptRequest) -> InterruptDecision: ...


class ResumeOfferRequest(BaseModel):
    """A customer's answer to "shall we go back to X?" (DESIGN.md section 6.6 step 4)."""

    model_config = ConfigDict(extra="forbid")

    workflow: str
    offer: str
    """The question exactly as it was put to them."""

    reply: str
    ctx: ConversationContext
    window: list[tuple[str, str]] = Field(default_factory=list)


class ResumeOfferChecker(Protocol):
    async def __call__(self, request: ResumeOfferRequest) -> "ConfirmDecision": ...


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

    hint: str | None = None
    """What the engine knows about this reply that the node does not (DESIGN.md section 6.6).

    Today: the customer also asked for a different workflow and the current graph would not stop
    for it, so part of this reply is not an answer at all. Without that, "sure - and change my
    address to 4 Elm Row" has to be read as a passcode by something that was told it is one."""

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
    async def __call__(self, request: HandoffRequest) -> bool:
        """Deliver the packet. Returns whether *somebody was actually told*.

        A boolean rather than ``None`` because the engine records the answer in the suspension
        detail and, for a ``handoff`` node, decides what the customer is told from it. A hook
        that returns ``False`` has not raised and has not failed the turn - it has said that the
        page did not land, which is the difference between a run parked for a human who knows
        and one parked for a human who does not (review finding P2).
        """
        ...


class ChannelSend(Protocol):
    async def __call__(
        self, conversation_id: uuid.UUID, messages: Sequence[OutboundMessage]
    ) -> None: ...


class Probe(Protocol):
    async def __call__(self, point: str, detail: dict[str, Any]) -> None: ...


async def continue_interrupt_check(request: InterruptRequest) -> InterruptDecision:
    """Always ``continue``: the suspended node resumes.

    Still the default, and still the right one. The real check is a structured model call
    (:class:`~support_core.llm.wiring.StructuredInterruptCheck`, DESIGN.md section 6.6), and an
    engine built without a provider must not pretend to have one; ``continue`` is also what the
    engine does for a pack that declares no ``interrupts`` block at all, so the two agree.
    """
    return InterruptDecision(kind="continue")


async def keyword_resume_offer(request: ResumeOfferRequest) -> ConfirmDecision:
    """The default reading of "shall we go back to that?": an explicit word, or ``unclear``.

    Same closed lists and same whole-reply match as :func:`keyword_confirm`, and the same
    reasoning: guessing "no" abandons work the customer asked for, guessing "yes" drags them back
    to something they had finished with, and asking again costs one message.
    """
    reply = " ".join(request.reply.lower().strip().strip(".!,").split())
    if reply in AFFIRMATIVE:
        return ConfirmDecision(answer="yes")
    if reply in NEGATIVE:
        return ConfirmDecision(answer="no")
    return ConfirmDecision(answer="unclear")


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


async def no_handoff(request: HandoffRequest) -> bool:
    """Record nothing, and say so. The run still suspends ``waiting_human``, which is the part
    of DESIGN.md section 7.3 phase 2 owns; the packet, the queue and the sinks are phase 6.

    It returns ``False`` because nobody was told, and an engine built without a desk should not
    be able to report otherwise. DESIGN.md section 14 forbids promising an escalation that did
    not happen, and the promise is made from this answer (review finding P2)."""
    return False


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
    resume_offer: ResumeOfferChecker = field(default=keyword_resume_offer)
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
