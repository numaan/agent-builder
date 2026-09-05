"""Recovery attempt counting and a total order for messages written together.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06

Phase 2 resolution (reviews/phase-2.md), independent review findings R3 and R4.

* ``run.recovery_attempts`` counts the times :meth:`Executor.recover_stalled` has failed on
  this run. Without it a conversation that cannot be recovered - a node type this core cannot
  execute, a hook that raises - is retried by every sweep for ever, and (before the same
  finding's other half) took the whole batch down with it. A column rather than a key in
  ``run.awaiting`` because a checkpoint rewrites ``awaiting``, so a run that makes a little
  progress before failing again would reset its own counter and never be parked.
* ``message.ordinal`` orders the messages one checkpoint writes. ``created_at`` is the
  transaction timestamp, identical for every row a single node produced, so ``ORDER BY
  created_at, id`` fell back to a random UUID. Nothing in phase 2 emits two messages from one
  node; phase 3's ``llm`` node and phase 6's handoff do, and both the stored transcript and the
  order handed to a channel depend on it.

Both columns are additive with server defaults, so an existing row needs no back-fill: a run
that has never failed recovery has made zero attempts, and a message written before this
revision was the only one in its transaction.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run", sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("message", sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("message", "ordinal")
    op.drop_column("run", "recovery_attempts")
