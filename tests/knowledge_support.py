"""Shared scaffolding for the knowledge-layer tests. Phase 5.

Two things everything here needs and one thing it deliberately refuses to do.

**A corpus on disk.** Ingestion reads a directory, so the tests write one into ``tmp_path`` and
sync it with the real :class:`~support_core.knowledge.ingest.Ingestor`. Nothing constructs a
``doc_chunk`` row by hand: the phase's claims are about what the *pipeline* produces, and a test
that inserted rows itself would prove them about the test.

**A Qdrant that may or may not be there.** Every collection a test touches carries a per-run
random prefix, so a suite run against a developer's own instance leaves their collections alone
and two suites cannot collide. When Qdrant does not answer, the vector tests skip and the rest
run - which is the same degradation the product claims, exercised by the test suite itself.

What it refuses to do is mock the retriever. ``FakeRetriever`` below exists only for tests about
*the engine's* behaviour when retrieval fails or returns nothing, where a real corpus would be
scaffolding around the thing being asked about.
"""

import secrets
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.knowledge.composite import CompositeRetriever
from support_core.knowledge.config import test_qdrant_url
from support_core.knowledge.document import ColbertRetriever, DocumentRetriever
from support_core.knowledge.embedding import DeterministicEncoder
from support_core.knowledge.ingest import Document, Ingestor, SyncResult
from support_core.knowledge.qdrant import QdrantStore
from support_core.knowledge.sources import KnowledgeSources, MarkdownDirSource, parse_sources
from support_core.knowledge.types import Passage, RetrievalRequest, RetrieverUnavailable
from support_core.storage.session import make_session_factory

DOCS = "knowledge/docs"


def write_corpus(root: Path, documents: dict[str, str], *, subdir: str = DOCS) -> Path:
    """Write ``{relative path: markdown}`` under ``root/<subdir>`` and return the pack root."""
    target = root / subdir
    target.mkdir(parents=True, exist_ok=True)
    for name, text in documents.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def markdown_source(source_id: str = "policy-docs", *, subdir: str = DOCS) -> MarkdownDirSource:
    return MarkdownDirSource(id=source_id, type="markdown_dir", path=f"./{subdir}")


def sources_for(source_id: str = "policy-docs", *, subdir: str = DOCS) -> KnowledgeSources:
    return parse_sources(
        {"documents": [{"id": source_id, "type": "markdown_dir", "path": f"./{subdir}"}]}
    )


def ingestor(
    engine: AsyncEngine,
    pack_path: Path,
    *,
    store: QdrantStore | None = None,
    fetch: Any = None,
) -> Ingestor:
    """An ingestor over the test database, with the deterministic encoder on both halves."""
    encoder = DeterministicEncoder()
    built = Ingestor(
        pack_path=pack_path,
        sessions=make_session_factory(engine),
        embedder=encoder,
        encoder=encoder,
        store=store,
    )
    if fetch is not None:
        built.fetch = fetch
    return built


def document_retriever(engine: AsyncEngine, source_ids: Sequence[str] = ()) -> DocumentRetriever:
    return DocumentRetriever(
        make_session_factory(engine), embedder=DeterministicEncoder(), source_ids=source_ids
    )


def colbert_retriever(
    engine: AsyncEngine, store: QdrantStore, source_ids: Sequence[str]
) -> ColbertRetriever:
    encoder = DeterministicEncoder()
    return ColbertRetriever(
        store,
        make_session_factory(engine),
        encoder=encoder,
        embedder=encoder,
        source_ids=source_ids,
        # No dense first stage. The corpora here are a dozen chunks, so MaxSim is measured
        # against every candidate rather than against whatever the first stage happened to like -
        # which is what makes a claim about late interaction a claim about late interaction.
        prefetch=0,
    )


def request(query: str, k: int = 3, *, node_id: str = "test") -> RetrievalRequest:
    from support_core.graph.context import ConversationContext

    return RetrievalRequest(query=query, ctx=ConversationContext(), k=k, node_id=node_id)


async def synced(
    engine: AsyncEngine,
    tmp_path: Path,
    documents: dict[str, str],
    *,
    store: QdrantStore | None = None,
    source_id: str = "policy-docs",
    force: bool = False,
) -> SyncResult:
    """Write a corpus and sync it. The one way these tests get rows into ``doc_chunk``."""
    write_corpus(tmp_path, documents)
    return await ingestor(engine, tmp_path, store=store).sync_source(
        markdown_source(source_id), force=force
    )


# -- Qdrant ------------------------------------------------------------------------------------


async def qdrant_available(store: QdrantStore) -> bool:
    return await store.ping()


def scratch_store(*, keep: int = 3) -> QdrantStore:
    """A store whose collections are namespaced to this test, and to nothing else.

    ``kb_test_<8 hex>_``. Every collection it creates is dropped by the fixture; a collection a
    developer created is not something these tests can name, let alone delete.
    """
    return QdrantStore(test_qdrant_url(), prefix=f"kb_test_{secrets.token_hex(4)}_", keep=keep)


async def drop_prefixed(store: QdrantStore) -> None:
    """Remove every collection this store's prefix owns."""
    try:
        client = store._connect()
        listing = await store._call("get_collections", client.get_collections)
    except RetrieverUnavailable:  # pragma: no cover - nothing to clean if it is not there
        return
    for description in listing.collections:
        name = str(description.name)
        if name.startswith(store.prefix):
            await store.drop(name)


# The ``qdrant`` fixture itself lives in tests/conftest.py, because pytest only collects
# fixtures from conftest files and plugins. What lives here is what it is built from.


def unreachable_store() -> QdrantStore:
    """A store pointing at a port nothing listens on. Never skipped: an outage is always testable.

    Port 1 rather than a random high port, because a random high port may be in use by something
    on a developer's machine and "connection refused" is what this needs to be certain of.
    """
    return QdrantStore("http://127.0.0.1:1", prefix="kb_unreachable_", timeout=1.0)


# -- doubles for engine-level tests ------------------------------------------------------------


class FakeRetriever:
    """Returns what it was given, or raises. For tests about the engine, not about retrieval."""

    def __init__(
        self, passages: Sequence[Passage] = (), *, name: str = "fake", fails: bool = False
    ) -> None:
        self.name = name
        self.passages = list(passages)
        self.fails = fails
        self.queries: list[str] = []

    async def retrieve(self, req: RetrievalRequest) -> Sequence[Passage]:
        self.queries.append(req.query)
        if self.fails:
            msg = f"{self.name} is down"
            raise RetrieverUnavailable(msg)
        return self.passages[: req.k]


def passage(
    text: str,
    *,
    source_id: str = "policy-docs",
    version: str = "r1-aaaaaaaaaaaa",
    locator: str = "policy.md#Refunds",
    score: float = 1.0,
) -> Passage:
    return Passage(
        text=text, source_id=source_id, source_version=version, locator=locator, score=score
    )


def composite(*retrievers: Any) -> CompositeRetriever:
    return CompositeRetriever(list(retrievers))


def document(name: str, markdown: str) -> Document:
    return Document(name=name, markdown=markdown)


def conversation_id() -> uuid.UUID:
    return uuid.uuid4()
