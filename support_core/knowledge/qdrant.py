"""The vector side of the knowledge layer. Implements DESIGN.md section 9.1's dense and
late-interaction retrieval, on the store BACKLOG.md's decisions log chose on 2026-09-06.

Qdrant stores multivectors and scores MaxSim natively, which is what makes ColBERT an ordinary
query against an ordinary service rather than an index directory the deployment has to build,
ship, version and back up itself. Three properties of this module are the reason it was chosen
over the alternatives, and each is enforced here rather than assumed:

* **Versions are collections, not rows.** A sync builds ``<prefix><source>_v<n+1>`` and then
  flips the alias ``<prefix><source>`` onto it in a single Qdrant call. Old and new genuinely
  coexist until a retention sweep removes the old one, which is what makes "an older trace still
  names the old version" exact rather than approximate: the collection that produced that answer
  is still there and still contains exactly what it contained.
* **An unreachable Qdrant raises one core exception**
  (:class:`~support_core.knowledge.types.RetrieverUnavailable`) and never an httpx or grpc one,
  because :class:`~support_core.knowledge.composite.CompositeRetriever` catches exactly that type
  to drop a backend and answer from the rest. A leaked transport exception would turn graceful
  degradation into a node error.
* **Nothing here is in the write path of a turn.** DESIGN.md 7.1 requires the frame stack and the
  trace step in one transaction and resume to derive from durable state; every call in this
  module is either a read during node execution or a sync run from the CLI. That is the
  distinction the decisions log drew from phase 10's mem0 question, and it is why a second
  stateful dependency is acceptable at all.

The client is imported lazily. A deployment with no Qdrant - a pack with no knowledge sources,
or the lexical-only degraded mode - should not need the dependency present to start.
"""

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from support_core.knowledge.types import RetrieverUnavailable

logger = logging.getLogger(__name__)

DENSE_VECTOR = "dense"
COLBERT_VECTOR = "colbert"
"""The two named vectors on every point. One collection carries both, so a sync writes the corpus
once and either retriever can query it; splitting them would double the ingestion cost and make
the alias flip two operations that can half-fail."""

DEFAULT_PREFIX = "kb_"
KEEP_COLLECTIONS = 3
"""Versions retained per source, newest first.

Not one, because the exit criterion needs the old collection to still exist; not unbounded,
because a daily-refreshed source would otherwise accumulate a collection a day forever. Three is
a week of a weekly refresh and three days of a daily one, and it is a constructor argument for a
deployment with a different retention policy."""

_SAFE = re.compile(r"[^a-z0-9_]+")


def collection_name(source_id: str, revision: int, *, prefix: str = DEFAULT_PREFIX) -> str:
    """``kb_policy_docs_v4``. Deterministic, so a sync and a query agree without being told."""
    return f"{prefix}{_SAFE.sub('_', source_id.lower())}_v{revision}"


def alias_name(source_id: str, *, prefix: str = DEFAULT_PREFIX) -> str:
    """``kb_policy_docs``. What a query names; the sync decides what it points at."""
    return f"{prefix}{_SAFE.sub('_', source_id.lower())}"


@dataclass(frozen=True, slots=True)
class VectorHit:
    """One Qdrant point, as the retrievers want it."""

    score: float
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VectorPoint:
    """One chunk, ready to upsert."""

    id: str
    dense: Sequence[float]
    colbert: Sequence[Sequence[float]]
    payload: Mapping[str, Any]


class QdrantStore:
    """Everything this system does to Qdrant, in one object.

    Synchronous client calls are pushed to a worker thread with :func:`asyncio.to_thread` rather
    than using the async client. The reason is narrow and worth stating: the async client holds
    its own connection pool bound to the loop that created it, and this repository builds and
    disposes engines per test with ``NullPool`` for exactly that class of problem (see
    ``tests/conftest.py``). A thread hop costs a few hundred microseconds against a four-second
    p95 budget and buys a client with no loop affinity at all.
    """

    def __init__(
        self,
        url: str,
        *,
        prefix: str = DEFAULT_PREFIX,
        keep: int = KEEP_COLLECTIONS,
        timeout: float = 10.0,
    ) -> None:
        self.url = url
        self.prefix = prefix
        self.keep = keep
        self.timeout = timeout
        self._client: Any | None = None

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover - the dependency is not optional
            msg = "qdrant-client is not installed; the vector side of retrieval cannot run"
            raise RetrieverUnavailable(msg) from exc
        # `check_compatibility=False` skips a client-server version handshake on construction.
        # It is off because the handshake is a *network call in a constructor*: against an
        # unreachable Qdrant it warns on stderr and the failure is then reported twice, once as a
        # warning nobody can act on and once as the RetrieverUnavailable that actually matters.
        self._client = QdrantClient(
            url=self.url, timeout=int(self.timeout), check_compatibility=False
        )
        return self._client

    async def _call(self, name: str, function: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one client call off the loop, and turn any transport failure into one type.

        The broad ``except Exception`` is deliberate and is the point of the method. The client
        raises ``ResponseHandlingException``, ``UnexpectedResponse``, ``httpx.ConnectError`` and
        several others depending on how it failed, and the caller - the composite retriever -
        needs one predicate for "this backend is not answering". Anything narrower here would
        mean a new Qdrant version's new exception class reaching a node as an engine error and
        parking a conversation, which is the opposite of what the split between the lexical and
        vector halves was for.
        """
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except RetrieverUnavailable:
            raise
        except Exception as exc:
            logger.warning("qdrant %s failed against %s: %s", name, self.url, exc)
            msg = f"qdrant {name} failed against {self.url}: {exc}"
            raise RetrieverUnavailable(msg) from exc

    async def ping(self) -> bool:
        """Whether Qdrant answers. Used by the CLI and by the tests' skip guard, never by a turn."""
        try:
            client = self._connect()
            await self._call("get_collections", client.get_collections)
        except RetrieverUnavailable:
            return False
        return True

    async def create_collection(self, name: str, *, dimensions: int) -> None:
        """One collection carrying both a dense vector and a MaxSim-scored multivector."""
        from qdrant_client import models

        client = self._connect()
        await self._call(
            "recreate_collection",
            client.create_collection,
            collection_name=name,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(size=dimensions, distance=models.Distance.COSINE),
                COLBERT_VECTOR: models.VectorParams(
                    size=dimensions,
                    distance=models.Distance.COSINE,
                    multivector_config=models.MultiVectorConfig(
                        comparator=models.MultiVectorComparator.MAX_SIM
                    ),
                    # The ColBERT vectors are not HNSW-indexed. Qdrant's own guidance for late
                    # interaction is to use it as a reranker over an indexed first stage, and an
                    # HNSW graph over every token of every chunk costs far more to build than a
                    # policy corpus of this size can repay. The first stage here is the dense
                    # vector and the lexical index; this is the rescoring pass.
                    hnsw_config=models.HnswConfigDiff(m=0),
                ),
            },
        )

    async def collection_exists(self, name: str) -> bool:
        client = self._connect()
        exists = await self._call("collection_exists", client.collection_exists, name)
        return bool(exists)

    async def upsert(self, name: str, points: Sequence[VectorPoint], *, batch: int = 64) -> None:
        from qdrant_client import models

        client = self._connect()
        for start in range(0, len(points), batch):
            window = points[start : start + batch]
            await self._call(
                "upsert",
                client.upsert,
                collection_name=name,
                points=[
                    models.PointStruct(
                        id=point.id,
                        vector={
                            DENSE_VECTOR: list(point.dense),
                            COLBERT_VECTOR: [list(row) for row in point.colbert],
                        },
                        payload=dict(point.payload),
                    )
                    for point in window
                ],
                wait=True,
            )

    async def flip_alias(self, alias: str, name: str) -> None:
        """Point ``alias`` at ``name``, atomically.

        Delete-then-create in one ``update_collection_aliases`` call, which Qdrant applies as one
        change. Two calls would leave a window in which the alias resolves to nothing and every
        retrieval in flight fell back to the lexical half for no reason.
        """
        from qdrant_client import models

        client = self._connect()
        await self._call(
            "update_collection_aliases",
            client.update_collection_aliases,
            change_aliases_operations=[
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)),
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(collection_name=name, alias_name=alias)
                ),
            ],
        )

    async def prune(self, source_id: str, *, keep: int | None = None) -> list[str]:
        """Drop all but the newest ``keep`` collections of one source. Returns what it dropped."""
        limit = self.keep if keep is None else keep
        client = self._connect()
        listing = await self._call("get_collections", client.get_collections)
        stem = f"{alias_name(source_id, prefix=self.prefix)}_v"
        versions: list[tuple[int, str]] = []
        for description in listing.collections:
            name = str(description.name)
            if not name.startswith(stem):
                continue
            suffix = name[len(stem) :]
            if suffix.isdigit():
                versions.append((int(suffix), name))
        dropped: list[str] = []
        for _, name in sorted(versions, reverse=True)[limit:]:
            await self._call("delete_collection", client.delete_collection, name)
            dropped.append(name)
        return dropped

    async def drop(self, name: str) -> None:
        client = self._connect()
        await self._call("delete_collection", client.delete_collection, name)

    async def search_dense(self, name: str, vector: Sequence[float], limit: int) -> list[VectorHit]:
        """Cosine nearest neighbours over the one-vector-per-chunk side."""
        return await self._query(name, query=list(vector), using=DENSE_VECTOR, limit=limit)

    async def search_colbert(
        self,
        name: str,
        *,
        dense: Sequence[float],
        colbert: Sequence[Sequence[float]],
        limit: int,
        prefetch: int = 0,
    ) -> list[VectorHit]:
        """Late interaction, in the two-stage form Qdrant's multivector support is built for.

        ``prefetch`` candidates are pulled by the HNSW-indexed dense vector and only those are
        rescored with MaxSim. That is what keeps a ColBERT query inside DESIGN.md section 20's
        four-second budget: scoring every point's multivector is a linear scan of the corpus, and
        the multivector deliberately has no HNSW graph of its own (see
        :meth:`create_collection`). ``prefetch=0`` asks for the linear scan explicitly, which is
        what the tests use on a corpus of a dozen chunks so the rescoring is measured against
        every candidate rather than against whatever the first stage happened to like.
        """
        from qdrant_client import models

        rows = [list(row) for row in colbert]
        if not rows:
            return []
        stages = (
            [models.Prefetch(query=list(dense), using=DENSE_VECTOR, limit=prefetch)]
            if prefetch
            else None
        )
        return await self._query(
            name, query=rows, using=COLBERT_VECTOR, limit=limit, prefetch=stages
        )

    async def _query(
        self, name: str, *, query: Any, using: str, limit: int, prefetch: Any = None
    ) -> list[VectorHit]:
        client = self._connect()
        response = await self._call(
            "query_points",
            client.query_points,
            collection_name=name,
            query=query,
            using=using,
            limit=limit,
            with_payload=True,
            prefetch=prefetch,
        )
        return [
            VectorHit(score=float(point.score), payload=dict(point.payload or {}))
            for point in response.points
        ]
