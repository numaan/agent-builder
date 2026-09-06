"""Per-turn tool call accounting.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-06

Phase 3 review finding V6. DESIGN.md section 5.1 declares ``limits.max_tool_calls_per_turn``,
and the phase-3 code passed that number to each ``llm`` node's tool gateway as a *per-node* cap,
so a turn with three tool-using nodes could make three times the documented limit. The fix is
the same shape as ``run.turn_nodes`` (migration 0002's ``max_nodes_per_turn`` counter): count on
the run row, in the checkpoint that follows the node, so a process that dies mid-turn does not
reset the budget it has already spent.

Additive, server default zero, and reversible: an existing run is one that has spent nothing in
the turn it is in.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run", sa.Column("turn_tool_calls", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("run", "turn_tool_calls")
