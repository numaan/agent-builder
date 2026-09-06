"""Where a packet goes. Implements DESIGN.md section 13's ``HandoffSink``.

    The packet is pushed to the queue named in `pack.yaml` through a `HandoffSink` (core ships a
    webhook sink and a Postgres queue sink; packs can add Zendesk, Intercom, and so on).
    - DESIGN.md section 13

The protocol is one method, deliberately. A sink is a transport, and the moment it can do more
than "take this packet" it starts to be somewhere business logic can hide: a sink that could
decide the queue, or edit the packet, would be a second place the pack's `handoff.queue` is
interpreted. What a pack adds is another implementation of :class:`HandoffSink`, not a hook into
this one.

Two things every sink here gets right, because getting them wrong is how a handoff silently does
not happen:

* **Delivery is at-least-once and never blocks the conversation.** The run is already parked
  ``waiting_human`` and checkpointed before a sink is called; a sink that raises is reported by
  :class:`CompositeSink` and does not undo that. A customer waiting for a person is better served
  by a parked conversation nobody was told about - which a queue sweep can still find - than by a
  turn that died trying to tell them.
* **The Postgres sink writes the same row a desk reads.** It is not a copy of the packet kept for
  audit; it *is* the queue, and :mod:`support_core.api.desk` reads exactly these rows.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from support_core.handoff.packet import HandoffPacket
from support_core.storage import repositories as repo
from support_core.storage.repositories import HandoffWrite

SessionFactory = Callable[[], Any]


@runtime_checkable
class HandoffSink(Protocol):
    """Somewhere a :class:`~support_core.handoff.packet.HandoffPacket` can be delivered."""

    name: str

    async def deliver(self, packet: HandoffPacket) -> str | None:
        """Deliver one packet. Returns a reference the sink can be asked about, if it has one."""
        ...


class NullSink:
    """Deliver nowhere, and say so.

    The default for an engine built without a desk. It is not the same as having no sink: a
    handoff still parks the run durably and is still visible in ``run.awaiting``, and this class
    is what makes "nobody was told" a fact somebody can read in a configuration rather than a
    silence.
    """

    name = "null"

    def __init__(self) -> None:
        self.delivered: list[HandoffPacket] = []

    async def deliver(self, packet: HandoffPacket) -> str | None:
        self.delivered.append(packet)
        return None


class PostgresQueueSink:
    """The queue named in ``pack.yaml``, as rows in the ``handoff`` table (section 13).

    The default sink, because it is the only one that needs nothing outside the deployment
    DESIGN.md section 4.1 describes: one service, one database. A desk polls it, an SLA sweep
    reads ``sla_due_at``, and nothing is lost if the process that wrote it dies a moment later.
    """

    name = "postgres"

    def __init__(self, sessions: SessionFactory) -> None:
        self.sessions = sessions

    async def deliver(self, packet: HandoffPacket) -> str | None:
        sla = (
            packet.created_at + timedelta(minutes=packet.sla_minutes)
            if packet.created_at is not None and packet.sla_minutes is not None
            else None
        )
        async with self.sessions() as session, session.begin():
            row = await repo.create_handoff(
                session,
                HandoffWrite(
                    conversation_id=packet.conversation_id,
                    run_id=packet.run_id,
                    packet=packet.model_dump(mode="json"),
                    queue=packet.queue or "default",
                    reason=packet.reason,
                    graph_id=packet.workflow,
                    node_id=packet.node,
                    step_id=packet.step_id,
                    sla_due_at=sla,
                ),
            )
            return str(row.id)


PostFn = Callable[[str, Mapping[str, Any]], Awaitable[Any]]


class WebhookSink:
    """POST the packet as JSON to a queue that lives somewhere else (section 13).

    ``post`` is injectable so the sink can be tested without a server, and defaults to one
    ``httpx`` call with a timeout. Deliberately no retry loop: a sink that retried inside the
    turn would hold the conversation lock while somebody else's service was down, and the packet
    is already durable wherever a :class:`PostgresQueueSink` is also configured. Retrying is a
    scheduler's job (phase 7), and until there is one, a failed webhook is reported rather than
    hidden.
    """

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        post: PostFn | None = None,
        timeout: float = 10.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._post = post or self._httpx_post

    async def _httpx_post(self, url: str, payload: Mapping[str, Any]) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=dict(payload), headers=self.headers)
            response.raise_for_status()
            return response

    async def deliver(self, packet: HandoffPacket) -> str | None:
        await self._post(self.url, packet.model_dump(mode="json"))
        return self.url


class CompositeSink:
    """Every sink gets the packet, and one failing does not stop the others.

    A deployment usually wants both: the row a desk can find whatever happens, and the webhook
    that pages somebody. If the webhook is down the row must still exist, so the failures are
    collected and re-raised as one :class:`SinkError` *after* every sink has been tried - and the
    caller (:class:`~support_core.handoff.service.HandoffService`) logs that rather than failing
    the turn, because the run is already parked.
    """

    name = "composite"

    def __init__(self, sinks: Sequence[HandoffSink]) -> None:
        self.sinks = list(sinks)

    async def deliver(self, packet: HandoffPacket) -> str | None:
        references: list[str] = []
        failures: list[str] = []
        for sink in self.sinks:
            try:
                reference = await sink.deliver(packet)
            except Exception as exc:
                failures.append(f"{sink.name}: {type(exc).__name__}: {exc}")
            else:
                if reference:
                    references.append(f"{sink.name}={reference}")
        if failures:
            raise SinkError("; ".join(failures), delivered=references)
        return ", ".join(references) or None


class SinkError(RuntimeError):
    """One or more sinks would not take the packet. The run is parked regardless."""

    def __init__(self, message: str, *, delivered: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.delivered = list(delivered)


def default_sink(sessions: SessionFactory, webhook_url: str | None = None) -> HandoffSink:
    """The Postgres queue, plus a webhook when the deployment configured one."""
    postgres: HandoffSink = PostgresQueueSink(sessions)
    if not webhook_url:
        return postgres
    return CompositeSink([postgres, WebhookSink(webhook_url)])


def transcript_url(template: str, conversation_id: uuid.UUID) -> str:
    """Render a deployment's transcript link (DESIGN.md section 13's ``transcript_url``).

    A template rather than a constant because the URL a human can open depends on where the
    service is reachable from, which is deployment configuration and not something core can know.
    The default names this service's own desk endpoint, so the link works out of the box and is
    not a plausible-looking address that 404s.
    """
    return template.format(conversation_id=conversation_id)
