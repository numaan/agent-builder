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
rows, and the unique constraint on ``conversation_id`` refuses to apply if any conversation
already has two runs. Nothing has run the engine yet, so both are precautions rather than data
migrations.

**The back-fill invents an order where finding F5 says none exists.** It orders by
``(started_at, id)``, and for the rows F5 is about - several steps written in one transaction,
so sharing one ``now()`` - the tie is broken by a random UUID. There is no better answer
available after the fact: the information that would give the true order is what this revision
adds. A run whose steps were all written in one transaction therefore gets *an* order rather
than *the* order (phase 2 review finding R13). It matters only for rows written before this
revision, and the engine has never written any.
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
    op.add_column(
        "run", sa.Column("next_frame_seq", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("run", sa.Column("suspended_at", _TS, nullable=True))
    op.add_column("run", sa.Column("timeout_at", _TS, nullable=True))
    op.add_column("run", sa.Column("awaiting", postgresql.JSONB(), nullable=True))
    op.create_index("ix_run_timeout_at", "run", ["timeout_at"])

    # One run per conversation from now on. A database that already holds more than one is a
    # database this migration must not decide about: deleting the loser cascades its trace
    # steps away (and, once phase 4 adds tool_call.run_id, its tool calls), which is a silent
    # loss of exactly the history section 7.3 exists to keep. Fail instead and let an operator
    # choose (phase 2 review finding R8).
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT conversation_id, count(*) FROM run "
                "GROUP BY conversation_id HAVING count(*) > 1 LIMIT 5"
            )
        )
        .fetchall()
    )
    if duplicates:
        listed = ", ".join(f"{row[0]} ({row[1]} runs)" for row in duplicates)
        msg = (
            "uq_run_conversation cannot be created: these conversations have more than one "
            f"run: {listed}. Merge or archive them first - this migration will not delete "
            "runs, because that would cascade their trace steps away."
        )
        raise RuntimeError(msg)
    op.drop_index("ix_run_conversation_id", table_name="run")
    op.create_unique_constraint("uq_run_conversation", "run", ["conversation_id"])


def downgrade() -> None:
    op.drop_constraint("uq_run_conversation", "run", type_="unique")
    op.create_index("ix_run_conversation_id", "run", ["conversation_id"])
    op.drop_index("ix_run_timeout_at", table_name="run")
    for column in (
        "awaiting",
        "timeout_at",
        "suspended_at",
        "next_frame_seq",
        "turn_nodes",
        "pack_fingerprint",
    ):
        op.drop_column("run", column)

    op.drop_constraint("uq_trace_step_run_seq", "trace_step", type_="unique")
    op.create_index("ix_trace_step_run_started", "trace_step", ["run_id", "started_at"])
    op.drop_column("trace_step", "error")
    op.drop_column("trace_step", "seq")
