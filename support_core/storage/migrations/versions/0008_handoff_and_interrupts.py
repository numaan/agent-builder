"""What a handoff packet needs, and where a deferred intent waits.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-06

Two unrelated needs, one migration, because both are phase 6's and both are additive.

**The handoff table becomes answerable.** Phase 0 created ``handoff`` from DESIGN.md section 17's
one-line description: a conversation, a JSONB packet, a queue and a status. A desk (section 13)
has to ask three more questions of it - which run this came from, *why* it came, and when its SLA
runs out - and answering them by reading JSON out of the packet would make the packet a schema.
``run_id`` also gives the desk the join it needs to resume the right run when a conversation has
had more than one.

**A deferred intent has to survive a crash.** DESIGN.md section 6.6 records a secondary intent
when the current graph blocks interrupts, and section 19 step 15 surfaces it several turns later.
Anything the engine has to remember across turns is a column, for the reason phase 2's two
must-fix findings were both about; ``run.secondary_intents`` is written in the same transaction
as the claim that recorded it.

Additive and reversible: every column is nullable or defaulted, and the downgrade drops exactly
what the upgrade added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column(
            "secondary_intents",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("handoff", sa.Column("run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_handoff_run_id_run", "handoff", "run", ["run_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_handoff_run_id", "handoff", ["run_id"])
    op.add_column(
        "handoff", sa.Column("reason", sa.Text(), nullable=False, server_default=sa.text("''"))
    )
    op.add_column("handoff", sa.Column("graph_id", sa.Text(), nullable=True))
    op.add_column("handoff", sa.Column("node_id", sa.Text(), nullable=True))
    op.add_column("handoff", sa.Column("step_id", sa.Text(), nullable=True))
    # The same device as ``uq_action_approval_run_step``: a ``handoff`` node re-executed after a
    # crash - the packet is built and delivered before its checkpoint commits - must put one
    # packet on the queue, not one per attempt. Partial, because a handoff raised by the engine's
    # own failure routing has a step and one raised by a timeout sweep does not.
    op.create_index(
        "uq_handoff_run_step",
        "handoff",
        ["run_id", "step_id"],
        unique=True,
        postgresql_where=sa.text("step_id IS NOT NULL"),
    )
    op.add_column("handoff", sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "handoff",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("handoff", "updated_at")
    op.drop_column("handoff", "sla_due_at")
    op.drop_index("uq_handoff_run_step", table_name="handoff")
    op.drop_column("handoff", "step_id")
    op.drop_column("handoff", "node_id")
    op.drop_column("handoff", "graph_id")
    op.drop_column("handoff", "reason")
    op.drop_index("ix_handoff_run_id", table_name="handoff")
    op.drop_constraint("fk_handoff_run_id_run", "handoff", type_="foreignkey")
    op.drop_column("handoff", "run_id")
    op.drop_column("run", "secondary_intents")
