"""Wiring channel adapters to the engine. Implements DESIGN.md section 12 with 7.1 and 4.1.

Two jobs, both of them the same for every channel, which is why they are here and not in an
adapter:

* **Which conversation is this?** A channel names a conversation with its own key (a chat
  session, a mail thread). :meth:`ChannelHub.conversation_for` turns that key into the durable
  conversation, creating one the first time and finding it every time after - across a
  reconnect, across a restart of the service, and across the days DESIGN.md section 7.2 allows
  an email conversation to be idle.

* **Where does an outbound message go?** :meth:`ChannelHub.deliver` is what
  :attr:`~support_core.engine.hooks.EngineHooks.send` is bound to. It looks up the conversation's
  channel, hands each committed message to that channel's adapter, and - this is the part that
  matters - never lets a transport failure out. A closed browser tab is the ordinary case, and
  phase 2's self-critique names a raising ``send`` hook as one of the two ways delivery can take
  a finished turn down with it: the hook runs inside the transaction that marks the rows sent, so
  an exception rolls that back and aborts the drain. The rows are durable and a reconnecting
  client is sent the transcript, so dropping a delivery is recoverable and dropping a turn is
  not.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError

from support_core.channels.base import ChannelAdapter, ChannelError, ConversationRef
from support_core.channels.base import InboundMessage as InboundMessage
from support_core.engine.executor import Executor
from support_core.engine.types import OutboundMessage
from support_core.storage import repositories as repo

SendErrorHandler = Callable[[ConversationRef, OutboundMessage, Exception], None]
"""Told about a delivery that failed. The default does nothing but is always called, so an
application that wants the failure logged, counted or retried has one place to say so."""


def _ignore(
    conversation: ConversationRef, message: OutboundMessage, error: Exception
) -> None:  # pragma: no cover - the default is deliberately silent
    return None


class UnknownChannelError(ChannelError):
    """No adapter is registered for a conversation's channel."""


class ChannelHub:
    """The adapters an application serves, and the two things every channel needs."""

    def __init__(
        self,
        executor: Executor,
        adapters: Sequence[ChannelAdapter],
        *,
        on_send_error: SendErrorHandler | None = None,
    ) -> None:
        self.executor = executor
        self.adapters: dict[str, ChannelAdapter] = {}
        for adapter in adapters:
            if adapter.channel in self.adapters:
                msg = f"two adapters registered for channel {adapter.channel!r}"
                raise ChannelError(msg)
            self.adapters[adapter.channel] = adapter
        self.on_send_error: SendErrorHandler = on_send_error or _ignore

    def adapter(self, channel: str) -> ChannelAdapter:
        adapter = self.adapters.get(channel)
        if adapter is None:
            known = sorted(self.adapters) or ["none"]
            msg = f"no channel adapter for {channel!r}; this application serves {known}"
            raise UnknownChannelError(msg)
        return adapter

    async def conversation_for(
        self,
        inbound: InboundMessage,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[ConversationRef, bool]:
        """The conversation this message belongs to, creating it the first time.

        Returns the conversation and whether it was created by this call. ``context`` seeds the
        :class:`~support_core.graph.context.ConversationContext` of a *new* conversation only:
        an existing conversation's context is what the engine and its tools have made of it, and
        an inbound message is not a reason to overwrite it.

        The unique ``(channel, channel_key)`` index is what makes this safe when two callers
        arrive at once - two browser tabs opened together, a webhook delivered twice. The loser
        of the race reads the winner's row back instead of starting a second conversation.
        """
        found = await self.find(inbound.channel, inbound.conversation_key)
        if found is not None:
            return found, False
        # An IntegrityError here means somebody else created it between the lookup and the
        # insert. Their row is the conversation; ours never existed, because the conversation
        # and its run are inserted in one transaction.
        #
        # Caught rather than suppressed, because the answer *this* call gives to "did you create
        # it?" is the difference between a greeting sent once and a greeting sent twelve times.
        # Every loser of a create race used to be told `created=True`, since the flag was read
        # off its own first lookup missing rather than off the insert committing (review finding
        # W7). Nothing branches on it in phase W; phase 7's email adapter is exactly the caller
        # that would.
        created = True
        try:
            await self.executor.start_conversation(
                channel=inbound.channel,
                channel_key=inbound.conversation_key,
                customer_ref=inbound.customer_ref,
                context=dict(context) if context else None,
            )
        except IntegrityError:
            created = False
        again = await self.find(inbound.channel, inbound.conversation_key)
        if again is None:  # pragma: no cover - would mean the unique index did not hold
            msg = f"conversation {inbound.conversation_key!r} vanished between create and read"
            raise ChannelError(msg)
        return again, created

    async def snapshot(self, conversation_id: uuid.UUID) -> ConversationRef | None:
        """The conversation as it stands, or ``None`` if there is no such row."""
        async with self.executor.sessions() as session, session.begin():
            row = await repo.get_conversation(session, conversation_id)
            return None if row is None else ConversationRef.of(row)

    async def deliver(
        self, conversation_id: uuid.UUID, messages: Sequence[OutboundMessage]
    ) -> None:
        """Hand committed messages to the conversation's channel. Never raises.

        Bound to :attr:`~support_core.engine.hooks.EngineHooks.send`, so it runs after the
        checkpoint has committed and inside the transaction that marks the rows ``sent``.
        """
        if not messages:
            return
        conversation = await self.snapshot(conversation_id)
        if conversation is None:  # pragma: no cover - a conversation cannot be deleted yet
            return
        try:
            adapter = self.adapter(conversation.channel)
        except UnknownChannelError as exc:
            for message in messages:
                self.on_send_error(conversation, message, exc)
            return
        for message in messages:
            try:
                await adapter.send(conversation, message)
            except Exception as exc:  # deliberately everything: see the module docstring
                self.on_send_error(conversation, message, exc)

    async def find(self, channel: str, key: str) -> ConversationRef | None:
        """The conversation a channel key names, or ``None``. Creates nothing."""
        return await self._by_key(channel, key)

    async def _by_key(self, channel: str, key: str) -> ConversationRef | None:
        async with self.executor.sessions() as session, session.begin():
            row = await repo.conversation_by_channel_key(session, channel=channel, channel_key=key)
            return None if row is None else ConversationRef.of(row)
