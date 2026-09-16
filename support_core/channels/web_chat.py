"""The web chat channel. Implements DESIGN.md section 12 ("Web chat").

    **Web chat**: WebSocket or SSE endpoint. Streaming of the final message only; intermediate
    node output is not streamed. - DESIGN.md section 12

Three things this adapter is careful about.

**Identity is the session key, never the connection.** A conversation is found by
``conversation_key``, which is a durable token the client keeps; a socket is only a place to
push text at. Two tabs open on one session are two connections on one conversation, a reconnect
after a dropped network is a third, and a conversation suspended waiting for the customer
(DESIGN.md section 7.2) resumes on whichever connection the reply arrives on - or on none, if
the reply arrives by ``POST`` instead.

**The customer describes nothing about themselves.** The inbound payload is a session key and
text, and the model forbids every other key. A browser that could post ``customer.ref`` or
``identity_verified`` would be an identity-spoofing hole with a friendly interface; who the
customer is comes from the application's configuration and from the pack's own verification
workflow, which is the only thing DESIGN.md section 10 lets set ``identity_verified``.

**Nothing is streamed before it commits.** :meth:`WebChatAdapter.send` is called by the engine
after the checkpoint that wrote the message, so what reaches the socket is what the conversation
durably said. There is no token streaming out of a model here and there must not be one until
outbound guardrails (DESIGN.md section 14, phase 7) run before delivery: a customer must never
see a message a guardrail would have stopped.
"""

import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from support_core.channels.base import (
    ConversationRef,
    InboundMessage,
    InboundRejected,
)
from support_core.engine.types import OutboundMessage
from support_core.graph.forms import FormSchema

CHANNEL = "web_chat"

SESSION_KEY = re.compile(r"^[A-Za-z0-9_.:\-]{8,128}$")
"""What a session key may look like. A key is an opaque token the client generates, so it is
worth refusing anything that is not one: it lands in a unique database index and is echoed back
to whoever holds it."""

MAX_TEXT = 4000
"""Longest customer message accepted. A browser has no business posting more, and the limit is
here rather than in the engine because it is a property of the transport."""


class ChatPayload(BaseModel):
    """What a browser may send. Every other key is refused (see the module docstring)."""

    model_config = ConfigDict(extra="forbid")

    session: str
    text: str
    type: Literal["message"] | None = None
    """Present on socket frames, absent on a plain webhook body; either is fine."""


@runtime_checkable
class ChatConnection(Protocol):
    """Somewhere to push events at. A WebSocket in production, a list in tests."""

    async def push(self, event: Mapping[str, Any]) -> None: ...


class ConnectionRegistry:
    """Which live connections are watching which conversation.

    Deliberately not durable and deliberately not authoritative: it is a delivery convenience,
    and everything it holds can be lost without losing a message, because the messages are rows
    and a connection that arrives late is sent the transcript.
    """

    def __init__(self) -> None:
        self._by_conversation: dict[uuid.UUID, list[ChatConnection]] = {}
        self._waiting: dict[str, list[ChatConnection]] = {}

    def add(self, conversation_id: uuid.UUID, connection: ChatConnection) -> None:
        self._by_conversation.setdefault(conversation_id, []).append(connection)

    def wait(self, key: str, connection: ChatConnection) -> None:
        """Watch a *channel key* whose conversation does not exist yet.

        A socket now resolves its session key without creating a conversation (phase W review
        finding W8), so between "connected" and "said something" there is nothing to register
        against. Two tabs opened together on a fresh key, and a tab watching while the first
        message arrives by ``POST``, both live in this gap; :meth:`attach` closes it the moment
        somebody's message creates the row.
        """
        self._waiting.setdefault(key, []).append(connection)

    def attach(self, key: str, conversation_id: uuid.UUID) -> int:
        """Move every connection waiting on ``key`` onto its now-existing conversation.

        Called by the runtime *before* the turn runs, so a socket that has been waiting is
        watching before the engine can produce a message for it. Returns how many moved.
        """
        waiting = self._waiting.pop(key, [])
        for connection in waiting:
            self.add(conversation_id, connection)
        return len(waiting)

    def discard(self, conversation_id: uuid.UUID, connection: ChatConnection) -> None:
        live = self._by_conversation.get(conversation_id)
        if live is None:
            return
        self._by_conversation[conversation_id] = [c for c in live if c is not connection]
        if not self._by_conversation[conversation_id]:
            del self._by_conversation[conversation_id]

    def forget(self, connection: ChatConnection) -> None:
        """Remove a connection from wherever it is watching or waiting.

        What a closing socket calls, because it may have been moved by :meth:`attach` since it
        registered and so cannot know which conversation it ended up on.
        """
        for key, waiting in list(self._waiting.items()):
            remaining = [c for c in waiting if c is not connection]
            if remaining:
                self._waiting[key] = remaining
            else:
                del self._waiting[key]
        for conversation_id in list(self._by_conversation):
            self.discard(conversation_id, connection)

    def watching(self, conversation_id: uuid.UUID) -> tuple[ChatConnection, ...]:
        return tuple(self._by_conversation.get(conversation_id, ()))

    def count(self, conversation_id: uuid.UUID) -> int:
        return len(self._by_conversation.get(conversation_id, ()))

    async def broadcast(self, conversation_id: uuid.UUID, event: Mapping[str, Any]) -> int:
        """Push an event to every live connection. Returns how many took it.

        A connection that raises is dropped from the registry rather than retried: its socket is
        gone, and the message it missed is in the transcript the next connection is sent.
        """
        delivered = 0
        for connection in self.watching(conversation_id):
            try:
                await connection.push(event)
            except Exception:
                self.discard(conversation_id, connection)
            else:
                delivered += 1
        return delivered


class WebChatAdapter:
    """DESIGN.md section 12's web chat adapter over the connection registry."""

    channel: str = CHANNEL

    def __init__(self, connections: ConnectionRegistry | None = None) -> None:
        self.connections = connections or ConnectionRegistry()

    def conversation_key(self, raw: Any) -> str:
        """The session key out of a payload, validated."""
        if not isinstance(raw, Mapping):
            msg = "a web chat payload must be a JSON object"
            raise InboundRejected(msg)
        key = raw.get("session")
        if not isinstance(key, str) or not SESSION_KEY.match(key):
            msg = (
                "a web chat payload needs a 'session' key of 8 to 128 characters from "
                "[A-Za-z0-9_.:-]"
            )
            raise InboundRejected(msg)
        return key

    async def parse_inbound(self, raw: Any) -> InboundMessage:
        if not isinstance(raw, Mapping):
            msg = "a web chat payload must be a JSON object"
            raise InboundRejected(msg)
        try:
            payload = ChatPayload.model_validate(dict(raw))
        except ValidationError as exc:
            fields = ", ".join(str(error["loc"][0]) for error in exc.errors() if error["loc"])
            msg = f"this is not a web chat message: {fields or exc.error_count()} rejected"
            raise InboundRejected(msg) from exc
        key = self.conversation_key(dict(raw))
        text = payload.text.strip()
        if not text:
            msg = "an empty message is not a message"
            raise InboundRejected(msg)
        if len(text) > MAX_TEXT:
            msg = f"a web chat message may be at most {MAX_TEXT} characters"
            raise InboundRejected(msg)
        return InboundMessage(channel="web_chat", conversation_key=key, text=text)

    async def send(self, conversation: ConversationRef, msg: OutboundMessage) -> None:
        """Push one committed message to every connection watching this conversation.

        No connection is not a failure: the customer closed the tab, the message is a durable
        row, and the next connection to open on this conversation is sent the transcript.
        """
        await self.connections.broadcast(
            conversation.id,
            {"type": "message", "author": msg.author, "text": msg.text},
        )


class AwaitingSummary(BaseModel):
    """What the run is waiting for, in the little the client is allowed to know.

    The client shows the confirmation step differently from an ordinary question - that is the
    moment DESIGN.md section 8.2 exists for, and a demo that hides it demonstrates nothing - so
    it is told *that* a confirm node is waiting and which tool it would run. It is not told the
    argument hash: the hash is the engine's binding between what was shown and what will happen,
    and nothing outside the engine has a use for it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = "question"
    """``confirm`` when a ``confirm`` node is waiting for the customer's explicit yes, and
    ``question`` for every other node that is. Anything else - a run parked for a human, say -
    keeps the engine's own word for it (``handoff``), because a client that shows a customer
    "waiting" wants to know which kind of waiting this is."""

    node: str | None = None
    tool: str | None = None
    form: FormSchema | None = None
    """The pack's form for this node, when it declares one (:attr:`PackUI.forms`). Filled by the
    runtime from the pack, not by the engine: it is how the node is *rendered*, not part of what
    the run is waiting for. A client that has one shows a form; a client that does not falls back
    to a text reply, and both resume the same gate."""

    @classmethod
    def of(cls, awaiting: Mapping[str, Any] | None) -> "AwaitingSummary | None":
        if not awaiting:
            return None
        detail = awaiting.get("detail")
        detail = detail if isinstance(detail, Mapping) else {}
        waiting_on_a_node = awaiting.get("kind") == "node"
        kind = detail.get("kind") or ("question" if waiting_on_a_node else awaiting.get("kind"))
        node = awaiting.get("node") or detail.get("node")
        return cls(
            kind=str(kind or "question"),
            node=str(node) if node is not None else None,
            tool=str(detail["tool"]) if isinstance(detail.get("tool"), str) else None,
        )


class ChatState(BaseModel):
    """The conversation as the client needs to render it."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID | None = None
    """``None`` for a session key no conversation exists for yet.

    A socket resolves its key without creating a row (phase W review finding W8), so "connected,
    nothing said yet" is a real state a client is sent rather than a conversation the customer
    never started."""

    session: str | None = None
    status: str = "idle"
    awaiting: AwaitingSummary | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
