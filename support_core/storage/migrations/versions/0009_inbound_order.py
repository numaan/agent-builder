"""A decided order for the pending inbound queue.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-07

DESIGN.md section 17 says inbound messages that arrive while the conversation is locked "are
stored in ``message`` with ``status = pending`` and processed in order". *In order* was, until
now, ``ORDER BY created_at, id``: ``created_at`` defaults to the transaction's start timestamp,
so two callers who arrive at the same instant share it to the microsecond and the tie falls to a
random UUID. Phase W's reviewer issued two ``POST``s together in a known order and got them back
in the other one - the second message ran against a conversation the first had not started, which
routed to a handoff and parked the run, and the first message is still ``pending`` and always will
be (review finding W4).

Two columns, and between them the order stops being a measurement:

* ``conversation.inbound_seq`` counts the inbound messages a conversation has accepted;
* ``message.queue_seq`` is the number one message claimed from it.

The claim is ``UPDATE conversation SET inbound_seq = inbound_seq + 1 ... RETURNING``, which takes
the conversation's row lock, so concurrent enqueues serialise there and leave with distinct,
increasing numbers in the order the database granted them. Phase 2's review found two
message-loss bugs of this family and fixed both in durable state rather than in timing; a reorder
that dead-ends a conversation is the same class of problem and gets the same kind of fix.

The backfill numbers existing inbound rows by the order the old query would have produced, so a
database upgraded in place drains its queue exactly as it would have. The downgrade drops both
columns; the old ordering is still there underneath as a tie-break.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column("inbound_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("message", sa.Column("queue_seq", sa.Integer(), nullable=True))
    op.create_index(
        "ix_message_pending_order",
        "message",
        ["conversation_id", "queue_seq"],
        postgresql_where=sa.text("direction = 'inbound'"),
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY conversation_id ORDER BY created_at, id
                   ) AS position
            FROM message
            WHERE direction = 'inbound'
        )
        UPDATE message SET queue_seq = ranked.position
        FROM ranked WHERE message.id = ranked.id
        """
    )
    op.execute(
        """
        UPDATE conversation SET inbound_seq = COALESCE(
            (
                SELECT max(queue_seq) FROM message
                WHERE message.conversation_id = conversation.id
                  AND message.direction = 'inbound'
            ),
            0
        )
        """
    )


def downgrade() -> None:
    op.drop_index("ix_message_pending_order", table_name="message")
    op.drop_column("message", "queue_seq")
    op.drop_column("conversation", "inbound_seq")
