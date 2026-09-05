"""SQLAlchemy 2 declarative models for every table in DESIGN.md section 17.

The Alembic migration under ``migrations/versions`` is the source of truth for the deployed
schema; these models must match it exactly, which ``tests/test_migrations.py`` enforces by
diffing the live schema against ``Base.metadata``.

Deliberate choices (see reviews/phase-0.md "Plan"):

* Enum-like columns (``status``, ``direction``, ``author``, ``risk``, ``approved_by``) are
  ``text``. Their vocabularies belong to phases 2, 4 and 6.
* ``doc_chunk.embedding`` is an untyped ``vector``; phase 5 fixes the dimension and adds the
  ANN index once the embedding model is chosen.
* ``doc_chunk.tsv`` is a stored generated column so full-text search can never drift from
  the chunk text.
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonObject = dict[str, Any]
JsonArray = list[Any]

_EMPTY_OBJECT = sql_text("'{}'::jsonb")
_EMPTY_ARRAY = sql_text("'[]'::jsonb")
_NOW = sql_text("now()")
_NEW_UUID = sql_text("gen_random_uuid()")


class Base(DeclarativeBase):
    """Declarative base with Postgres-native type mapping."""

    type_annotation_map = {
        str: Text(),
        datetime: DateTime(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
        JsonObject: JSONB(),
        JsonArray: JSONB(),
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=_NEW_UUID)


def _created_at() -> Mapped[datetime]:
    return mapped_column(nullable=False, server_default=_NOW)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(nullable=False, server_default=_NOW, onupdate=func.now())


class Conversation(Base):
    """One customer conversation on one channel (section 17; section 10 "Customer context")."""

    __tablename__ = "conversation"

    id: Mapped[uuid.UUID] = _uuid_pk()
    channel: Mapped[str] = mapped_column(nullable=False)
    customer_ref: Mapped[str | None] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(nullable=False, server_default="open")
    context: Mapped[JsonObject] = mapped_column(nullable=False, server_default=_EMPTY_OBJECT)
    summary: Mapped[str | None] = mapped_column()
    created_at: Mapped[datetime] = _created_at()
    closed_at: Mapped[datetime | None] = mapped_column()


class Message(Base):
    """Inbound and outbound messages.

    ``status = pending`` queues messages that arrive while the conversation is locked
    (section 17 "Concurrency").
    """

    __tablename__ = "message"
    __table_args__ = (Index("ix_message_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(nullable=False)
    author: Mapped[str] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(nullable=False)
    redacted_text: Mapped[str | None] = mapped_column()
    status: Mapped[str] = mapped_column(nullable=False, server_default="received")
    created_at: Mapped[datetime] = _created_at()


class Run(Base):
    """Durable execution state: the frame stack and checkpoint sequence (sections 7.1, 17).

    One run per conversation, for the life of the conversation: DESIGN.md section 7.1 says
    "load Run" for the conversation being locked, and keeping the id stable is what keeps the
    ``run_id:frame_seq:node_id:attempt`` step ids of section 7.1 stable across turns. A run
    that reaches the end of its root frame goes to ``done`` and the next inbound message pushes
    a fresh root frame onto the same run (phase 0 review left this open for phase 2).

    Columns beyond section 17's list carry the suspension bookkeeping of section 7.2 and the
    per-turn limit of section 7.3, which have to survive a crash like everything else.
    """

    __tablename__ = "run"
    __table_args__ = (
        UniqueConstraint("conversation_id", name="uq_run_conversation"),
        Index("ix_run_timeout_at", "timeout_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    pack_version: Mapped[str] = mapped_column(nullable=False)
    pack_fingerprint: Mapped[str | None] = mapped_column()
    """``PackPin.fingerprint`` the run started on (section 6.7); ``None`` for a run that has
    never executed a node."""

    status: Mapped[str] = mapped_column(nullable=False, server_default="idle")
    frames: Mapped[JsonArray] = mapped_column(nullable=False, server_default=_EMPTY_ARRAY)
    checkpoint_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    turn_nodes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Nodes executed in the current turn, for ``max_nodes_per_turn`` (section 7.3)."""

    suspended_at: Mapped[datetime | None] = mapped_column()
    timeout_at: Mapped[datetime | None] = mapped_column()
    """When the per-status timeout of section 7.2 expires; ``None`` means never."""

    awaiting: Mapped[JsonObject | None] = mapped_column()
    """What the run is waiting for, for example ``{"kind": "async_tool", "step_id": ...}``."""

    updated_at: Mapped[datetime] = _updated_at()


class TraceStep(Base):
    """One node execution.

    ``step_id`` is the deterministic ``run_id:frame_seq:node_id:attempt`` key from
    section 7.1 and is unique. ``seq`` is the run's ``checkpoint_seq`` at write time and gives
    replay (section 7.3) a total order per run: ``started_at`` cannot, because it is a
    timestamp and several steps can share one (phase 0 review finding F5).
    """

    __tablename__ = "trace_step"
    __table_args__ = (
        UniqueConstraint("step_id", name="uq_trace_step_step_id"),
        UniqueConstraint("run_id", "seq", name="uq_trace_step_run_seq"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(nullable=False)
    edge: Mapped[str | None] = mapped_column()
    state_patch: Mapped[JsonObject] = mapped_column(nullable=False, server_default=_EMPTY_OBJECT)
    llm_response: Mapped[JsonObject | None] = mapped_column()
    error: Mapped[str | None] = mapped_column()
    """The failure this step recorded, if it failed (section 7.3)."""

    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=_NOW)
    """Written from the engine's clock, never left to the server default: ``now()`` is
    transaction start, so two steps in one transaction would share it (finding F5)."""

    ended_at: Mapped[datetime | None] = mapped_column()


class ActionApproval(Base):
    """Customer or human approval bound to ``sha256(tool_name + canonical_json(args))``.

    See section 8.2.
    """

    __tablename__ = "action_approval"
    __table_args__ = (
        Index("ix_action_approval_conversation_hash", "conversation_id", "args_hash"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(nullable=False)
    args_hash: Mapped[str] = mapped_column(nullable=False)
    approved_by: Mapped[str] = mapped_column(nullable=False)
    approved_at: Mapped[datetime] = mapped_column(nullable=False, server_default=_NOW)


class ToolCall(Base):
    """Every tool invocation, keyed by the idempotency key derived from the step id.

    See sections 7.1, 8.1 and 17.
    """

    __tablename__ = "tool_call"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_tool_call_idempotency_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    idempotency_key: Mapped[str] = mapped_column(nullable=False)
    tool: Mapped[str] = mapped_column(nullable=False)
    args: Mapped[JsonObject] = mapped_column(nullable=False, server_default=_EMPTY_OBJECT)
    result: Mapped[JsonObject | None] = mapped_column()
    risk: Mapped[str] = mapped_column(nullable=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("action_approval.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(nullable=False, server_default="pending")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Handoff(Base):
    """A handoff packet waiting for, or resolved by, a human (section 13)."""

    __tablename__ = "handoff"
    __table_args__ = (Index("ix_handoff_queue_status", "queue", "status"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True
    )
    packet: Mapped[JsonObject] = mapped_column(nullable=False)
    queue: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False, server_default="open")
    human_id: Mapped[str | None] = mapped_column()
    created_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None] = mapped_column()


class CustomerMemory(Base):
    """Durable per-customer notes across conversations (section 10). Never identity."""

    __tablename__ = "customer_memory"

    customer_ref: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[JsonObject] = mapped_column(nullable=False)
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = _updated_at()


class DocSource(Base):
    """A knowledge document source from ``knowledge/sources.yaml`` (section 9.1).

    ``version`` is the current ``source_version``; chunks carry the version they were built
    from, so an old trace can still name the version it used (section 9.2).
    """

    __tablename__ = "doc_source"

    id: Mapped[str] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(nullable=False)
    config: Mapped[JsonObject] = mapped_column(nullable=False, server_default=_EMPTY_OBJECT)
    version: Mapped[str] = mapped_column(nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = _created_at()


class DocChunk(Base):
    """A retrievable chunk with pgvector embedding and full-text vector (sections 9.1, 9.3)."""

    __tablename__ = "doc_chunk"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "source_version", "chunk_index", name="uq_doc_chunk_source_version_index"
        ),
        Index("ix_doc_chunk_tsv", "tsv", postgresql_using="gin"),
        Index("ix_doc_chunk_source_stale", "source_id", "stale"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[str] = mapped_column(
        ForeignKey("doc_source.id", ondelete="CASCADE"), nullable=False
    )
    source_version: Mapped[str] = mapped_column(nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    locator: Mapped[str] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector())
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, text)", persisted=True)
    )
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    created_at: Mapped[datetime] = _created_at()


class KgEntity(Base):
    """Knowledge graph entity (section 9.1, ``KnowledgeGraphRetriever``)."""

    __tablename__ = "kg_entity"
    __table_args__ = (UniqueConstraint("entity_type", "name", name="uq_kg_entity_type_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_type: Mapped[str] = mapped_column(nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    attributes: Mapped[JsonObject] = mapped_column(nullable=False, server_default=_EMPTY_OBJECT)
    source_id: Mapped[str | None] = mapped_column()
    source_version: Mapped[str | None] = mapped_column()
    created_at: Mapped[datetime] = _created_at()


class KgRelation(Base):
    """Knowledge graph relation ``subject -predicate-> object`` (section 9.1)."""

    __tablename__ = "kg_relation"
    __table_args__ = (
        UniqueConstraint("subject_id", "predicate", "object_id", name="uq_kg_relation_triple"),
        Index("ix_kg_relation_object", "object_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kg_entity.id", ondelete="CASCADE"), nullable=False, index=True
    )
    predicate: Mapped[str] = mapped_column(nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kg_entity.id", ondelete="CASCADE"), nullable=False
    )
    attributes: Mapped[JsonObject] = mapped_column(nullable=False, server_default=_EMPTY_OBJECT)
    source_id: Mapped[str | None] = mapped_column()
    source_version: Mapped[str | None] = mapped_column()
    created_at: Mapped[datetime] = _created_at()


class EvalRun(Base):
    """Results of one eval suite run against one pack version (section 16)."""

    __tablename__ = "eval_run"

    id: Mapped[uuid.UUID] = _uuid_pk()
    pack_version: Mapped[str] = mapped_column(nullable=False)
    suite: Mapped[str] = mapped_column(nullable=False)
    results: Mapped[JsonObject] = mapped_column(nullable=False, server_default=_EMPTY_OBJECT)
    created_at: Mapped[datetime] = _created_at()


ALL_TABLES = tuple(Base.metadata.sorted_tables)
"""Every mapped table in dependency order; the test fixtures truncate these between tests."""
