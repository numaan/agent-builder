"""A dimensioned embedding column, its ANN index, and a versioned source.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-07

Three changes, all to the knowledge tables of DESIGN.md section 17.

**``doc_chunk.embedding`` becomes ``vector(128)``** (phase-0 review finding N12). Phase 0 left it
dimensionless because the design does not fix an embedding model and phase 5 does. Dimensionless
was not a neutral placeholder: ``CREATE INDEX ... USING hnsw`` fails on it with "column does not
have dimensions", so there was no ANN index and could not be one, and rows of different
dimensions coexisted happily until a distance query hit two of them and failed at *query* time
with "different vector dimensions 2 and 3" - the worst place to find out.

128 is what ``colbert-ir/colbertv2.0`` emits per token, and the deterministic encoder that ships
as this deployment's default is built to match it, so the dense side and the late-interaction
side cannot disagree about a dimension. A deployment that swaps in a hosted embedder of another
width needs a migration, and the sync refuses to write a vector of the wrong width naming this
one rather than letting Postgres discover it later.

The column is altered rather than rebuilt, and the table must be **empty** to alter it: chunks are
derived data - ``support pack knowledge sync`` rebuilds them from the pack in seconds - so the
migration refuses on a non-empty table and says what to do, rather than either casting rows it
cannot interpret or deleting a customer's index without being asked. That is the same rule
phase-2 finding R8 settled for back-fills: fail loudly, name the rows, destroy nothing.

The index is HNSW with ``vector_cosine_ops``, because the retriever scores by cosine distance and
an index built for a different operator class is not used at all.

**``doc_source`` gains ``revision`` and ``checksum``.** ``version`` (the ``source_version`` chunks
carry) is content-addressed - ``r<n>-<sha256[:12]>`` - so a sync of unchanged content is a no-op
that keeps the version, and any edit produces a new one. ``revision`` is the monotonic ``n``,
which is also the Qdrant collection suffix (``<prefix><source>_v<n>``), and ``checksum`` is the
hash of the normalised corpus the last sync read. Both default to a value meaning "never synced",
so an existing row upgrades without a back-fill.

**The downgrade no longer drops the ``vector`` extension** - that half is in migration ``0001``
and is phase-0 finding N2; see the note there. This revision's downgrade drops the index, returns
the column to a dimensionless ``vector`` and drops the two columns, which is exactly what it
added.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 128
"""Kept in step with :data:`support_core.knowledge.embedding.DIMENSIONS`, which is what writes
the vectors. A test asserts the two agree, because a silent drift here is a column that accepts
nothing the encoder produces."""


class NonEmptyDocChunkError(RuntimeError):
    """``doc_chunk`` holds rows whose embedding width this migration cannot know."""


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT count(*) FROM doc_chunk")).scalar_one()
    if rows:
        msg = (
            f"doc_chunk holds {rows} row(s) and this migration fixes the embedding column to "
            f"vector({EMBEDDING_DIMENSIONS}). Chunks are derived data: empty the table "
            f"(DELETE FROM doc_chunk) and re-run `support pack knowledge sync <pack>` after the "
            f"upgrade. The migration refuses rather than casting or deleting rows it cannot "
            f"interpret."
        )
        raise NonEmptyDocChunkError(msg)

    op.execute(f"ALTER TABLE doc_chunk ALTER COLUMN embedding TYPE vector({EMBEDDING_DIMENSIONS})")
    op.execute(
        "CREATE INDEX ix_doc_chunk_embedding ON doc_chunk USING hnsw (embedding vector_cosine_ops)"
    )

    op.add_column(
        "doc_source",
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("doc_source", sa.Column("checksum", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("doc_source", "checksum")
    op.drop_column("doc_source", "revision")
    op.execute("DROP INDEX IF EXISTS ix_doc_chunk_embedding")
    op.execute("ALTER TABLE doc_chunk ALTER COLUMN embedding TYPE vector")
