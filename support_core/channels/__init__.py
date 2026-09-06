"""Channel adapters: web_chat, email, desk. Implements DESIGN.md section 12.

Phase W delivers the protocol itself and the web chat adapter over a WebSocket; phase 7 adds
the email adapter and the desk, both of which implement this protocol without changing it. The
protocol is therefore written against the *email* case as much as against the browser one: a
conversation named by a mail thread rather than by a session or a connection, and gaps measured
in days (DESIGN.md section 7.2).
"""

from support_core.channels.base import (
    ChannelAdapter,
    ChannelError,
    ConversationRef,
    InboundMessage,
    InboundRejected,
)
from support_core.channels.hub import ChannelHub, UnknownChannelError
from support_core.channels.web_chat import (
    AwaitingSummary,
    ChatConnection,
    ChatState,
    ConnectionRegistry,
    WebChatAdapter,
)

__all__ = [
    "AwaitingSummary",
    "ChannelAdapter",
    "ChannelError",
    "ChannelHub",
    "ChatConnection",
    "ChatState",
    "ConnectionRegistry",
    "ConversationRef",
    "InboundMessage",
    "InboundRejected",
    "UnknownChannelError",
    "WebChatAdapter",
]
