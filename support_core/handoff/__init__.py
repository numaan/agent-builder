"""Human handoff. Implements DESIGN.md section 13, and the "otherwise handoff" of section 7.3.

    The `handoff` node builds a `HandoffPacket` ... The packet is pushed to the queue named in
    `pack.yaml` through a `HandoffSink` (core ships a webhook sink and a Postgres queue sink;
    packs can add Zendesk, Intercom, and so on). The human desk API lets the human reply
    directly, take over fully (`close`), or hand back (`resume` with optional state patch ...).
    On `resume`, the graph continues from the handoff node's `resumed` edge.

    Handoff is also the universal fallback for every failure path in section 7.3.

Four modules and one idea: a handoff is a *packet*, not a transcript, and it is built from
durable state so that it is complete even when the process that would have remembered it died.

* :mod:`~support_core.handoff.packet` - the shape section 13 gives, field for field.
* :mod:`~support_core.handoff.builder` - reading one out of the database.
* :mod:`~support_core.handoff.sinks` - where it goes: a Postgres queue, a webhook, or both.
* :mod:`~support_core.handoff.service` - the one object the ``handoff`` node and the engine's
  failure routing both use, so a conversation that failed on the way to a handoff node and one
  that reached it produce the same packet.
"""

from support_core.handoff.builder import PacketRequest, fallback_summary
from support_core.handoff.packet import ActionRecord, CustomerRef, HandoffPacket, Passage
from support_core.handoff.service import (
    DEFAULT_TRANSCRIPT_URL,
    HandoffService,
    no_handoff_service,
)
from support_core.handoff.sinks import (
    CompositeSink,
    HandoffSink,
    NullSink,
    PostgresQueueSink,
    SinkError,
    WebhookSink,
    default_sink,
)

__all__ = [
    "DEFAULT_TRANSCRIPT_URL",
    "ActionRecord",
    "CompositeSink",
    "CustomerRef",
    "HandoffPacket",
    "HandoffService",
    "HandoffSink",
    "NullSink",
    "PacketRequest",
    "Passage",
    "PostgresQueueSink",
    "SinkError",
    "WebhookSink",
    "default_sink",
    "fallback_summary",
    "no_handoff_service",
]
