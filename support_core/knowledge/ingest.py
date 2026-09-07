"""The ingestion pipeline behind ``support pack knowledge sync``. Implements DESIGN.md section
9.3.

Section 9.3 is the whole specification: "fetch, normalize to markdown, chunk by headings (target
300 to 500 tokens), embed, upsert, and mark stale chunks." Everything below is one of those six
words, with three decisions the sentence does not make:

**``source_version`` is content-addressed.** ``r<revision>-<sha256[:12]>`` over the normalised
documents. Section 9.2 says a sync "re-indexes with a new ``source_version``" and section 9.3
says it "runs on a schedule"; those two together, with a version that is only a counter or a
timestamp, mean a daily job builds a new index every day whether or not anything changed, and
every in-flight conversation's next retrieval picks up a version that is identical to the one
before it. Hashing makes an unchanged sync a no-op that *keeps* the version, and makes any edit -
one word in one file - a version that is unmistakably different.

**Old chunks are marked stale, not deleted.** This is the phase's exit criterion, in one line of
SQL. A trace names the ``source_version`` its passages came from; if a re-sync deleted them, that
name would point at nothing and "a wrong answer is traceable to a source version" would be a
claim about a string rather than about a document. Retrieval filters ``stale = false``, so the
live version is what a customer's next question meets, and the previous versions are still there
to be read.

**The vector index is versioned by collection.** The sync builds ``<prefix><source>_v<n>`` and
then flips the alias, so the two stores agree about what "the live version" means at one instant
rather than over the length of an upsert. A crash between building the collection and flipping
the alias leaves an orphan collection nothing points at and a database that still describes the
previous version: no half-state, and the next sync builds ``_v<n+1>`` and moves on.

Fetching is behind :class:`Fetcher` so that ``html_crawl`` can be tested without the network. The
default implementation is the only place in this package that opens a socket to the outside world.
"""

import hashlib
import html
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urldefrag, urljoin, urlparse

from support_core.knowledge.chunking import chunk_markdown
from support_core.knowledge.embedding import Embedder, LateInteractionEncoder
from support_core.knowledge.qdrant import (
    QdrantStore,
    VectorPoint,
    alias_name,
    collection_name,
)
from support_core.knowledge.sources import (
    DocumentSource,
    HtmlCrawlSource,
    KnowledgeSources,
    MarkdownDirSource,
)
from support_core.knowledge.types import RetrieverUnavailable
from support_core.storage import knowledge_repo as repo

logger = logging.getLogger(__name__)

MARKDOWN_SUFFIXES = (".md", ".markdown")


@dataclass(frozen=True, slots=True)
class Document:
    """One fetched, normalised document, before chunking."""

    name: str
    """What a locator names it by: a path relative to the source, or a URL."""

    markdown: str


@dataclass(slots=True)
class SyncResult:
    """What one source's sync did. Printed by the CLI and asserted by the tests."""

    source_id: str
    version: str
    revision: int
    documents: int = 0
    chunks: int = 0
    marked_stale: int = 0
    unchanged: bool = False
    """The corpus hashed to what the last sync saw, so nothing was rewritten."""

    collection: str = ""
    vectors_indexed: int = 0
    vector_error: str | None = None
    """Set when Qdrant could not be reached. The sync still succeeds: the lexical and dense
    halves are in Postgres and answer without it, and a sync that failed outright would leave a
    pack unable to update its knowledge because a *secondary* index was down."""

    dropped_collections: list[str] = field(default_factory=list)
    dropped_versions: list[str] = field(default_factory=list)

    def line(self) -> str:
        if self.unchanged:
            return f"{self.source_id}: unchanged at {self.version}"
        parts = [
            f"{self.source_id}: {self.version}",
            f"{self.documents} document(s)",
            f"{self.chunks} chunk(s)",
            f"{self.marked_stale} marked stale",
        ]
        if self.vector_error:
            parts.append(f"vectors NOT indexed ({self.vector_error})")
        else:
            parts.append(f"{self.vectors_indexed} vector(s) in {self.collection}")
        return ", ".join(parts)


class Fetcher(Protocol):
    """Reads one URL. The seam that keeps the tests off the network."""

    async def __call__(self, url: str) -> str: ...


async def http_fetch(url: str) -> str:  # pragma: no cover - the one network call in the package
    """Fetch a page over HTTP. The default :class:`Fetcher`.

    ``httpx`` rather than ``urllib`` because it is already a dependency (the API tests drive the
    app through it) and because it does not block the loop.
    """
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


class _Extract(HTMLParser):
    """HTML to markdown, for the shapes a help centre actually uses.

    Not a general converter, and it should not become one. What a retrieval corpus needs from a
    help-centre page is the heading structure (so chunking by heading works and a locator can
    name a section), the paragraphs, the list items and the link text; what it must drop is
    script, style, navigation chrome and every attribute. A fuller conversion would add a
    dependency and a large surface for very little retrieval quality, and the alternative for a
    pack that needs one is to point a ``markdown_dir`` source at documents it controls.
    """

    _SKIP = frozenset({"script", "style", "noscript", "template", "svg"})
    _BLOCK = frozenset({"p", "div", "section", "article", "br", "tr", "table", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0
        self._heading: int | None = None
        self.title = ""
        self._in_title = False
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._heading = int(tag[1])
            self.parts.append("\n\n" + "#" * self._heading + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skipping = max(0, self._skipping - 1)
            return
        if tag == "title":
            self._in_title = False
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._heading = None
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skipping:
            return
        if self._in_title:
            self.title += data.strip()
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip() or (self.parts and not self.parts[-1].endswith(" ")):
            self.parts.append(text)

    def markdown(self) -> str:
        joined = "".join(self.parts)
        joined = html.unescape(joined)
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return "\n".join(line.rstrip() for line in joined.strip().splitlines())


def html_to_markdown(source: str) -> tuple[str, str, list[str]]:
    """``(markdown, title, hrefs)``."""
    parser = _Extract()
    parser.feed(source)
    parser.close()
    body = parser.markdown()
    if parser.title and not body.startswith("#"):
        body = f"# {parser.title}\n\n{body}"
    return body, parser.title, parser.links


async def read_markdown_dir(pack_path: Path, source: MarkdownDirSource) -> list[Document]:
    """Every ``*.md`` under the source's directory, in a stable order.

    Sorted by relative path, because the order decides ``chunk_index`` and the corpus checksum,
    and a filesystem's own order is not the same on two machines. The same reasoning as every
    other explicit ordering in this repository: an order that is reconstructed from the
    environment is not an order.
    """
    root = source.resolve(pack_path)
    if not root.is_dir():
        msg = f"{source.id}: {source.path} is not a directory"
        raise FileNotFoundError(msg)
    documents: list[Document] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in MARKDOWN_SUFFIXES or not path.is_file():
            continue
        documents.append(
            Document(
                name=path.relative_to(root).as_posix(),
                markdown=path.read_text(encoding="utf-8"),
            )
        )
    return documents


async def crawl(source: HtmlCrawlSource, fetch: Fetcher) -> list[Document]:
    """Breadth-first crawl from ``source.url``, normalised to markdown.

    Two bounds, both from the schema: ``depth`` and ``max_pages``. And one rule that is not in the
    schema because it is not negotiable - a link is followed only if it stays under the start
    URL's path. A help-centre page that links to a marketing site would otherwise put marketing
    copy in a policy corpus, and a passage the agent cites has to be something the pack's owner
    meant to publish as policy. It is not enough to stay on the host: ``/billing`` and
    ``/pricing`` are the same host and only one of them is the policy this pack answers from.

    "Under the path" is decided by :func:`_boundary`, which is the one judgement call in here.
    """
    start = urldefrag(source.url).url
    prefix = _boundary(start)
    origin = urlparse(start)
    seen: set[str] = {start}
    frontier: list[tuple[str, int]] = [(start, 1)]
    documents: list[Document] = []
    while frontier and len(documents) < source.max_pages:
        url, depth = frontier.pop(0)
        try:
            page = await fetch(url)
        except Exception as exc:
            # One unreachable page must not fail a crawl of fifty: a help centre with a broken
            # link is normal, and the alternative is a scheduled sync that never completes.
            logger.warning("crawl could not fetch %s: %s", url, exc)
            continue
        markdown, _, links = html_to_markdown(page)
        if markdown.strip():
            documents.append(Document(name=url, markdown=markdown))
        if depth >= source.depth:
            continue
        for href in links:
            target = urldefrag(urljoin(url, href)).url
            parsed = urlparse(target)
            if parsed.scheme not in ("http", "https") or parsed.netloc != origin.netloc:
                continue
            if not _under(target, prefix) or target in seen:
                continue
            seen.add(target)
            frontier.append((target, depth + 1))
    return documents


def _boundary(start: str) -> str:
    """The URL prefix a crawl may not leave, with no trailing slash.

    The start URL itself when it names a section (``/billing``, ``/billing/``), and its parent
    directory when it names a page (``/billing/index.html``) - because a pack that points at a
    section's front page means the section, and a crawl of depth two that could reach nothing
    would be a silently empty corpus.

    A last segment containing a dot is what "names a page" means here. It is a heuristic and it
    is the only one in this module; the case it gets wrong (a section literally called
    ``v1.2``) crawls less than the author wanted rather than more, which is the right direction
    for a rule about what may enter a policy corpus.
    """
    stripped = start.rstrip("/")
    parsed = urlparse(stripped)
    last = parsed.path.rsplit("/", 1)[-1]
    if "." in last:
        return stripped.rsplit("/", 1)[0]
    return stripped


def _under(target: str, prefix: str) -> bool:
    """Whether ``target`` is the boundary itself or something beneath it.

    The ``/`` matters: a plain ``startswith`` would let ``/billing-partners`` pass a boundary of
    ``/billing``.
    """
    return target == prefix or target.rstrip("/") == prefix or target.startswith(prefix + "/")


async def fetch_documents(
    pack_path: Path, source: DocumentSource, *, fetch: Fetcher
) -> list[Document]:
    if isinstance(source, MarkdownDirSource):
        return await read_markdown_dir(pack_path, source)
    return await crawl(source, fetch)


def corpus_checksum(documents: Sequence[Document]) -> str:
    """sha256 over the names and text of the whole corpus, in order.

    The *names* are hashed as well as the text, so renaming a file - which changes every locator
    it produces - is a new version even when the words did not change. A trace's locator has to
    keep meaning what it meant.
    """
    digest = hashlib.sha256()
    for document in documents:
        digest.update(document.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(document.markdown.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def version_string(revision: int, checksum: str) -> str:
    """``r3-1f4c8a90bb21``. Ordered by the revision, identified by the content."""
    return f"r{revision}-{checksum[:12]}"


@dataclass(slots=True)
class Ingestor:
    """One pack's knowledge sync (DESIGN.md section 9.3)."""

    pack_path: Path
    sessions: Any
    embedder: Embedder
    encoder: LateInteractionEncoder | None = None
    store: QdrantStore | None = None
    fetch: Fetcher = http_fetch
    now: Any = None
    """An injectable clock, as everywhere else in this repository."""

    def _clock(self) -> datetime:
        return self.now() if self.now is not None else datetime.now(UTC)

    async def sync(self, sources: KnowledgeSources, *, force: bool = False) -> list[SyncResult]:
        results: list[SyncResult] = []
        for source in sources.documents:
            results.append(await self.sync_source(source, force=force))
        return results

    async def sync_source(self, source: DocumentSource, *, force: bool = False) -> SyncResult:
        documents = await fetch_documents(self.pack_path, source, fetch=self.fetch)
        checksum = corpus_checksum(documents)

        async with self.sessions() as session, session.begin():
            existing = await repo.get_source(session, source.id)
            previous_revision = int(existing.revision) if existing is not None else 0
            unchanged = not force and existing is not None and existing.checksum == checksum
            if unchanged:
                return SyncResult(
                    source_id=source.id,
                    version=str(existing.version) if existing else "",
                    revision=previous_revision,
                    documents=len(documents),
                    unchanged=True,
                    collection=collection_name(source.id, previous_revision, prefix=self._prefix()),
                )

        revision = previous_revision + 1
        version = version_string(revision, checksum)
        chunks = _chunks_for(documents)
        texts = [text for _, text in chunks]
        embeddings = await self.embedder.embed_documents(texts) if texts else []

        result = SyncResult(
            source_id=source.id,
            version=version,
            revision=revision,
            documents=len(documents),
            chunks=len(chunks),
        )

        async with self.sessions() as session, session.begin():
            await repo.upsert_source(
                session,
                source_id=source.id,
                source_type=source.type,
                config=source.model_dump(mode="json"),
                version=version,
                revision=revision,
                checksum=checksum,
                now=self._clock(),
            )
            # Flushed before the chunks: `models.py` declares no relationships anywhere, so the
            # unit of work does not order a parent before its children and an unflushed
            # `doc_source` is a foreign key violation (phase-0 implementation note, which named
            # this phase).
            await session.flush()
            await repo.write_chunks(
                session,
                source_id=source.id,
                source_version=version,
                chunks=[
                    repo.ChunkWrite(
                        chunk_index=index,
                        locator=locator,
                        text=text,
                        embedding=embedding,
                    )
                    for index, ((locator, text), embedding) in enumerate(
                        zip(chunks, embeddings, strict=True)
                    )
                ],
            )
            await session.flush()
            result.marked_stale = await repo.mark_stale(
                session, source_id=source.id, keep_version=version
            )

        await self._index_vectors(source.id, revision, result)
        await self._prune(source.id, result)
        return result

    def _prefix(self) -> str:
        return self.store.prefix if self.store is not None else ""

    async def _index_vectors(self, source_id: str, revision: int, result: SyncResult) -> None:
        """Build the new collection and flip the alias onto it.

        The rows are read back from Postgres rather than carried over from the loop above, so the
        vectors are built from what was actually committed. Indexing what was *intended* to be
        committed is how two stores drift.
        """
        if self.store is None or self.encoder is None:
            result.vector_error = "no vector store configured"
            return
        name = collection_name(source_id, revision, prefix=self.store.prefix)
        result.collection = name
        try:
            async with self.sessions() as session, session.begin():
                rows = await repo.live_chunks(session, [source_id])
            vectors = await self.encoder.encode_documents([row.text for row in rows])
            dense = await self.embedder.embed_documents([row.text for row in rows])
            await self.store.create_collection(name, dimensions=self.embedder.dimensions)
            await self.store.upsert(
                name,
                [
                    VectorPoint(
                        id=str(row.id),
                        dense=one,
                        colbert=many,
                        payload={
                            "chunk_id": str(row.id),
                            "source_id": row.source_id,
                            "source_version": row.source_version,
                            "locator": row.locator,
                        },
                    )
                    for row, one, many in zip(rows, dense, vectors, strict=True)
                ],
            )
            await self.store.flip_alias(alias_name(source_id, prefix=self.store.prefix), name)
            result.vectors_indexed = len(rows)
        except RetrieverUnavailable as exc:
            # A sync that failed outright because a secondary index was down would leave a pack
            # unable to correct its knowledge for as long as Qdrant was unavailable. The lexical
            # and dense halves are already committed in Postgres and answer without it.
            logger.warning("vector index for %s not built: %s", source_id, exc)
            result.vector_error = str(exc)

    async def _prune(self, source_id: str, result: SyncResult) -> None:
        """Retention. Collections and rows are pruned to the same depth, in that order.

        Collections first: an alias never points at a pruned one (the flip has already happened),
        and a collection whose rows are gone is worse than a row whose collection is gone -
        the second degrades to the lexical half, the first returns hits that cannot be read.
        """
        keep = self.store.keep if self.store is not None else 3
        if self.store is not None:
            try:
                result.dropped_collections = await self.store.prune(source_id, keep=keep)
            except RetrieverUnavailable as exc:
                logger.warning("could not prune collections for %s: %s", source_id, exc)
        async with self.sessions() as session, session.begin():
            stale = await repo.stale_versions(session, source_id, keep=max(0, keep - 1))
            for version in stale:
                await repo.drop_version(session, source_id=source_id, source_version=version)
            result.dropped_versions = stale


def _chunks_for(documents: Sequence[Document]) -> list[tuple[str, str]]:
    """``(locator, indexed text)`` for the whole corpus, in document then chunk order."""
    pieces: list[tuple[str, str]] = []
    for document in documents:
        for chunk in chunk_markdown(document.markdown):
            pieces.append((chunk.locator(document.name), chunk.with_heading()))
    return pieces
