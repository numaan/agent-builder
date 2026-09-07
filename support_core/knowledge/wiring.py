"""Building the knowledge layer from a pack. Implements the composition half of DESIGN.md
section 9.1's "packs choose which backends a given ``llm`` node may use".

Deliberately the same shape as :mod:`support_core.llm.wiring`: one function that turns a
:class:`~support_core.graph.pack.Pack` plus a session factory into the object the engine holds,
so the service, the ingestion CLI and the tests all compose the layer the same way and there is
one place to read to find out what a deployment is actually running.

Which backends a pack gets is decided by what it declares, and by nothing else:

* a pack with **document sources** gets :class:`~support_core.knowledge.document.DocumentRetriever`
  (Postgres: lexical over ``tsv``, dense over ``embedding``) and, when Qdrant is configured,
  :class:`~support_core.knowledge.document.ColbertRetriever` beside it;
* a pack with **live lookups** gets :class:`~support_core.knowledge.live.LiveLookupRetriever`,
  built *per node* by the executor rather than here, because that retriever calls READ tools and
  a tool call belongs to the turn whose budget it spends;
* a pack with neither gets ``None``, and an ``llm`` node with a ``knowledge:`` block then sees an
  empty layer 7. That is not a hole: the citation guardrail of section 9.2 refuses the first
  factual claim made with nothing to cite, so the conversation reaches a person.

**Which encoder.** :class:`~support_core.knowledge.embedding.DeterministicEncoder`, which
implements both protocols and needs no model and no download. That is a limitation and it is the
one the ColBERT decision was taken to remove: this deployment has no embedding API, and the local
model that would replace it (:class:`~support_core.knowledge.embedding.FastEmbedColbertEncoder`)
cannot fetch its weights from this network. Swapping it is one argument to
:func:`build_retriever`, which is what makes the seam real rather than aspirational.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from support_core.knowledge.composite import CompositeRetriever
from support_core.knowledge.config import qdrant_url as default_qdrant_url
from support_core.knowledge.document import ColbertRetriever, DocumentRetriever
from support_core.knowledge.embedding import DeterministicEncoder, Embedder, LateInteractionEncoder
from support_core.knowledge.ingest import Fetcher, Ingestor, http_fetch
from support_core.knowledge.qdrant import DEFAULT_PREFIX, QdrantStore
from support_core.knowledge.sources import KnowledgeSources


def default_encoder() -> DeterministicEncoder:
    """The encoder this deployment runs, in one place so the sync and the query cannot differ.

    They must not differ. A corpus indexed with one projection and queried with another returns
    nothing and looks exactly like a corpus with no matches, so the two sides share a constructor
    rather than each naming a class.
    """
    return DeterministicEncoder()


def build_store(
    url: str | None = None, *, prefix: str = DEFAULT_PREFIX, keep: int | None = None
) -> QdrantStore:
    """The Qdrant client wrapper, from ``SUPPORT_QDRANT_URL`` unless told otherwise.

    Always returns a store, never ``None``: whether Qdrant *answers* is a per-call fact that
    :class:`~support_core.knowledge.composite.CompositeRetriever` handles by dropping the backend,
    and deciding at startup that it is absent would turn a restart during an outage into a
    deployment that never uses the vector side again.
    """
    resolved = url or default_qdrant_url()
    if keep is None:
        return QdrantStore(resolved, prefix=prefix)
    return QdrantStore(resolved, prefix=prefix, keep=keep)


def build_retriever(
    sources: KnowledgeSources,
    sessions: Any,
    *,
    embedder: Embedder | None = None,
    encoder: LateInteractionEncoder | None = None,
    store: QdrantStore | None = None,
    use_qdrant: bool = True,
) -> CompositeRetriever | None:
    """The retriever the engine holds, or ``None`` for a pack with no knowledge at all.

    ``store`` defaults to :func:`build_store`. Pass ``use_qdrant=False`` for a deployment that
    genuinely has no Qdrant - which is different from one whose Qdrant is down, and is treated
    differently: the first is a configuration and is silent, the second is a fault and is logged
    on every call it degrades (see :mod:`support_core.knowledge.config`).
    """
    if not sources.documents and not sources.live_lookups:
        return None
    chosen = _default(embedder, encoder)
    source_ids = sources.source_ids()
    backends: list[Any] = []
    if source_ids:
        backends.append(DocumentRetriever(sessions, embedder=chosen[0], source_ids=source_ids))
        if use_qdrant:
            backends.append(
                ColbertRetriever(
                    store if store is not None else build_store(),
                    sessions,
                    encoder=chosen[1],
                    embedder=chosen[0],
                    source_ids=source_ids,
                )
            )
    return CompositeRetriever(backends)


def build_ingestor(
    pack_path: Path,
    sessions: Any,
    *,
    embedder: Embedder | None = None,
    encoder: LateInteractionEncoder | None = None,
    store: QdrantStore | None = None,
    use_qdrant: bool = True,
    fetch: Fetcher = http_fetch,
    now: Any = None,
) -> Ingestor:
    """The sync pipeline behind ``support pack knowledge sync`` (DESIGN.md section 9.3).

    ``use_qdrant=False`` builds a sync that indexes the Postgres halves and nothing else. Same
    distinction as :func:`build_retriever`: not having a Qdrant is a configuration, and it is why
    this is a separate flag rather than ``store=None`` - a default argument cannot tell "I did
    not pass one" from "there is not one".
    """
    chosen = _default(embedder, encoder)
    if use_qdrant and store is None:
        store = build_store()
    return Ingestor(
        pack_path=pack_path,
        sessions=sessions,
        embedder=chosen[0],
        encoder=chosen[1],
        store=store if use_qdrant else None,
        fetch=fetch,
        now=now,
    )


def _default(
    embedder: Embedder | None, encoder: LateInteractionEncoder | None
) -> tuple[Embedder, LateInteractionEncoder]:
    """One :class:`DeterministicEncoder` for both halves unless a caller supplied its own.

    One object rather than two, because the deterministic encoder's dense vector is the sum of
    the same token vectors its multivector holds; building two would spend the work twice and
    invite a deployment that configured one and forgot the other.
    """
    if embedder is not None and encoder is not None:
        return embedder, encoder
    fallback = default_encoder()
    return embedder or fallback, encoder or fallback


__all__: Sequence[str] = [
    "build_ingestor",
    "build_retriever",
    "build_store",
    "default_encoder",
]
