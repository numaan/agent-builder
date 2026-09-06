"""The channel's own name for a conversation.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-06

DESIGN.md section 12 identifies a conversation by something the *channel* owns - "thread id,
session id" - and section 4.1 runs any number of processes against one database, so that name
has to be durable and has to be the same fact for every process. ``conversation.channel_key``
is it: a web chat session key that survives a reconnect, and (phase 7) the mail thread an
``In-Reply-To`` resolves to.

Unique per channel, deliberately. Two browser tabs that open at the same moment with the same
session key, or a mail provider that delivers the same webhook twice, must not produce two
conversations for one thread; the loser of the race reads the winner's row back rather than
starting a second conversation whose history is half the story. It is nullable because a
conversation started by the API rather than by a channel - the engine's own tests, a desk-opened
conversation in phase 7 - has no channel name for itself, and a partial index keeps those rows
out of the uniqueness check.

Additive and reversible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversation", sa.Column("channel_key", sa.Text(), nullable=True))
    op.create_index(
        "uq_conversation_channel_key",
        "conversation",
        ["channel", "channel_key"],
        unique=True,
        postgresql_where=sa.text("channel_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversation_channel_key", table_name="conversation")
    op.drop_column("conversation", "channel_key")
