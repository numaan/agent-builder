"""``DocumentRetriever`` and ``ColbertRetriever``. Implements DESIGN.md section 9.1's two
document backends.

DESIGN.md 9.1 describes one ``DocumentRetriever`` doing "hybrid search (pgvector embeddings plus
Postgres full-text)". That is :class:`DocumentRetriever`, unchanged. BACKLOG.md's 2026-09-06
amendment adds a second, :class:`ColbertRetriever`, scoring late interaction in Qdrant. The two
sit behind the same protocol and :class:`~support_core.knowledge.composite.CompositeRetriever`
merges them, which is the split the decisions log made deliberately:

    Qdrant owns the vector side (dense and ColBERT multivector), Postgres full-text owns the
    lexical side. ``CompositeRetriever`` merges them. This buys graceful degradation - with
    Qdrant unavailable the lexical half still answers.

There is one wrinkle in that sentence worth being exact about, because a reader will notice it:
the *dense* vectors are in both stores. ``doc_chunk.embedding`` is populated and indexed
(migration ``0010``) as well as being pushed to Qdrant. That is not an accident and it is not
redundancy for its own sake. It is what makes two of this phase's obligations possible at once -
the backlog's "measure it against the pgvector path on the same corpus", which needs a pgvector
path to measure, and "retrieval degrades rather than dies", which is a stronger promise if the
degraded mode still has a semantic half rather than only a keyword one. The cost is one vector
per chunk stored twice, which for a policy corpus is kilobytes.

Both retrievers hold *readers*, never a session or a client they own. The lifetime of a
connection belongs to whoever built the executor, and a retriever that opened its own session
would be holding one across a model call.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from support_core.knowledge.embedding import Embedder, LateInteractionEncoder
from support_core.knowledge.qdrant import QdrantStore, alias_name
from support_core.knowledge.types import Passage, RetrievalRequest, RetrieverUnavailable
from support_core.storage import knowledge_repo as repo

logger = logging.getLogger(__name__)

Sessions = Callable[[], Any]
"""An async session factory, the same one the executor holds."""

CANDIDATE_MULTIPLIER = 4
"""How many candidates each half fetches beyond ``k``.

The merge that follows is a rank fusion, so a passage that is fourth in both halves should be
able to beat one that is first in one and absent from the other. Fetching exactly ``k`` from each
would make that invisible. Four is enough for the ``k`` values a ``knowledge:`` block declares
(1 to 20) without turning a retrieval into a scan."""

COLBERT_PREFETCH = 64
"""Candidates the dense stage hands to the MaxSim rescoring (see
:meth:`support_core.knowledge.qdrant.QdrantStore.search_colbert`). Late interaction is a
reranker; the first stage is what keeps it inside DESIGN.md section 20's budget."""


def _passage(row: repo.ChunkRow, backend: str) -> Passage:
    return Passage(
        text=row.text,
        source_id=row.source_id,
        source_version=row.source_version,
        locator=row.locator,
        score=row.score,
        backend=backend,
    )


class DocumentRetriever:
    """Hybrid lexical plus dense search over ``doc_chunk`` (DESIGN.md section 9.1).

    The two halves are separately callable - :meth:`lexical` and :meth:`dense` - and
    :meth:`retrieve` is their union, deduplicated. They are separate methods rather than one
    query with two ``ORDER BY`` terms because the backlog requires the two to be *measured*
    against each other and against ColBERT, and a measurement of a blend is not a measurement of
    its parts.

    Dense search needs an embedder. With none configured the retriever is lexical only and says
    so once, at construction: a deployment with no embedding API (which is this one, until
    something local is installed) should get keyword search rather than an exception on the first
    customer's question.
    """

    name = "document"

    def __init__(
        self,
        sessions: Sessions,
        *,
        embedder: Embedder | None = None,
        source_ids: Sequence[str] = (),
    ) -> None:
        self.sessions = sessions
        self.embedder = embedder
        self.source_ids = tuple(source_ids)
        if embedder is None:
            logger.info("document retriever has no embedder; the dense half is off")

    async def lexical(self, query: str, k: int) -> list[Passage]:
        async with self.sessions() as session, session.begin():
            rows = await repo.search_lexical(session, query=query, k=k, source_ids=self.source_ids)
        return [_passage(row, "lexical") for row in rows]

    async def dense(self, query: str, k: int) -> list[Passage]:
        if self.embedder is None:
            return []
        embedding = await self.embedder.embed_query(query)
        async with self.sessions() as session, session.begin():
            rows = await repo.search_dense(
                session, embedding=embedding, k=k, source_ids=self.source_ids
            )
        return [_passage(row, "dense") for row in rows]

    async def retrieve(self, request: RetrievalRequest) -> Sequence[Passage]:
        """Both halves, fused. The composite fuses again across backends; this is the same rule
        applied one level down, so ``DocumentRetriever`` used alone behaves the same way."""
        wide = request.k * CANDIDATE_MULTIPLIER
        lexical = await self.lexical(request.query, wide)
        dense = await self.dense(request.query, wide)
        return fuse([lexical, dense])[: request.k]


class ColbertRetriever:
    """Late interaction over Qdrant multivectors, scored by MaxSim (BACKLOG.md, phase 5).

    Why this belongs in this phase rather than a later one is in the decisions log: it runs
    locally, and this deployment has no embedding API, so without it the dense path would ship
    with its retrieval quality unmeasured against a stand-in. Why it lives in Qdrant rather than
    in a PLAID index directory or in SQL is there too.

    The payload carried on each point is deliberately minimal: the chunk's primary key, its
    source and its version. The *text* is read back from Postgres. Two reasons, and the second is
    the one that matters. A vector store is a derived index, and an index that also holds the
    only copy of the text is a second source of truth that can disagree with the first. And a
    passage's ``source_version`` has to be the version Postgres says produced that row, because
    that is what the trace names and what the exit criterion checks - reading it out of a
    document written by the sync that also wrote the vectors would be checking the sync against
    itself.
    """

    name = "colbert"

    def __init__(
        self,
        store: QdrantStore,
        sessions: Sessions,
        *,
        encoder: LateInteractionEncoder,
        embedder: Embedder,
        source_ids: Sequence[str],
        prefetch: int = COLBERT_PREFETCH,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.encoder = encoder
        self.embedder = embedder
        self.source_ids = tuple(source_ids)
        self.prefetch = prefetch

    async def retrieve(self, request: RetrievalRequest) -> Sequence[Passage]:
        if not self.source_ids:
            return []
        query_vectors = await self.encoder.encode_query(request.query)
        dense = await self.embedder.embed_query(request.query)
        wide = request.k * CANDIDATE_MULTIPLIER
        scored: dict[uuid.UUID, tuple[float, str, str]] = {}
        failures: list[str] = []
        for source_id in self.source_ids:
            alias = alias_name(source_id, prefix=self.store.prefix)
            try:
                hits = await self.store.search_colbert(
                    alias,
                    dense=dense,
                    colbert=query_vectors,
                    limit=wide,
                    prefetch=self.prefetch,
                )
            except RetrieverUnavailable as exc:
                # One source's collection may be missing - a pack that declares a source nobody
                # has synced yet - without the whole backend being down. Only a total failure is
                # reported as unavailable, so a half-synced pack degrades to the sources that do
                # exist rather than losing the vector side entirely.
                failures.append(f"{source_id}: {exc}")
                continue
            for hit in hits:
                chunk_id = _chunk_id(hit.payload)
                if chunk_id is None:
                    continue
                previous = scored.get(chunk_id)
                if previous is None or hit.score > previous[0]:
                    scored[chunk_id] = (
                        hit.score,
                        str(hit.payload.get("source_id", source_id)),
                        str(hit.payload.get("source_version", "")),
                    )
        if failures and len(failures) == len(self.source_ids):
            raise RetrieverUnavailable("; ".join(failures))
        if not scored:
            return []
        async with self.sessions() as session, session.begin():
            rows = await repo.chunks_by_id(session, list(scored))
        passages: list[Passage] = []
        for chunk_id, (score, _, indexed_version) in scored.items():
            row = rows.get(chunk_id)
            if row is None:
                # The collection knows about a chunk Postgres no longer has: a retention sweep
                # dropped the version, or somebody emptied the table. Skipping is right - the
                # text is gone and a passage without text is not evidence - and it is logged
                # because the two stores drifting is worth knowing about.
                logger.warning("qdrant point %s has no doc_chunk row", chunk_id)
                continue
            if indexed_version and indexed_version != row.source_version:
                logger.warning(
                    "qdrant point %s says version %s and doc_chunk says %s",
                    chunk_id,
                    indexed_version,
                    row.source_version,
                )
            row.score = score
            passages.append(_passage(row, self.name))
        passages.sort(key=lambda passage: passage.score, reverse=True)
        return passages[: request.k]


def _chunk_id(payload: dict[str, Any]) -> uuid.UUID | None:
    raw = payload.get("chunk_id")
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def fuse(ranked: Sequence[Sequence[Passage]], *, constant: int = 60) -> list[Passage]:
    """Reciprocal rank fusion over several ranked lists (used here and by the composite).

    ``sum(1 / (constant + rank))`` over the lists a passage appears in. Rank fusion rather than
    score fusion because the scores are not comparable: ``ts_rank_cd`` is a cover density in no
    particular unit, cosine similarity is in [-1, 1], and MaxSim is a *sum* over query tokens and
    so grows with the length of the question. Normalising three of those onto one scale would be
    inventing a relationship between them; their orderings are the part that means something.

    ``constant`` is RRF's usual 60, which flattens the difference between the first few ranks -
    the intent being that agreement between backends counts for more than a narrow win inside
    one.

    The surviving passage keeps the identity of its best-ranked appearance, and its ``backend``
    becomes a ``+``-joined list when more than one found it, because "both halves agreed" is the
    most useful thing a trace can say about a passage.
    """
    best: dict[tuple[str, str, str], Passage] = {}
    scores: dict[tuple[str, str, str], float] = {}
    backends: dict[tuple[str, str, str], list[str]] = {}
    for passages in ranked:
        for rank, passage in enumerate(passages):
            key = (passage.source_id, passage.source_version, passage.locator)
            scores[key] = scores.get(key, 0.0) + 1.0 / (constant + rank + 1)
            if key not in best:
                best[key] = passage
            if passage.backend and passage.backend not in backends.setdefault(key, []):
                backends[key].append(passage.backend)
    fused: list[Passage] = []
    for key, passage in best.items():
        fused.append(
            passage.model_copy(
                update={"score": scores[key], "backend": "+".join(backends.get(key, []))}
            )
        )
    fused.sort(key=lambda passage: passage.score, reverse=True)
    return fused


__all__ = ["ColbertRetriever", "DocumentRetriever", "Sessions", "fuse"]
