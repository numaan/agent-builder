"""The read-only conversation context expressions see as ``ctx``. Implements DESIGN.md
sections 6.1 and 10 (the "Customer context" layer).

DESIGN.md section 6.1: "State is per graph frame; the shared ``ConversationContext`` is
available read-only to all nodes." Section 10 says the customer context holds "identity
verification status, customer record from CRM, channel, locale".

Phase 1 needs this model for one reason: the validator type-checks every ``ctx`` expression
(``ctx.customer.identity_verified`` and the rest) against it at load time. It is deliberately
minimal and it is *not* the durable representation; phase 2 owns ``conversation.context`` and
may extend this model, but must not narrow it without updating the graphs that read it.

``identity_verified`` is only ever set by the ``verify_identity`` sub-graph via a tool
(DESIGN.md section 10); nothing in customer memory may set it.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CustomerContext(BaseModel):
    """What is known about the person on the other end of the conversation."""

    model_config = ConfigDict(extra="forbid")

    ref: str | None = None
    """Stable customer identifier in the pack's system of record, once known."""

    identity_verified: bool = False
    """Set only by the identity verification workflow (DESIGN.md sections 5.2 and 10)."""

    name: str | None = None
    email: str | None = None
    locale: str = "en"
    attributes: dict[str, Any] = Field(default_factory=dict)
    """CRM record fields the pack loaded. Untyped on purpose: pack-specific (phase 4 types it)."""


class ConversationContext(BaseModel):
    """Shared, read-only context for every frame in a conversation."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = None
    channel: Literal["web_chat", "email"] = "web_chat"
    """DESIGN.md section 12 channels; the desk is not a customer channel."""

    locale: str = "en"
    customer: CustomerContext = Field(default_factory=CustomerContext)
    summary: str | None = None
    """Rolling conversation summary (DESIGN.md section 10)."""
