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
    """The rolling conversation summary of section 10, rewritten every K turns."""

    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Turns begun on this conversation. Incremented in the transaction that starts the turn,
    so a process that dies mid-turn cannot lose or double-count one (section 10's "every K
    turns" needs a K that survives a crash)."""

    summary_turn: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """The ``turn_count`` the stored ``summary`` covers. The pair is written together, so a
    crash leaves them consistent and the next due turn simply summarises again."""

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
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Position among the messages written by one checkpoint transaction.

    ``created_at`` is the transaction timestamp, so every message a single node produced shares
    it and cannot order them; ``ORDER BY created_at, ordinal, id`` can (phase 2 review finding
    R4). Successive checkpoints get increasing timestamps, so the ordinal only ever breaks a
    tie within one of them."""


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

    turn_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Model-loop tool calls made in the current turn, for ``max_tool_calls_per_turn``
    (section 5.1, which says *per turn*; review finding V6 found the limit being applied per
    node, so three tool-using ``llm`` nodes in one turn could make three times it). A column
    beside ``turn_nodes`` for the same reason: a limit counted in memory resets when a process
    dies mid-turn."""

    next_frame_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """The next value of ``Frame.frame_seq``. Monotonic and never reused, because ``frame_seq``
    is the second field of the step id (section 7.1): a graph invoked twice must not produce
    the same step ids twice."""

    suspended_at: Mapped[datetime | None] = mapped_column()
    timeout_at: Mapped[datetime | None] = mapped_column()
    """When the per-status timeout of section 7.2 expires; ``None`` means never."""

    awaiting: Mapped[JsonObject | None] = mapped_column()
    """What the run is waiting for, for example ``{"kind": "async_tool", "step_id": ...}``."""

    recovery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    """Failed passes of the recovery sweep over this run (section 7.3, review finding R3).

    A conversation core cannot recover - an unexecutable node type, a hook that raises - is
    parked for a human after a few attempts instead of being retried by every sweep for ever.
    Reset when a sweep gets through the turn."""

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

    See section 8.2. Beyond the design's columns this records *where* the approval was given -
    the run, the frame, the confirm node and its step - and whether it has been used.

    The extra binding is what makes an approval un-replayable. ``frame_seq`` is monotonic and
    never reused (section 7.1's step id depends on that), so an approval given in an earlier
    invocation of the same graph does not match a later one however identical the arguments;
    ``consumed_at`` then makes even the right approval good for exactly one call (phase-0 review
    finding N1). ``uq_action_approval_run_step`` keeps a confirm node whose step re-executes
    after a crash from recording a second approval.
    """

    __tablename__ = "action_approval"
    __table_args__ = (
        Index("ix_action_approval_conversation_hash", "conversation_id", "args_hash"),
        UniqueConstraint("run_id", "step_id", name="uq_action_approval_run_step"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), index=True
    )
    frame_seq: Mapped[int | None] = mapped_column(Integer)
    node_id: Mapped[str | None] = mapped_column()
    """The ``confirm`` node that recorded it; a ``tool`` node's ``requires_approval`` names it."""

    step_id: Mapped[str | None] = mapped_column()
    tool: Mapped[str] = mapped_column(nullable=False)
    args: Mapped[JsonObject | None] = mapped_column()
    """The canonical arguments the hash was taken over, so an auditor need not reverse it."""

    args_hash: Mapped[str] = mapped_column(nullable=False)
    approved_by: Mapped[str] = mapped_column(nullable=False)
    approved_at: Mapped[datetime] = mapped_column(nullable=False, server_default=_NOW)
    consumed_by_tool_call_id: Mapped[uuid.UUID | None] = mapped_column(
        # ``use_alter``: ``tool_call.approval_id`` points back here, so the two tables are a
        # cycle. It is a hint for ``create_all`` only - the migrations are the schema - but
        # without it ``Base.metadata.sorted_tables`` cannot order the tables, and the test
        # fixtures truncate that list.
        ForeignKey("tool_call.id", ondelete="SET NULL", use_alter=True)
    )
    consumed_at: Mapped[datetime | None] = mapped_column()
    """Set in the same transaction as the call it authorised. A consumed approval is absent."""


class ToolCall(Base):
    """Every tool invocation, keyed by the idempotency key derived from the step id.

    See sections 7.1, 8.1 and 17. The row is written *before* the tool runs and updated after,
    which is what gives a non-idempotent tool at-most-once semantics: a row left ``running`` by
    a process that died is a call whose outcome nobody knows, and the runtime refuses to repeat
    it rather than guessing (section 8.1, ``idempotent: bool``).
    """

    __tablename__ = "tool_call"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_tool_call_idempotency_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    idempotency_key: Mapped[str] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str | None] = mapped_column()
    node_id: Mapped[str | None] = mapped_column()
    tool: Mapped[str] = mapped_column(nullable=False)
    args: Mapped[JsonObject] = mapped_column(nullable=False, server_default=_EMPTY_OBJECT)
    result: Mapped[JsonObject | None] = mapped_column()
    context_patch: Mapped[JsonObject | None] = mapped_column()
    """The ``ctx`` change this call asked for, replayed with the result (section 19 step 9)."""

    risk: Mapped[str] = mapped_column(nullable=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("action_approval.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(nullable=False, server_default="pending")
    """``running``, ``succeeded``, ``failed``, ``awaiting_callback`` or ``indeterminate``."""

    error: Mapped[str | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column()
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
