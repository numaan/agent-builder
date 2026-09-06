"""One object that turns "give up" into a packet on a queue. Implements DESIGN.md section 13.

    Handoff is also the universal fallback for every failure path in section 7.3.
    - DESIGN.md section 13

Which is why there is one of these and not two. The ``handoff`` node and the engine's failure
routing want exactly the same thing to happen - build a packet from durable state, write the
summary with the escalation model, push it to the pack's queue - and phase 2 deliberately left
the second of those as a hook with a do-nothing default so that this phase would have one place
to attach. Filling that seam twice, once for the node and once for the ladder, would give a
conversation that failed *inside* a handoff node a different packet from one that failed on the
way to it.

:class:`HandoffService` therefore is the ``EngineHooks.handoff`` implementation *and* what the
``handoff`` node reaches through its ``NodeRuntime``. The engine passes it what only the engine
knows (the frame stack, the reason, the step); everything else it reads back itself.

**It never raises into a turn.** A run is parked ``waiting_human`` and checkpointed before this
is called, so the customer is already waiting for a person whatever happens here. A summariser
that is down falls back to a factual line; a sink that is down is recorded in
:attr:`HandoffService.failures` and the packet is still built. Letting either take the turn down
would turn "we could not tell anyone" into "we also lost the conversation".
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from support_core.graph.pack import Pack
from support_core.handoff.builder import PacketRequest, assemble, fallback_summary, gather
from support_core.handoff.packet import HandoffPacket
from support_core.handoff.sinks import HandoffSink, NullSink, transcript_url
from support_core.llm.prompt import TranscriptMessage
from support_core.llm.service import HandoffSummaryRequest, LlmService

if TYPE_CHECKING:  # pragma: no cover - type-only, for the reason given in builder.py
    from support_core.engine.hooks import HandoffRequest


def utc_now() -> datetime:
    """The clock this service stamps a packet with, when the engine does not supply one."""
    return datetime.now(UTC)


DEFAULT_TRANSCRIPT_URL = "/desk/conversations/{conversation_id}/transcript"
"""Where :attr:`~support_core.handoff.packet.HandoffPacket.transcript_url` points by default.

This service's own desk endpoint, so the link in a packet works out of the box. A deployment
behind a hostname sets :attr:`HandoffService.transcript_template` to an absolute URL; what it
must not be is a plausible address that returns 404, which is what a fabricated link is.
"""

SUMMARY_FAILURES_KEPT = 50


class HandoffService:
    """Build a handoff packet and deliver it (DESIGN.md sections 7.3, 13)."""

    def __init__(
        self,
        pack: Pack,
        sessions: Callable[[], Any],
        *,
        sink: HandoffSink | None = None,
        llm: LlmService | None = None,
        clock: Callable[[], datetime] = utc_now,
        transcript_template: str = DEFAULT_TRANSCRIPT_URL,
    ) -> None:
        self.pack = pack
        self.sessions = sessions
        self.sink = sink or NullSink()
        self.llm = llm
        self.clock = clock
        self.transcript_template = transcript_template
        self.delivered: list[HandoffPacket] = []
        """Packets this process built, newest last. For tests and for the desk's own view of what
        this replica has done; the durable record is the sink's."""

        self.failures: list[str] = []
        """Deliveries a sink would not take. Phase 7's metrics replace this; until then it is
        what makes "nobody was told" visible rather than silent."""

    # -- the EngineHooks.handoff seam -----------------------------------------------------

    async def __call__(self, request: "HandoffRequest") -> None:
        """What phase 2 left as ``hooks.handoff``, filled (DESIGN.md section 7.3).

        Every failure the engine routes - a limit, a node error, a timeout, a pack it cannot
        run, a recovery it gave up on - arrives here with the reason the engine gave it, and
        leaves as a packet carrying that reason.
        """
        await self.raise_handoff(
            PacketRequest(
                conversation_id=request.conversation_id,
                run_id=request.run_id,
                reason=request.reason,
                detail=request.detail,
                frames=request.frames,
                node_id=request.node_id,
                step_id=request.step_id,
                suggested_next_steps=list(request.next_steps),
            )
        )

    # -- the whole job --------------------------------------------------------------------

    async def raise_handoff(self, request: PacketRequest) -> HandoffPacket:
        """Build the packet, deliver it, and return it. Never raises."""
        packet = await self.build(request)
        await self.deliver(packet)
        return packet

    async def build(self, request: PacketRequest) -> HandoffPacket:
        """DESIGN.md section 13's packet, from durable state plus one model call."""
        actions, pending, context, transcript = await gather(self.sessions, request)
        window = [TranscriptMessage(author=author, text=text) for author, text in transcript]
        summary = await self._summary(request, actions, pending, context, window)
        return assemble(
            request,
            actions=actions,
            pending=pending,
            context=context,
            summary=summary,
            queue=self.pack.manifest.handoff.queue,
            sla_minutes=self.pack.manifest.handoff.sla_minutes,
            transcript_url=transcript_url(self.transcript_template, request.conversation_id),
            now=self.clock(),
        )

    async def deliver(self, packet: HandoffPacket) -> None:
        """Push a packet at the sink, recording rather than raising a refusal."""
        self.delivered.append(packet)
        try:
            await self.sink.deliver(packet)
        except Exception as exc:
            self.failures.append(f"{packet.conversation_id}: {type(exc).__name__}: {exc}")
            del self.failures[:-SUMMARY_FAILURES_KEPT]

    async def _summary(
        self,
        request: PacketRequest,
        actions: Sequence[Any],
        pending: Any,
        context: Any,
        window: Sequence[TranscriptMessage],
    ) -> str:
        """The escalation model's summary, or the facts (see the module docstring)."""
        if self.llm is None:
            return fallback_summary(request.reason, request.detail, list(actions))
        frames = list(request.frames)
        top = frames[-1] if frames else None
        try:
            return await self.llm.write_handoff_summary(
                HandoffSummaryRequest(
                    reason=request.reason,
                    workflow=top.graph_id if top is not None else "",
                    node=request.node_id or (top.node_id if top is not None else ""),
                    identity_verified=bool(
                        context.customer.identity_verified if context is not None else False
                    ),
                    actions_taken=[action.one_line() for action in actions],
                    pending_action=pending.one_line() if pending is not None else None,
                    detail=request.detail,
                    state=dict(top.state) if top is not None else {},
                    window=window,
                    summary=context.summary if context is not None else None,
                    max_chars=self.pack.manifest.memory.max_summary_chars * 2,
                )
            )
        except Exception as exc:
            self.failures.append(f"summary: {type(exc).__name__}: {exc}")
            del self.failures[:-SUMMARY_FAILURES_KEPT]
            return fallback_summary(request.reason, request.detail, list(actions))


NodeHandoff = Callable[[PacketRequest], Any]
"""What a ``handoff`` node holds: one closure that builds and delivers a packet, and can do
nothing else. The same shape as :class:`~support_core.engine.runners.NodeToolAccess` and for the
same reason - a node is arbitrary Python once a pack registers one, and ``rt`` is its only route
to the outside, so what it reaches must already carry the answer to "which conversation"."""


async def no_handoff_service(request: PacketRequest) -> HandoffPacket:
    """The default a node gets when no desk is configured: a packet delivered nowhere.

    Not an error, and not a silence either. The run still parks ``waiting_human`` durably, and
    the packet exists in the trace; what does not happen is that anyone is told, and a
    deployment that has not configured a sink should be able to see that in one place.
    """
    return assemble(
        request,
        actions=[],
        pending=None,
        context=None,
        summary=fallback_summary(request.reason, request.detail, []),
        queue="",
        sla_minutes=None,
        transcript_url=transcript_url(DEFAULT_TRANSCRIPT_URL, request.conversation_id),
        now=utc_now(),
    )


__all__ = [
    "DEFAULT_TRANSCRIPT_URL",
    "HandoffService",
    "NodeHandoff",
    "no_handoff_service",
]
