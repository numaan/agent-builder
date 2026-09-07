"""Every SQL statement the knowledge layer runs. Implements the storage half of DESIGN.md
sections 9.1 to 9.3 over the ``doc_source`` and ``doc_chunk`` tables of section 17.

Split out of :mod:`support_core.storage.repositories` rather than added to it: that module is
"every statement *the engine* runs", and its docstring's claim - that the executor holds no
authoritative state because everything deciding what happens next is in three tables read and
written here - is worth keeping true by keeping retrieval out of it. Nothing in this module is
in the write path of a turn.

The one thing to know about the reads: they filter ``stale = false``. A re-sync writes new rows
under a new ``source_version`` and marks every older row of that source stale in the same
transaction, so retrieval sees exactly one version of a source and an older trace can still name
- and a query can still find, by version - the rows that produced it.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Float, Text, bindparam, cast, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import TSQUERY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_core.knowledge.embedding import DIMENSIONS
from support_core.storage.models import DocChunk, DocSource

NEVER_SYNCED = "unsynced"
"""``doc_source.version`` before the first sync. A real version is ``r<n>-<sha>``, so this can
never be mistaken for one."""


@dataclass(slots=True)
class ChunkWrite:
    """One chunk, ready to insert."""

    chunk_index: int
    locator: str
    text: str
    embedding: list[float]


@dataclass(slots=True)
class ChunkRow:
    """One chunk read back, with everything a :class:`~support_core.knowledge.types.Passage`
    needs and nothing a retriever would have to join for."""

    id: uuid.UUID
    source_id: str
    source_version: str
    chunk_index: int
    locator: str
    text: str
    score: float = 0.0


async def get_source(session: AsyncSession, source_id: str) -> DocSource | None:
    return (
        await session.execute(select(DocSource).where(DocSource.id == source_id))
    ).scalar_one_or_none()


async def upsert_source(
    session: AsyncSession,
    *,
    source_id: str,
    source_type: str,
    config: dict[str, Any],
    version: str,
    revision: int,
    checksum: str,
    now: datetime,
) -> None:
    """Create or update the source row, and nothing else.

    Written as one ``INSERT ... ON CONFLICT DO UPDATE`` because two syncs of the same pack
    started together would otherwise race between a ``SELECT`` and an ``INSERT`` and one of them
    would fail on the primary key. The sync is not in a turn's write path, so this is a
    convenience rather than a correctness property of the engine - but a scheduled job that
    fails once a week for a reason nobody can reproduce is its own kind of cost.
    """
    statement = insert(DocSource).values(
        id=source_id,
        type=source_type,
        config=config,
        version=version,
        revision=revision,
        checksum=checksum,
        last_synced_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[DocSource.id],
            set_={
                "type": statement.excluded.type,
                "config": statement.excluded.config,
                "version": statement.excluded.version,
                "revision": statement.excluded.revision,
                "checksum": statement.excluded.checksum,
                "last_synced_at": statement.excluded.last_synced_at,
            },
        )
    )


async def write_chunks(
    session: AsyncSession, *, source_id: str, source_version: str, chunks: Sequence[ChunkWrite]
) -> None:
    """Insert one version's chunks.

    The caller has already flushed the ``doc_source`` row: there are no ``relationship()``\\ s
    anywhere in ``models.py``, so the unit of work does not order these two inserts and a
    foreign key violation is what an unflushed parent looks like (phase-0 implementation note,
    which named this phase).
    """
    for chunk in chunks:
        if len(chunk.embedding) != DIMENSIONS:
            msg = (
                f"{source_id}: the encoder produced a {len(chunk.embedding)}-dimensional vector "
                f"and doc_chunk.embedding is vector({DIMENSIONS}). A different embedding model "
                f"needs its own migration; see 0010_knowledge_index.py."
            )
            raise ValueError(msg)
    session.add_all(
        [
            DocChunk(
                source_id=source_id,
                source_version=source_version,
                chunk_index=chunk.chunk_index,
                locator=chunk.locator,
                text=chunk.text,
                embedding=chunk.embedding,
                stale=False,
            )
            for chunk in chunks
        ]
    )


async def mark_stale(session: AsyncSession, *, source_id: str, keep_version: str) -> int:
    """Mark every chunk of this source that is not the live version stale (DESIGN.md 9.3).

    Marked, not deleted. The rows are what a trace's ``source_version`` refers to, and the exit
    criterion of this phase is that an old trace still names - and can still be shown - the
    version that produced a wrong answer. Deleting them would make the trace a dangling
    reference, which is the failure mode "traceable to a source version" exists to prevent.
    """
    result = await session.execute(
        update(DocChunk)
        .where(DocChunk.source_id == source_id, DocChunk.source_version != keep_version)
        .where(DocChunk.stale.is_(False))
        .values(stale=True)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def drop_version(session: AsyncSession, *, source_id: str, source_version: str) -> int:
    """Delete one version's chunks. Only a retention sweep calls this, never a sync."""
    result = await session.execute(
        delete(DocChunk).where(
            DocChunk.source_id == source_id, DocChunk.source_version == source_version
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def stale_versions(session: AsyncSession, source_id: str, *, keep: int) -> list[str]:
    """The stale versions of one source beyond the newest ``keep``, oldest last.

    Ordered by the first row's ``created_at`` rather than by parsing the version string: the
    string's shape is a knowledge-layer decision and this module should not depend on it.
    """
    rows = (
        await session.execute(
            select(DocChunk.source_version, func.min(DocChunk.created_at).label("first"))
            .where(DocChunk.source_id == source_id, DocChunk.stale.is_(True))
            .group_by(DocChunk.source_version)
            .order_by(text("first DESC"))
        )
    ).all()
    return [str(row[0]) for row in rows[keep:]]


async def chunk_count(session: AsyncSession, source_id: str, *, stale: bool | None = False) -> int:
    statement = select(func.count()).select_from(DocChunk).where(DocChunk.source_id == source_id)
    if stale is not None:
        statement = statement.where(DocChunk.stale.is_(stale))
    return int((await session.execute(statement)).scalar_one())


async def live_chunks(session: AsyncSession, source_ids: Sequence[str]) -> list[ChunkRow]:
    """Every live chunk of the named sources. Used by the sync's vector push and by tests."""
    statement = select(
        DocChunk.id,
        DocChunk.source_id,
        DocChunk.source_version,
        DocChunk.chunk_index,
        DocChunk.locator,
        DocChunk.text,
    ).where(DocChunk.stale.is_(False))
    if source_ids:
        statement = statement.where(DocChunk.source_id.in_(list(source_ids)))
    rows = (
        await session.execute(statement.order_by(DocChunk.source_id, DocChunk.chunk_index))
    ).all()
    return [
        ChunkRow(
            id=row[0],
            source_id=row[1],
            source_version=row[2],
            chunk_index=row[3],
            locator=row[4],
            text=row[5],
        )
        for row in rows
    ]


async def chunks_by_id(
    session: AsyncSession, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ChunkRow]:
    """Read specific chunks back by primary key, for a vector hit that carries only an id."""
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                DocChunk.id,
                DocChunk.source_id,
                DocChunk.source_version,
                DocChunk.chunk_index,
                DocChunk.locator,
                DocChunk.text,
            ).where(DocChunk.id.in_(list(ids)))
        )
    ).all()
    return {
        row[0]: ChunkRow(
            id=row[0],
            source_id=row[1],
            source_version=row[2],
            chunk_index=row[3],
            locator=row[4],
            text=row[5],
        )
        for row in rows
    }


async def search_lexical(
    session: AsyncSession, *, query: str, k: int, source_ids: Sequence[str] = ()
) -> list[ChunkRow]:
    """Postgres full-text search over the stored ``tsv`` (DESIGN.md section 9.1's lexical half).

    ``websearch_to_tsquery`` rather than ``plainto_tsquery``: it accepts a customer's own words
    including quotes and ``or`` without raising on punctuation, which matters because the query
    is built from a template that may interpolate what the customer typed. ``ts_rank_cd`` rather
    than ``ts_rank`` because cover density rewards a passage where the terms appear near each
    other, which is what "the answer turns on a phrase" means for the lexical side.

    **The conjunction is relaxed to a disjunction**, and that is the one non-obvious thing in
    here. ``websearch_to_tsquery`` joins bare words with ``&``, so a retrieval query built from a
    customer's sentence - "how many days do I have to ask for a refund" - becomes
    ``'mani' & 'day' & 'ask' & 'refund'`` and matches only a passage containing every one of
    them. On a policy corpus that is almost never any passage: measured over the labelled query
    set in ``tests/test_retrieval_quality.py``, the ``&`` form answered 3 of 12 and the ``|``
    form answers 11. Which of the matches is best is then ``ts_rank_cd``'s job, and cover density
    is good at exactly that - it rewards a passage carrying more of the terms, closer together.

    The rewrite is textual and it is careful: only ``&`` is replaced, so a quoted phrase's
    ``<->`` survives and a phrase search still means a phrase. The case it does get wrong is a
    leading negation - ``!x & y`` becomes ``!x | y``, which matches on ``y`` alone - and that is
    accepted rather than fixed, because the alternative is parsing tsquery text here, and a
    customer who types a minus sign in a support chat is asking for a hyphen.

    An empty query - a customer message of pure punctuation, a template that rendered to nothing
    - produces a tsquery that matches everything at rank zero. It is refused here instead, so
    the lexical half returns nothing rather than an arbitrary k rows with no relevance at all.
    """
    if not query.strip():
        return []
    tsquery = cast(
        func.replace(
            cast(func.websearch_to_tsquery("english", bindparam("q", query)), Text), "&", "|"
        ),
        TSQUERY,
    )
    rank = func.ts_rank_cd(DocChunk.tsv, tsquery, 32).cast(Float)
    statement = (
        select(
            DocChunk.id,
            DocChunk.source_id,
            DocChunk.source_version,
            DocChunk.chunk_index,
            DocChunk.locator,
            DocChunk.text,
            rank.label("score"),
        )
        .where(DocChunk.stale.is_(False), DocChunk.tsv.op("@@")(tsquery))
        .order_by(text("score DESC"))
        .limit(k)
    )
    if source_ids:
        statement = statement.where(DocChunk.source_id.in_(list(source_ids)))
    rows = (await session.execute(statement)).all()
    return [
        ChunkRow(
            id=row[0],
            source_id=row[1],
            source_version=row[2],
            chunk_index=row[3],
            locator=row[4],
            text=row[5],
            score=float(row[6]),
        )
        for row in rows
    ]


async def search_dense(
    session: AsyncSession, *, embedding: Sequence[float], k: int, source_ids: Sequence[str] = ()
) -> list[ChunkRow]:
    """pgvector cosine nearest neighbours (DESIGN.md section 9.1's embedding half).

    This is the *baseline* the ColBERT path is measured against, which is the backlog's
    "measure it against the pgvector path on the same corpus before making it the default", and
    it is also the dense half of the answer when Qdrant is unreachable. Score is ``1 - distance``
    so that every backend in this system reports "bigger is better" and the merge does not have
    to know which is which.
    """
    if len(embedding) != DIMENSIONS:
        msg = f"query embedding is {len(embedding)}-dimensional; the index is vector({DIMENSIONS})"
        raise ValueError(msg)
    distance = DocChunk.embedding.cosine_distance(list(embedding))
    statement = (
        select(
            DocChunk.id,
            DocChunk.source_id,
            DocChunk.source_version,
            DocChunk.chunk_index,
            DocChunk.locator,
            DocChunk.text,
            distance.label("distance"),
        )
        .where(DocChunk.stale.is_(False), DocChunk.embedding.is_not(None))
        .order_by(text("distance ASC"))
        .limit(k)
    )
    if source_ids:
        statement = statement.where(DocChunk.source_id.in_(list(source_ids)))
    rows = (await session.execute(statement)).all()
    return [
        ChunkRow(
            id=row[0],
            source_id=row[1],
            source_version=row[2],
            chunk_index=row[3],
            locator=row[4],
            text=row[5],
            score=1.0 - float(row[6]),
        )
        for row in rows
    ]
