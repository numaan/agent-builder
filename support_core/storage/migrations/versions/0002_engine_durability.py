"""Engine durability columns: trace step ordering and run suspension bookkeeping.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05

Phase 2 (BACKLOG.md), DESIGN.md sections 7.1 to 7.3 and 17.

* ``trace_step.seq`` with a unique ``(run_id, seq)`` is the total order per run that replay
  (section 7.3) needs. ``started_at`` cannot provide it: it defaults to ``now()``, which is
  transaction start, so every step written in one transaction shares a value (phase 0 review
  finding F5). The existing ``ix_trace_step_run_started`` index is replaced by the unique
  constraint's index, which is the order replay actually reads.
* ``trace_step.error`` records a node failure (section 7.3) so replay can show it.
* ``run`` gains the suspension bookkeeping of section 7.2 (``suspended_at``, ``timeout_at``,
  ``awaiting``), the per-turn node counter of section 7.3 (``turn_nodes``, in a column so the
  limit survives a crash mid-turn), and the pack pin of section 6.7 (``pack_fingerprint``).
* ``uq_run_conversation``: one run per conversation, which is what keeps a conversation's step
  ids stable across turns. Phase 0 left the question open for phase 2.

The ``seq`` backfill uses ``row_number()`` so the migration applies to a table that already has
rows; ``conversation_id`` is de-duplicated before the unique constraint for the same reason.
Nothing has run the engine yet, so both are precautions rather than data migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("trace_step", sa.Column("seq", sa.Integer(), nullable=True))
    op.add_column("trace_step", sa.Column("error", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE trace_step AS t
        SET seq = ordered.rn
        FROM (
            SELECT id, row_number() OVER (PARTITION BY run_id ORDER BY started_at, id) AS rn
            FROM trace_step
        ) AS ordered
        WHERE t.id = ordered.id
        """
    )
    op.alter_column("trace_step", "seq", nullable=False)
    op.drop_index("ix_trace_step_run_started", table_name="trace_step")
    op.create_unique_constraint("uq_trace_step_run_seq", "trace_step", ["run_id", "seq"])

    op.add_column("run", sa.Column("pack_fingerprint", sa.Text(), nullable=True))
    op.add_column("run", sa.Column("turn_nodes", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("run", sa.Column("suspended_at", _TS, nullable=True))
    op.add_column("run", sa.Column("timeout_at", _TS, nullable=True))
    op.add_column("run", sa.Column("awaiting", postgresql.JSONB(), nullable=True))
    op.create_index("ix_run_timeout_at", "run", ["timeout_at"])

    # One run per conversation from now on. Keep the oldest if history ever holds more.
    op.execute(
        """
        DELETE FROM run AS r
        USING run AS other
        WHERE r.conversation_id = other.conversation_id
          AND (other.updated_at, other.id) < (r.updated_at, r.id)
        """
    )
    op.drop_index("ix_run_conversation_id", table_name="run")
    op.create_unique_constraint("uq_run_conversation", "run", ["conversation_id"])


def downgrade() -> None:
    op.drop_constraint("uq_run_conversation", "run", type_="unique")
    op.create_index("ix_run_conversation_id", "run", ["conversation_id"])
    op.drop_index("ix_run_timeout_at", table_name="run")
    for column in ("awaiting", "timeout_at", "suspended_at", "turn_nodes", "pack_fingerprint"):
        op.drop_column("run", column)

    op.drop_constraint("uq_trace_step_run_seq", "trace_step", type_="unique")
    op.create_index("ix_trace_step_run_started", "trace_step", ["run_id", "started_at"])
    op.drop_column("trace_step", "error")
    op.drop_column("trace_step", "seq")
