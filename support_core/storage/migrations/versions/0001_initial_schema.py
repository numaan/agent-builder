"""Initial schema: every table in DESIGN.md section 17.

Revision ID: 0001
Revises: None
Create Date: 2026-09-05

Hand-written to match ``support_core.storage.models``; ``tests/test_migrations.py`` fails if the
two drift. See reviews/phase-0.md "Plan" for the column-type decisions.

**The downgrade does not drop the ``vector`` extension**, and that asymmetry is deliberate
(phase-0 review finding N2, deferred to phase 5 which owns pgvector). The upgrade creates it with
``IF NOT EXISTS``, which means it may well have been there first: ``CREATE EXTENSION`` needs
superuser on this image, so on a managed instance an administrator usually installs it once for
the whole database before the application ever runs. A ``DROP EXTENSION`` on the way down would
then remove something this migration did not create, taking every ``vector`` column in that
database - including another application's - with it, and ``downgrade base`` means "remove this
application's schema", not "remove pgvector". An extension left behind costs nothing: the next
upgrade's ``IF NOT EXISTS`` is a no-op, and a fresh database that genuinely wants it gone can
drop it in one statement that a human chose to type.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_NOW = sa.text("now()")
_EMPTY_OBJECT = sa.text("'{}'::jsonb")
_EMPTY_ARRAY = sa.text("'[]'::jsonb")


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _conversation_fk(name: str = "conversation_id", *, ondelete: str = "CASCADE") -> sa.Column[Any]:
    return sa.Column(
        name,
        _UUID,
        sa.ForeignKey("conversation.id", ondelete=ondelete),
        nullable=(ondelete == "SET NULL"),
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "conversation",
        _uuid_pk(),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("customer_ref", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("context", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("closed_at", _TS, nullable=True),
    )
    op.create_index("ix_conversation_customer_ref", "conversation", ["customer_ref"])

    op.create_table(
        "message",
        _uuid_pk(),
        _conversation_fk(),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("redacted_text", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="received"),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_index("ix_message_conversation_created", "message", ["conversation_id", "created_at"])

    op.create_table(
        "run",
        _uuid_pk(),
        _conversation_fk(),
        sa.Column("pack_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("frames", postgresql.JSONB(), nullable=False, server_default=_EMPTY_ARRAY),
        sa.Column("checkpoint_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_index("ix_run_conversation_id", "run", ["conversation_id"])

    op.create_table(
        "trace_step",
        _uuid_pk(),
        sa.Column("run_id", _UUID, sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("edge", sa.Text(), nullable=True),
        sa.Column("state_patch", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("llm_response", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("ended_at", _TS, nullable=True),
        sa.UniqueConstraint("step_id", name="uq_trace_step_step_id"),
    )
    op.create_index("ix_trace_step_run_started", "trace_step", ["run_id", "started_at"])

    op.create_table(
        "action_approval",
        _uuid_pk(),
        _conversation_fk(),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_index(
        "ix_action_approval_conversation_hash", "action_approval", ["conversation_id", "args_hash"]
    )

    op.create_table(
        "tool_call",
        _uuid_pk(),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("risk", sa.Text(), nullable=False),
        sa.Column(
            "approval_id",
            _UUID,
            sa.ForeignKey("action_approval.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
        sa.UniqueConstraint("idempotency_key", name="uq_tool_call_idempotency_key"),
    )

    op.create_table(
        "handoff",
        _uuid_pk(),
        _conversation_fk(),
        sa.Column("packet", postgresql.JSONB(), nullable=False),
        sa.Column("queue", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("human_id", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("resolved_at", _TS, nullable=True),
    )
    op.create_index("ix_handoff_conversation_id", "handoff", ["conversation_id"])
    op.create_index("ix_handoff_queue_status", "handoff", ["queue", "status"])

    op.create_table(
        "customer_memory",
        sa.Column("customer_ref", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        _conversation_fk("source_conversation_id", ondelete="SET NULL"),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
    )

    op.create_table(
        "doc_source",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("last_synced_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )

    op.create_table(
        "doc_chunk",
        _uuid_pk(),
        sa.Column(
            "source_id",
            sa.Text(),
            sa.ForeignKey("doc_source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        # Untyped vector: phase 5 fixes the dimension and adds the ANN index (reviews/phase-0.md).
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english'::regconfig, text)", persisted=True),
            nullable=True,
        ),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.UniqueConstraint(
            "source_id", "source_version", "chunk_index", name="uq_doc_chunk_source_version_index"
        ),
    )
    op.create_index("ix_doc_chunk_tsv", "doc_chunk", ["tsv"], postgresql_using="gin")
    op.create_index("ix_doc_chunk_source_stale", "doc_chunk", ["source_id", "stale"])

    op.create_table(
        "kg_entity",
        _uuid_pk(),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("source_version", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.UniqueConstraint("entity_type", "name", name="uq_kg_entity_type_name"),
    )

    op.create_table(
        "kg_relation",
        _uuid_pk(),
        sa.Column(
            "subject_id", _UUID, sa.ForeignKey("kg_entity.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column(
            "object_id", _UUID, sa.ForeignKey("kg_entity.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("source_version", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.UniqueConstraint("subject_id", "predicate", "object_id", name="uq_kg_relation_triple"),
    )
    op.create_index("ix_kg_relation_subject_id", "kg_relation", ["subject_id"])
    op.create_index("ix_kg_relation_object", "kg_relation", ["object_id"])

    op.create_table(
        "eval_run",
        _uuid_pk(),
        sa.Column("pack_version", sa.Text(), nullable=False),
        sa.Column("suite", sa.Text(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=False, server_default=_EMPTY_OBJECT),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )


def downgrade() -> None:
    for table in (
        "eval_run",
        "kg_relation",
        "kg_entity",
        "doc_chunk",
        "doc_source",
        "customer_memory",
        "handoff",
        "tool_call",
        "action_approval",
        "trace_step",
        "run",
        "message",
        "conversation",
    ):
        op.drop_table(table)
    # The `vector` extension is deliberately left in place; see the module docstring (finding N2).
