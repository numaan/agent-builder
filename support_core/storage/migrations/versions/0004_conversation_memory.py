"""Turn counting for the rolling conversation summary.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06

Phase 3, DESIGN.md section 10: "Conversation summary: rolling LLM summary updated every K turns
... ``conversation.summary``". The column holding the summary has existed since 0001; what was
missing was the K.

* ``conversation.turn_count`` is incremented in the same transaction that starts a turn - the
  claim transaction for a customer message, the turn-start write for a resume - so it cannot be
  lost by a process that dies mid-turn and cannot be double-counted by one that is re-entered
  from its last checkpoint.
* ``conversation.summary_turn`` records which turn the stored summary covers. "Due for a
  summary" is then ``turn_count - summary_turn >= K``, evaluated entirely from durable state,
  and the summary and the marker are written together so a crash between them is not a state
  the code has to reason about.

Both are additive with a server default of zero, so an existing conversation is simply one that
has never been summarised: its next turn makes it due. Nothing a turn *depends on* is read from
either column - the summary is prompt context and nothing else - which is the property phase 2's
two must-fix findings were about, stated here so a later phase does not quietly break it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation", sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "conversation", sa.Column("summary_turn", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("conversation", "summary_turn")
    op.drop_column("conversation", "turn_count")
