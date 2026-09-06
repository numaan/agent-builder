"""Everything a running service is made of. Implements DESIGN.md section 4.1 with 7.1 and 12.

    One domain equals one repository, one Docker image, one service, one Postgres database.
    The service exposes an HTTP API for channels and for the human agent desk. Horizontal
    scaling is safe because all execution state is in Postgres and a per-conversation lock
    ensures a single writer. - DESIGN.md section 4.1

This module builds that service's insides - provider, LLM layer, executor, channel hub, drain
worker - and owns the two operations every transport needs, so that the WebSocket endpoint and
the webhook endpoint do the same thing in the same order:

* :meth:`AppRuntime.accept` - parse a payload, find or create its conversation, run whatever
  turns it makes possible, and hand a conversation whose lock was busy to the drain worker
  rather than waiting for it;
* :meth:`AppRuntime.state` - what the conversation looks like now, which is what a client that
  has just connected (or reconnected) is sent.

Nothing here is web-chat-specific. Phase 7's email webhook is another caller of
:meth:`AppRuntime.accept` with another adapter.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api.config import AppConfig, ConfigError
from support_core.api.drain import DrainQueue
from support_core.channels import (
    ChannelAdapter,
    ChannelHub,
    ChatState,
    ConnectionRegistry,
    ConversationRef,
    InboundMessage,
    WebChatAdapter,
)
from support_core.channels.web_chat import AwaitingSummary
from support_core.engine import Executor
from support_core.engine.hooks import EngineHooks
from support_core.graph.context import ConversationContext
from support_core.graph.manifest import Channel
from support_core.graph.pack import Pack
from support_core.llm.provider import LLMProvider
from support_core.llm.service import LlmService
from support_core.llm.wiring import (
    StructuredConfirmClassifier,
    StructuredSlotExtractor,
    service_for_pack,
)
from support_core.memory import LlmSummarizer
from support_core.storage import repositories as repo
from support_core.storage.session import make_engine


@dataclass(frozen=True, slots=True)
class Accepted:
    """What happened to one inbound message."""

    conversation_id: uuid.UUID
    status: str
    queued: bool
    """The conversation's lock was held elsewhere. The message is durable and ``pending``, the
    drain worker has been asked to pick it up, and this handler returned instead of waiting."""

    created: bool
    """This message opened the conversation."""


def build_provider(config: AppConfig, resolved: str) -> LLMProvider | None:
    """The model provider this configuration means (DESIGN.md section 11.1).

    ``replay`` and ``anthropic`` are the same application with a different object here, which is
    what "chosen by configuration rather than by a code change" has to mean: the prompts, the
    schemas, the retry ladder and the node behaviour are identical, and only the thing that
    answers differs.
    """
    if resolved == "none":
        return None
    if resolved == "replay":
        if config.cassette_dir is None:
            msg = "provider 'replay' needs a cassette_dir to replay from"
            raise ConfigError(msg)
        from support_core.llm.fake import FakeProvider
        from support_core.llm.recording import load_cassettes

        try:
            return FakeProvider(load_cassettes(config.cassette_dir))
        except (OSError, ValueError) as exc:
            msg = f"cannot load cassettes from {config.cassette_dir}: {exc}"
            raise ConfigError(msg) from exc
    if resolved == "anthropic":
        from support_core.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    msg = f"unknown provider {resolved!r}"
    raise ConfigError(msg)


def _check_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse a configured context that is not one, or that verifies its own identity.

    DESIGN.md section 10: ``identity_verified`` "is only ever set by the ``verify_identity``
    sub-graph via a tool". A deployment that could hand it out in a configuration file would
    open every gate in every pack it serves, quietly, at startup.
    """
    if not context:
        return {}
    customer = context.get("customer")
    if isinstance(customer, Mapping) and customer.get("identity_verified"):
        msg = (
            "new_conversation_context may not set customer.identity_verified: only the pack's "
            "verification workflow may (DESIGN.md section 10)"
        )
        raise ConfigError(msg)
    try:
        ConversationContext.model_validate(dict(context))
    except Exception as exc:
        msg = f"new_conversation_context is not a ConversationContext: {exc}"
        raise ConfigError(msg) from exc
    return dict(context)


@dataclass(slots=True)
class AppRuntime:
    """The service's moving parts, built once per application."""

    pack: Pack
    config: AppConfig
    engine: AsyncEngine
    executor: Executor
    hub: ChannelHub
    drainer: DrainQueue
    connections: ConnectionRegistry
    provider_name: str
    llm: LlmService | None
    owns_engine: bool
    send_failures: list[str] = field(default_factory=list)
    """Deliveries a transport refused, most recent last. Phase 7 replaces this with the metrics
    and structured logs of DESIGN.md section 15; until then it is what makes a dropped delivery
    visible at all rather than silent."""

    async def start(self) -> None:
        self.drainer.on_drained = self.announce
        await self.drainer.start()

    async def announce(self, conversation_id: uuid.UUID) -> None:
        """Tell whoever is watching a conversation what it looks like now.

        Needed because a turn is not always run by the connection that caused it: a message that
        arrived while the lock was held is run by the drain worker, and the client would
        otherwise see the messages of that turn without learning that it ended waiting for a
        confirmation. Web chat only - a channel with no live connection has nothing to tell.
        """
        if self.connections.count(conversation_id) == 0:
            return
        state = await self.state(conversation_id)
        await self.connections.broadcast(
            conversation_id,
            {"type": "turn", "queued": False, **json.loads(state.model_dump_json())},
        )

    async def aclose(self) -> None:
        await self.drainer.aclose()
        if self.owns_engine:
            await self.engine.dispose()

    def adapter(self, channel: str) -> ChannelAdapter:
        return self.hub.adapter(channel)

    async def accept(self, raw: Any, *, channel: str) -> Accepted:
        """Take one payload from a channel all the way to a turn (DESIGN.md section 7.1).

        The order matters and is the same for every channel: parse (and refuse) first, resolve
        the conversation from the channel's own durable key second, and only then hand the text
        to the engine, which writes it as ``pending`` before it tries for the lock. A handler
        that cannot take the lock returns ``queued`` rather than holding this connection until
        the lock frees (phase 2 review finding R7).
        """
        adapter = self.hub.adapter(channel)
        inbound = await adapter.parse_inbound(raw)
        return await self.deliver_inbound(inbound)

    async def deliver_inbound(self, inbound: InboundMessage) -> Accepted:
        conversation, created = await self.hub.conversation_for(
            inbound, context=self.config.new_conversation_context
        )
        outcome = await self.executor.on_inbound(conversation.id, inbound.text)
        if outcome.queued:
            self.drainer.submit(conversation.id)
        return Accepted(
            conversation_id=conversation.id,
            status=outcome.status or "unknown",
            queued=outcome.queued,
            created=created,
        )

    async def conversation_for_key(self, channel: Channel, key: str) -> ConversationRef:
        """The conversation a client names, for a transport that connects before it speaks.

        A browser opens its socket and says which session it is; it may have said nothing since
        Tuesday, or nothing ever. Either way the conversation is the durable one for that key.
        """
        self.hub.adapter(channel)
        conversation, _created = await self.hub.conversation_for(
            InboundMessage(channel=channel, conversation_key=key, text=""),
            context=self.config.new_conversation_context,
        )
        return conversation

    async def state(self, conversation_id: uuid.UUID, *, history: int = 200) -> ChatState:
        """The conversation as a client needs to render it, read from durable state only."""
        async with self.executor.sessions() as session, session.begin():
            conversation = await repo.get_conversation(session, conversation_id)
            run = await repo.load_run(session, conversation_id)
            rows = await repo.transcript(session, conversation_id, history)
            return ChatState(
                conversation_id=conversation_id,
                session=conversation.channel_key if conversation is not None else None,
                status=run.status if run is not None else "idle",
                awaiting=AwaitingSummary.of(run.awaiting if run is not None else None),
                history=[
                    {
                        "author": row.author,
                        "text": row.text,
                        "pending": row.status == "pending",
                    }
                    for row in rows
                ],
            )


def build_runtime(
    pack: Pack,
    config: AppConfig,
    *,
    engine: AsyncEngine | None = None,
    provider: LLMProvider | None = None,
    hooks: EngineHooks | None = None,
    adapters: Sequence[ChannelAdapter] | None = None,
    env: Mapping[str, str] | None = None,
) -> AppRuntime:
    """Build the service's insides. Nothing here talks to the database or the network yet."""
    context = _check_context(config.new_conversation_context)
    config = config.model_copy(update={"new_conversation_context": context})

    resolved = config.resolve_provider(env)
    model_provider = provider if provider is not None else build_provider(config, resolved)
    if provider is not None:
        resolved = provider.name

    service = service_for_pack(pack, model_provider) if model_provider is not None else None
    engine_hooks = hooks or EngineHooks()
    if service is not None:
        engine_hooks.extract_slots = StructuredSlotExtractor(service)
        engine_hooks.confirm_decision = StructuredConfirmClassifier(service)
        engine_hooks.summarize = LlmSummarizer(
            service, max_chars=pack.manifest.memory.max_summary_chars
        )

    owns_engine = engine is None
    db = engine if engine is not None else make_engine()
    executor = Executor(
        pack,
        db,
        hooks=engine_hooks,
        lock_wait_seconds=config.lock_wait_seconds,
        llm=service,
    )

    connections = ConnectionRegistry()
    chosen = list(adapters) if adapters is not None else [WebChatAdapter(connections)]
    for adapter in chosen:
        registry = getattr(adapter, "connections", None)
        if isinstance(registry, ConnectionRegistry):
            connections = registry
    failures: list[str] = []
    hub = ChannelHub(
        executor,
        chosen,
        on_send_error=lambda conversation, message, error: failures.append(
            f"{conversation.id}: {type(error).__name__}: {error}"
        ),
    )
    # The hook and the hub each need the other: the hub sends through the executor's
    # conversations, the executor sends through the hub. Binding after construction is the
    # honest way round it, and it is what makes `hooks.send` a channel rather than a stub.
    executor.hooks.send = hub.deliver

    return AppRuntime(
        pack=pack,
        config=config,
        engine=db,
        executor=executor,
        hub=hub,
        drainer=DrainQueue(
            executor,
            workers=config.drain_workers,
            budget_seconds=config.drain_budget_seconds,
        ),
        connections=connections,
        provider_name=resolved,
        llm=service,
        owns_engine=owns_engine,
        send_failures=failures,
    )
