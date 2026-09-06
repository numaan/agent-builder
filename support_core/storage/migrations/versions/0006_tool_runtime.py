"""Tool calls joinable to a run, and single-use approvals.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-06

Two deferred findings from the phase-0 review, both owned by this phase:

* **F6.** ``tool_call`` had no link to a conversation except by parsing the idempotency key
  string. It gains ``run_id`` (FK, indexed), ``step_id`` and ``node_id``, so "every tool call in
  this conversation" is a join - which the handoff packet (DESIGN.md section 13), replay
  (section 15) and the audit question "which approval covered which call for whom" all need.
* **N1.** ``action_approval`` had no single-use marker, so one row could satisfy two calls with
  the same argument hash - a second refund of the same amount in the same conversation would
  pass the check. It gains ``consumed_by_tool_call_id`` and ``consumed_at``, and the runtime
  treats a consumed approval as absent.

The approval also gains where it was given (``run_id``, ``frame_seq``, ``node_id``, ``step_id``)
and what it approved (``args``). The first four are what let the runtime refuse an approval from
an *earlier invocation of the same graph*: ``frame_seq`` is monotonic and never reused
(migration 0002), so a replayed approval is refused before single-use has to catch it. ``args``
is for the human reading the audit trail, who should not have to reverse a sha256 to see what
was approved.

``tool_call`` also gains ``error``, ``finished_at``, ``attempts`` and ``context_patch``:
the failure text a ``tool`` node routes on, when the call ended, how many times the runtime
entered it under one key, and the ``ctx`` change the call asked for (DESIGN.md section 19
step 9), which has to be replayable with the result or a crash between the two would leave the
effect and the context out of step.

Additive and reversible. Every added column is nullable or has a server default, because a
database that already holds ``tool_call`` rows written before this revision has no run to point
them at.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tool_call", sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("tool_call", sa.Column("step_id", sa.Text(), nullable=True))
    op.add_column("tool_call", sa.Column("node_id", sa.Text(), nullable=True))
    op.add_column("tool_call", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("tool_call", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tool_call", sa.Column("attempts", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("tool_call", sa.Column("context_patch", postgresql.JSONB(), nullable=True))
    # Unnamed, like every other foreign key in this schema: Postgres names them
    # ``<table>_<column>_fkey``, which is what the models produce too.
    op.create_foreign_key(None, "tool_call", "run", ["run_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_tool_call_run_id", "tool_call", ["run_id"])

    op.add_column(
        "action_approval", sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("action_approval", sa.Column("frame_seq", sa.Integer(), nullable=True))
    op.add_column("action_approval", sa.Column("node_id", sa.Text(), nullable=True))
    op.add_column("action_approval", sa.Column("step_id", sa.Text(), nullable=True))
    op.add_column("action_approval", sa.Column("args", postgresql.JSONB(), nullable=True))
    op.add_column(
        "action_approval",
        sa.Column("consumed_by_tool_call_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "action_approval", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(None, "action_approval", "run", ["run_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_action_approval_run_id", "action_approval", ["run_id"])
    op.create_foreign_key(
        None,
        "action_approval",
        "tool_call",
        ["consumed_by_tool_call_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # One confirm node, one approval, however many times its step re-executes after a crash.
    # Postgres treats NULLs as distinct in a unique constraint, so rows written before this
    # revision (which have neither column) are unaffected.
    op.create_unique_constraint(
        "uq_action_approval_run_step", "action_approval", ["run_id", "step_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_action_approval_run_step", "action_approval", type_="unique")
    op.drop_index("ix_action_approval_run_id", table_name="action_approval")
    # The foreign keys go with their columns: Postgres drops a constraint when the column it
    # constrains is dropped, so naming them here would only be a second way to get it wrong.
    for column in (
        "consumed_at",
        "consumed_by_tool_call_id",
        "args",
        "step_id",
        "node_id",
        "frame_seq",
        "run_id",
    ):
        op.drop_column("action_approval", column)

    op.drop_index("ix_tool_call_run_id", table_name="tool_call")
    for column in (
        "context_patch",
        "attempts",
        "finished_at",
        "error",
        "node_id",
        "step_id",
        "run_id",
    ):
        op.drop_column("tool_call", column)
