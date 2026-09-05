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
``handoff``                   6            record nothing; the run still suspends for a human
``send``                      7            do not deliver; rows stay ``pending_send``
``probe``                     -            nothing (tests use it to kill a turn mid-flight)
============================  ===========  ==================================================
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


class SlotExtractor(Protocol):
    async def __call__(
        self, slots: Sequence[str], reply: str, ctx: ConversationContext
    ) -> dict[str, Any]: ...


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


async def first_slot_extractor(
    slots: Sequence[str], reply: str, ctx: ConversationContext
) -> dict[str, Any]:
    """Put the whole reply into the first declared slot.

    Deterministic and obviously not the real thing: DESIGN.md section 6.2 says an ``ask`` node
    "extracts slots via structured output", which is phase 3. Keeping the extraction behind
    this hook is what lets phase 2 own suspension and resumption (section 7.2) without owning
    the model call.
    """
    return {slots[0]: reply} if slots else {}


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
    handoff: HandoffHook = field(default=no_handoff)
    send: ChannelSend = field(default=no_send)
    probe: Probe = field(default=no_probe)
    clock: Callable[[], datetime] = utc_now
    """Every timestamp the engine writes comes from here, never from the database default:
    ``now()`` is transaction start, so two steps checkpointed in one transaction would share
    it and replay could not order them (phase 0 review finding F5)."""


ProbeResult = Awaitable[None]
