"""The channel adapter protocol. Implements DESIGN.md section 12.

Section 12 gives the protocol in three methods::

    class ChannelAdapter(Protocol):
        channel: str
        async def parse_inbound(self, raw: Any) -> InboundMessage: ...
        async def send(self, conversation: Conversation, msg: OutboundMessage) -> None: ...
        def conversation_key(self, raw: Any) -> str: ...   # thread id, session id

Those three are the whole contract, and phase W keeps them as they are written because phase 7's
email adapter has to implement the same protocol without changing it. Email is the case this
module is designed against even though only web chat implements it here:

* a conversation is named by a **mail thread**, not by a session and never by a connection, so
  :meth:`ChannelAdapter.conversation_key` is the only identity and it comes out of the transport
  payload (a session key, an ``In-Reply-To`` header);
* the payload is **whatever the transport delivers** - a JSON frame from a browser, a webhook
  body from a mail provider - so ``raw`` is deliberately untyped and each adapter validates its
  own shape, refusing with :class:`InboundRejected` rather than guessing;
* gaps are **days**, so nothing an adapter needs may live in memory: what
  :meth:`ChannelAdapter.send` is given is a :class:`ConversationRef`, read from the row at
  delivery time.

The one deliberate difference from the written signature: ``send`` takes a
:class:`ConversationRef` rather than the :class:`~support_core.storage.models.Conversation` ORM
object. An adapter holding a live ORM instance could write through it, and a transport that keeps
it past the session that loaded it would touch a detached object; a frozen snapshot of the
fields a transport can legitimately need has neither problem.
"""

import uuid
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from support_core.engine.types import OutboundMessage
from support_core.graph.manifest import Channel
from support_core.storage.models import Conversation

__all__ = [
    "ChannelAdapter",
    "ChannelError",
    "ConversationRef",
    "InboundMessage",
    "InboundRejected",
]


class ChannelError(Exception):
    """Anything a channel adapter could not do."""


class InboundRejected(ChannelError):
    """The payload is not something this adapter can read.

    Raised by :meth:`ChannelAdapter.parse_inbound` and :meth:`ChannelAdapter.conversation_key`.
    A transport turns it into whatever "you sent nonsense" means there - a 400 for a webhook, an
    error frame on a socket - and nothing reaches the engine.
    """


class InboundMessage(BaseModel):
    """One customer message, in the form the engine can use (DESIGN.md section 12).

    Produced by :meth:`ChannelAdapter.parse_inbound` from the transport's own payload. Every
    field here is a fact about the *message*; nothing about the transport survives except in
    :attr:`metadata`, which nothing in the engine reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Channel
    conversation_key: str = Field(min_length=1)
    """The channel's own name for the conversation: a chat session key, a mail thread id. It is
    the only identity a channel has, and it is durable - a reconnect, or a reply two days later,
    carries the same key and reaches the same conversation."""

    text: str
    customer_ref: str | None = None
    """Who the transport believes this is, when the transport can know - a mail provider knows
    the From address. A browser cannot know anything of the sort, and an adapter for one must not
    pretend otherwise: see :mod:`support_core.channels.web_chat`."""

    external_id: str | None = None
    """The transport's own id for this message (a mail ``Message-ID``), where it has one.

    Not used by phase W. Delivery towards a channel is at-least-once and the engine offers no
    de-duplication either way (phase 2 self-critique, fragility item 1), so this is the field an
    adapter that can de-duplicate inbound - email can, a socket frame cannot - will need."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Transport detail the adapter wants back at delivery time. Opaque to the engine."""


class ConversationRef(BaseModel):
    """An immutable snapshot of a conversation, for delivery (DESIGN.md section 12).

    What :meth:`ChannelAdapter.send` is given. It carries what a transport can legitimately need
    to address a message - which conversation, on which channel, under which channel key, for
    which customer - and nothing it could write through.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    channel: str
    key: str | None = None
    customer_ref: str | None = None
    status: str = "open"
    context: dict[str, Any] = Field(default_factory=dict)
    """The stored :class:`~support_core.graph.context.ConversationContext`, as JSON. An email
    adapter reads the customer's address out of it; nothing may write to it here."""

    @classmethod
    def of(cls, row: Conversation) -> "ConversationRef":
        """Snapshot a conversation row. Read inside the session that loaded it."""
        return cls(
            id=row.id,
            channel=row.channel,
            key=row.channel_key,
            customer_ref=row.customer_ref,
            status=row.status,
            context=dict(row.context or {}),
        )


@runtime_checkable
class ChannelAdapter(Protocol):
    """DESIGN.md section 12's protocol, unchanged apart from ``send``'s first argument."""

    channel: str

    def conversation_key(self, raw: Any) -> str:
        """The channel's durable name for the conversation this payload belongs to.

        Separate from :meth:`parse_inbound` because a transport often has to resolve the
        conversation before it has a message - a browser opening a socket, a webhook that
        carries a delivery receipt rather than text.
        """
        ...

    async def parse_inbound(self, raw: Any) -> InboundMessage:
        """Read the transport's payload into an :class:`InboundMessage`.

        Raises :class:`InboundRejected` if the payload is not one.
        """
        ...

    async def send(self, conversation: ConversationRef, msg: OutboundMessage) -> None:
        """Deliver one committed message to the customer.

        Called after the checkpoint that wrote the message has committed, never before: no
        customer sees text that a guardrail (phase 7) would later have stopped.
        """
        ...


def as_metadata(raw: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """The named keys of a payload, for :attr:`InboundMessage.metadata`."""
    return {key: raw[key] for key in keys if key in raw}
