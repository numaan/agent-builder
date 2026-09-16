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

from sqlalchemy import text as sql_text
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
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
from support_core.engine.interrupts import workflow_intents
from support_core.engine.types import OutboundMessage
from support_core.graph.context import ConversationContext
from support_core.graph.manifest import Channel
from support_core.graph.pack import Pack
from support_core.handoff import HandoffService, default_sink
from support_core.knowledge.wiring import build_retriever, build_store
from support_core.llm.provider import LLMProvider
from support_core.llm.service import LlmService
from support_core.llm.wiring import (
    StructuredConfirmClassifier,
    StructuredInterruptCheck,
    StructuredResumeOffer,
    StructuredSlotExtractor,
    service_for_pack,
)
from support_core.memory import LlmSummarizer
from support_core.storage import repositories as repo
from support_core.storage.config import pool_settings
from support_core.storage.models import Run
from support_core.storage.session import (
    connections_in_use,
    make_engine,
    make_session_factory,
    pool_capacity,
)

SEND_FAILURES_KEPT = 50
"""How many delivery failures :attr:`AppRuntime.send_failures` remembers."""


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
    if resolved == "glm":
        from support_core.llm.glm_provider import GlmProvider, api_key_present

        if not api_key_present():
            msg = "provider 'glm' needs GLM_API_KEY (or ZAI_API_KEY) in the environment"
            raise ConfigError(msg)
        return GlmProvider()
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


def _check_channels(pack: Pack, adapters: Sequence[ChannelAdapter]) -> None:
    """Serve only the channels the pack enabled (DESIGN.md section 12).

    "Packs enable channels in ``pack.yaml``." A service that answered on a channel its pack
    never declared would run that pack's workflows under timeouts, a persona and a policy set
    written for somewhere else - and the manifest is where a pack author says which somewhere
    that is.
    """
    declared = set(pack.manifest.channels)
    undeclared = sorted({adapter.channel for adapter in adapters} - declared)
    if undeclared:
        msg = (
            f"pack {pack.manifest.id!r} does not enable {undeclared}; its pack.yaml declares "
            f"{sorted(declared)}"
        )
        raise ConfigError(msg)


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
    handoff: "HandoffService | None" = None
    """The desk side of DESIGN.md section 13: what built and delivered each packet. Held so a
    test - and, later, an operator endpoint - can ask what this replica has queued and what a
    sink refused."""

    send_failures: list[str] = field(default_factory=list)
    """Deliveries a transport refused, most recent last, capped at :data:`SEND_FAILURES_KEPT`.

    Phase 7 replaces this with the metrics and structured logs of DESIGN.md section 15; until
    then it is what makes a dropped delivery visible at all rather than silent. Capped because a
    service runs for weeks and an unbounded list of every failed delivery is a leak."""

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

    def pool_pressure(self) -> tuple[int, int]:
        """Connections checked out, and the most this process will ever hold.

        A capacity of ``0`` means this engine does not pool at all - ``NullPool``, which the test
        fixtures use - and there is nothing to be full of.
        """
        return connections_in_use(self.engine), pool_capacity(self.engine)

    async def database_health(self) -> str:
        """``"ok"``, or a short phrase saying what is wrong. Never raises, never queues.

        Security review finding S2 measured this endpoint at **28.5 seconds** during a flood,
        because it asked the same exhausted pool the turns had taken for a connection and then
        waited out the checkout timeout. An orchestrator reads a health check that slow as a dead
        instance and restarts it, which loses every turn in flight and adds the restarted
        replica's reconnects to the load - so a health check that hangs is worse than one that
        says "degraded".

        Two things stop it now. The pool is sized with a reserve the turns cannot reach
        (:data:`~support_core.storage.config.RESERVE_CONNECTIONS`), because they are bounded by
        :class:`~support_core.engine.executor.TurnSlots`; and if the pool is full anyway - some
        other caller, or a database that has stopped answering and is holding every connection
        open - this asks the pool how many connections are out **before** asking for one, and
        answers from that instead of joining the queue. Counting is free; queueing is what took
        28.5 seconds.
        """
        in_use, capacity = self.pool_pressure()
        if capacity and in_use >= capacity:
            return f"saturated: {in_use} of {capacity} connections are in use"
        try:
            async with self.engine.connect() as connection:
                await connection.execute(sql_text("SELECT 1"))
        except SQLTimeoutError:
            # Lost the race between the count above and the checkout. Bounded by
            # `pool_timeout`, which is five seconds rather than SQLAlchemy's thirty.
            return "saturated: no connection was free"
        except Exception as exc:  # a health endpoint reports the failure, it does not raise
            return f"unavailable: {type(exc).__name__}"
        return "ok"

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
        # Any connection that named this channel key before the conversation existed is moved
        # onto it now, *before* the turn runs, so it is watching in time for the turn's own
        # messages (review finding W8: a socket no longer creates a conversation by connecting).
        # Two tabs opened together on a fresh key, and a tab watching while the first message
        # arrives by POST, are both this case. A channel with no live connections - phase 7's
        # email - promotes nothing and pays a dictionary lookup.
        self.connections.attach(inbound.conversation_key, conversation.id)
        outcome = await self.executor.on_inbound(conversation.id, inbound.text)
        if outcome.queued:
            self.drainer.submit(conversation.id)
        else:
            # Every turn is announced to everyone watching the conversation, not to whoever
            # happened to cause it (review finding W3). The ``turn`` frame is the only thing that
            # carries `status` and `awaiting`, so it is the only thing that raises the approval
            # panel: pushing it to one connection meant a second tab - and any customer whose
            # reply arrived on another transport - saw the refund proposal with no panel and a
            # stale status pill, which is the one moment this phase exists to show.
            await self.announce(conversation.id)
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

    async def conversation_if_known(self, channel: Channel, key: str) -> ConversationRef | None:
        """The conversation for a key, or ``None`` - creating nothing (review finding W8).

        What a transport that connects before it speaks should ask. Opening a socket used to
        create a durable ``conversation`` and ``run`` before the customer had typed anything,
        from an unauthenticated endpoint with no rate limit, so a loop of connects filled two
        tables; phase 7's email adapter has the same shape for bounce and delivery-receipt
        webhooks. A conversation is created by somebody saying something.
        """
        self.hub.adapter(channel)
        return await self.hub.find(channel, key)

    async def say(self, conversation_id: uuid.UUID, text: str, *, author: str = "agent") -> None:
        """Put a message into a conversation from outside a turn (DESIGN.md sections 12, 13).

        The human desk's ``reply``, and nothing else so far. It takes the same route a node's
        message does - written durably first, delivered afterwards - so a human's answer is in
        the transcript whether or not anybody is connected, and reaches a live socket if
        somebody is.

        Not a turn: no lock, no run, no checkpoint. A person typing into a parked conversation
        is not the engine executing anything, and making it a turn would resume a graph that is
        deliberately waiting.
        """
        async with self.executor.sessions() as session, session.begin():
            await repo.add_outbound(
                session, conversation_id=conversation_id, text_=text, author=author
            )
        await self.executor.deliver_pending(conversation_id)

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
                awaiting=self._awaiting_with_form(
                    AwaitingSummary.of(run.awaiting if run is not None else None), run
                ),
                history=[
                    {
                        "author": row.author,
                        "text": row.text,
                        "pending": row.status == "pending",
                    }
                    for row in rows
                ],
            )

    def _awaiting_with_form(
        self, awaiting: AwaitingSummary | None, run: Run | None
    ) -> AwaitingSummary | None:
        """Attach the form the waiting node declares, if it has one (DESIGN.md section 12).

        Rendering is a graph concern: the node that suspends carries its own ``form`` (see
        :mod:`support_core.graph.nodes`), and the run's frame stack says which graph that node is
        in. A node with no form is unchanged, so a pack that declares none pays a lookup and
        nothing else.
        """
        if awaiting is None or awaiting.node is None or run is None:
            return awaiting
        graph_id = self._suspended_graph_id(run)
        graph = self.pack.graphs.get(graph_id) if graph_id else None
        node = graph.nodes.get(awaiting.node) if graph else None
        form = getattr(node, "form", None)
        if form is None:
            return awaiting
        return awaiting.model_copy(update={"form": form})

    @staticmethod
    def _suspended_graph_id(run: Run) -> str | None:
        """The graph of the frame the run suspended in, addressed by ``awaiting.frame_seq``."""
        frame_seq = (run.awaiting or {}).get("frame_seq")
        frames = run.frames or []
        for frame in frames:
            if frame.get("frame_seq") == frame_seq:
                graph_id = frame.get("graph_id")
                return graph_id if isinstance(graph_id, str) else None
        if frames:
            graph_id = frames[-1].get("graph_id")
            return graph_id if isinstance(graph_id, str) else None
        return None


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

    turn_limit = config.max_concurrent_turns
    resolved = config.resolve_provider(env)
    model_provider = provider if provider is not None else build_provider(config, resolved)
    if provider is not None:
        resolved = provider.name

    service = (
        service_for_pack(
            pack,
            model_provider,
            default_model=config.models.default,
            escalation_model=config.models.escalation,
        )
        if model_provider is not None
        else None
    )
    engine_hooks = hooks or EngineHooks()
    if service is not None:
        engine_hooks.extract_slots = StructuredSlotExtractor(service)
        engine_hooks.confirm_decision = StructuredConfirmClassifier(service)
        engine_hooks.summarize = LlmSummarizer(
            service, max_chars=pack.manifest.memory.max_summary_chars
        )
        # DESIGN.md section 6.6's interrupt check and its return offer. The intents come from the
        # pack's own root graph, so a deployment cannot widen what a customer may switch to.
        engine_hooks.interrupt_check = StructuredInterruptCheck(
            service, [intent.as_tuple() for intent in workflow_intents(pack)]
        )
        engine_hooks.resume_offer = StructuredResumeOffer(service)

    owns_engine = engine is None
    # The pool is sized from the turn bound rather than left at SQLAlchemy's default, and the
    # two are chosen together here so they cannot drift apart (security review finding S2).
    # `pool_settings` shows the arithmetic; `TurnSlots` is what keeps the turns inside it.
    db = engine if engine is not None else make_engine(pool=pool_settings(turn_limit))
    sessions = make_session_factory(db)
    executor = Executor(
        pack,
        db,
        hooks=engine_hooks,
        lock_wait_seconds=config.lock_wait_seconds,
        max_concurrent_turns=turn_limit,
        llm=service,
        # DESIGN.md section 9.1, from the pack's own `knowledge/sources.yaml`. `None` for a pack
        # that declares nothing, which is a real configuration and not a failure: layer 7 is then
        # empty and section 9.2's citation guardrail is what stops that becoming a guess.
        retriever=build_retriever(
            pack.knowledge,
            sessions,
            store=build_store(config.qdrant_url) if config.use_qdrant else None,
            use_qdrant=config.use_qdrant,
        ),
    )
    # DESIGN.md section 13's "universal fallback for every failure path in section 7.3", filled.
    # One service for the ``handoff`` node and for the engine's own routing, so a conversation
    # that fell over on the way to a handoff produces the same packet as one that reached it.
    handoff = HandoffService(
        pack,
        executor.sessions,
        sink=default_sink(executor.sessions, config.handoff_webhook_url),
        llm=service,
        clock=engine_hooks.clock,
        transcript_template=config.transcript_url_template,
        # The same retriever the engine holds, so a packet grounded on the customer's last
        # message rests on the corpus the conversation itself was answered from.
        retriever=executor.retriever,
    )
    engine_hooks.handoff = handoff

    connections = ConnectionRegistry()
    chosen = list(adapters) if adapters is not None else [WebChatAdapter(connections)]
    _check_channels(pack, chosen)
    for adapter in chosen:
        registry = getattr(adapter, "connections", None)
        if isinstance(registry, ConnectionRegistry):
            connections = registry
    failures: list[str] = []

    def note_failure(
        conversation: ConversationRef, message: OutboundMessage, error: Exception
    ) -> None:
        failures.append(f"{conversation.id}: {type(error).__name__}: {error}")
        del failures[:-SEND_FAILURES_KEPT]

    hub = ChannelHub(executor, chosen, on_send_error=note_failure)
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
        handoff=handoff,
        send_failures=failures,
    )
