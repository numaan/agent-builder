"""The ingestion pipeline. DESIGN.md section 9.3, and the exit criterion's storage half.

    ``support pack knowledge sync`` is a CLI in core: fetch, normalize to markdown, chunk by
    headings (target 300 to 500 tokens), embed, upsert, and mark stale chunks. - DESIGN.md 9.3

Everything here runs against real Postgres, per PLAN.md, and everything gets rows into
``doc_chunk`` by running the real sync. The claims being tested are about what a *re-sync* does,
and a test that inserted its own rows would be testing its own fixture.

The one seam is the network: ``html_crawl`` fetches through an injectable ``Fetcher``, so a crawl
is exercised without a web server and without depending on somebody else's uptime.
"""

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.knowledge.embedding import DIMENSIONS, DeterministicEncoder
from support_core.knowledge.ingest import (
    corpus_checksum,
    crawl,
    html_to_markdown,
    read_markdown_dir,
    version_string,
)
from support_core.knowledge.sources import HtmlCrawlSource, parse_sources
from support_core.storage import knowledge_repo as repo
from support_core.storage.models import DocChunk, DocSource
from support_core.storage.session import make_session_factory
from tests.knowledge_support import (
    document,
    ingestor,
    markdown_source,
    synced,
    write_corpus,
)

MIGRATIONS = Path(__file__).resolve().parent.parent / "support_core/storage/migrations/versions"

POLICY_V1 = """# Refund policy

## Refund window

A charge can be refunded within 60 days of the payment.

## Duplicate charges

A duplicate charge is always refundable.
"""

POLICY_V2 = POLICY_V1.replace("60 days", "90 days")

TIMING = """# Processing times

## Card refunds

A refund to a card takes five to seven business days.
"""


def load_migration(name: str) -> Any:
    """Import one Alembic revision by file name.

    They are not importable as modules - ``0010_knowledge_index`` is not an identifier - so this
    loads the file the way Alembic itself does. Worth the four lines: the constant it defines is
    what the database column's width *is*, and a test that copied the number instead of reading
    it would agree with itself for ever.
    """
    import importlib.util

    path = MIGRATIONS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_migration_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def chunks(engine: AsyncEngine, source_id: str = "policy-docs") -> list[dict[str, Any]]:
    async with make_session_factory(engine)() as session, session.begin():
        rows = (
            await session.execute(
                select(
                    DocChunk.source_version, DocChunk.locator, DocChunk.text, DocChunk.stale
                ).where(DocChunk.source_id == source_id)
            )
        ).all()
    return [
        {"version": r[0], "locator": r[1], "text": r[2], "stale": r[3]}
        for r in sorted(rows, key=lambda r: (r[0], r[1]))
    ]


async def source_row(engine: AsyncEngine, source_id: str = "policy-docs") -> DocSource | None:
    async with make_session_factory(engine)() as session, session.begin():
        return await repo.get_source(session, source_id)


# -- markdown_dir ------------------------------------------------------------------------------


async def test_a_first_sync_writes_a_versioned_corpus(engine: AsyncEngine, tmp_path: Path) -> None:
    result = await synced(engine, tmp_path, {"policy.md": POLICY_V1, "timing.md": TIMING})

    assert result.documents == 2
    assert result.chunks == 3
    assert result.marked_stale == 0
    assert result.version.startswith("r1-")
    assert result.unchanged is False

    row = await source_row(engine)
    assert row is not None
    assert row.version == result.version
    assert row.revision == 1
    assert row.checksum is not None

    written = await chunks(engine)
    assert len(written) == 3
    assert {chunk["version"] for chunk in written} == {result.version}
    assert all(chunk["stale"] is False for chunk in written)
    assert "policy.md#Refund policy > Refund window" in {chunk["locator"] for chunk in written}


async def test_a_re_sync_of_unchanged_content_is_a_no_op_that_keeps_the_version(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """DESIGN.md 9.3 runs this "on a schedule".

    A daily job over a corpus nobody edited must not build a new index every day: every in-flight
    conversation's next retrieval would pick up a version identical to the last one, and the
    retention sweep would throw away the versions old traces name. Content-addressing is what
    makes the no-op decidable rather than guessed at.
    """
    first = await synced(engine, tmp_path, {"policy.md": POLICY_V1})
    again = await synced(engine, tmp_path, {"policy.md": POLICY_V1})

    assert again.unchanged is True
    assert again.version == first.version
    assert again.chunks == 0
    assert [chunk["version"] for chunk in await chunks(engine)] == [first.version] * 2


async def test_editing_one_word_produces_a_new_version_and_marks_the_old_one_stale(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The mechanism the phase's exit criterion rests on, at the storage level.

    Old rows are *marked*, not deleted. A trace naming the old version has to keep pointing at
    text somebody can read, or "traceable to a source version" is a claim about a string.
    """
    first = await synced(engine, tmp_path, {"policy.md": POLICY_V1})
    second = await synced(engine, tmp_path, {"policy.md": POLICY_V2})

    assert second.version != first.version
    assert second.revision == 2
    assert second.marked_stale == 2

    rows = await chunks(engine)
    old = [row for row in rows if row["version"] == first.version]
    new = [row for row in rows if row["version"] == second.version]
    assert old and new
    assert all(row["stale"] is True for row in old)
    assert all(row["stale"] is False for row in new)
    assert any("60 days" in row["text"] for row in old)
    assert any("90 days" in row["text"] for row in new)
    assert not any("60 days" in row["text"] for row in new)


async def test_renaming_a_file_is_a_new_version_even_when_the_words_did_not_change(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Every locator the file produced changes, so a trace's locator would stop meaning what it
    meant. The checksum covers the names for exactly this."""
    first = await synced(engine, tmp_path, {"policy.md": POLICY_V1})
    (tmp_path / "knowledge" / "docs" / "policy.md").unlink()
    second = await synced(engine, tmp_path, {"rules.md": POLICY_V1})
    assert second.version != first.version


async def test_a_forced_sync_re_indexes_content_that_did_not_change(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The escape hatch for the case content-addressing cannot see: the *chunker* changed.

    A code change is a deploy, not an edit, so the corpus hash is the same and the stored chunks
    are the old shape. ``--force`` is how an operator rebuilds them, and the revision still
    advances so the two indexes are distinguishable.
    """
    first = await synced(engine, tmp_path, {"policy.md": POLICY_V1})
    forced = await synced(engine, tmp_path, {"policy.md": POLICY_V1}, force=True)
    assert forced.unchanged is False
    assert forced.revision == first.revision + 1
    assert forced.version != first.version
    assert forced.marked_stale == 2


async def test_a_source_directory_that_is_not_there_is_a_readable_failure(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError, match="is not a directory"):
        await ingestor(engine, tmp_path).sync_source(markdown_source())


async def test_documents_are_read_in_a_stable_order(tmp_path: Path) -> None:
    """The order decides ``chunk_index`` and the corpus checksum, so it cannot come from the
    filesystem - two machines do not agree about that."""
    write_corpus(tmp_path, {"b.md": "# B\n\nb\n", "a.md": "# A\n\na\n", "sub/c.md": "# C\n\nc\n"})
    read = await read_markdown_dir(tmp_path, markdown_source())
    assert [doc.name for doc in read] == ["a.md", "b.md", "sub/c.md"]


def test_the_corpus_checksum_notices_a_word_and_a_name() -> None:
    base = [document("a.md", "# A\n\nhello\n")]
    assert corpus_checksum(base) == corpus_checksum([document("a.md", "# A\n\nhello\n")])
    assert corpus_checksum(base) != corpus_checksum([document("a.md", "# A\n\nhello!\n")])
    assert corpus_checksum(base) != corpus_checksum([document("b.md", "# A\n\nhello\n")])


def test_a_version_string_is_ordered_and_content_addressed() -> None:
    assert version_string(3, "1f4c8a90bb21ff") == "r3-1f4c8a90bb21"


# -- embeddings --------------------------------------------------------------------------------


async def test_every_chunk_is_written_with_a_vector_of_the_column_s_width(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    await synced(engine, tmp_path, {"policy.md": POLICY_V1})
    async with make_session_factory(engine)() as session, session.begin():
        rows = (await session.execute(select(DocChunk.embedding))).scalars().all()
    assert rows
    assert all(vector is not None and len(vector) == DIMENSIONS for vector in rows)


async def test_a_vector_of_the_wrong_width_is_refused_by_name(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Phase-0 finding N12's other half. A dimensionless column accepted mixed rows and failed at
    *query* time; the refusal now names the migration and happens before anything is written."""

    class NarrowEmbedder:
        name = "narrow"
        dimensions = 8

        async def embed_documents(self, texts: Any) -> list[list[float]]:
            return [[0.1] * 8 for _ in texts]

        async def embed_query(self, text: str) -> list[float]:
            return [0.1] * 8

    write_corpus(tmp_path, {"policy.md": POLICY_V1})
    built = ingestor(engine, tmp_path)
    built.embedder = NarrowEmbedder()
    with pytest.raises(ValueError, match="0010_knowledge_index"):
        await built.sync_source(markdown_source())


def test_the_encoder_and_the_migration_agree_about_the_width() -> None:
    """Two constants that must not drift: the column is ``vector(128)`` and the encoder emits 128.

    A drift here is a column that accepts nothing the encoder produces, discovered on the first
    sync of a deployment rather than here.
    """
    migration = load_migration("0010_knowledge_index")
    assert migration.EMBEDDING_DIMENSIONS == DIMENSIONS
    assert DeterministicEncoder().dimensions == DIMENSIONS


# -- html_crawl --------------------------------------------------------------------------------

PAGE = """<html><head><title>Billing help</title><script>evil()</script></head>
<body><nav>Home</nav><h1>Billing help</h1><p>Refunds take five to seven business days.</p>
<h2>Fees</h2><ul><li>No setup fee.</li></ul>
<a href="/billing/refunds">Refunds</a><a href="https://elsewhere.example/ad">Ad</a>
<a href="/pricing">Pricing</a></body></html>"""

REFUNDS = """<html><head><title>Refunds</title></head><body>
<h1>Refunds</h1><p>A duplicate charge is always refundable.</p></body></html>"""


class StubFetcher:
    """The seam that keeps the crawl off the network."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    async def __call__(self, url: str) -> str:
        self.asked.append(url)
        try:
            return self.pages[url]
        except KeyError:
            msg = f"404 {url}"
            raise RuntimeError(msg) from None


def crawl_source(**kwargs: Any) -> HtmlCrawlSource:
    values: dict[str, Any] = {
        "id": "help-center",
        "type": "html_crawl",
        "url": "https://help.example/billing",
    }
    values.update(kwargs)
    return HtmlCrawlSource.model_validate(values)


def test_html_becomes_markdown_with_its_structure_and_without_its_chrome() -> None:
    markdown, title, links = html_to_markdown(PAGE)
    assert title == "Billing help"
    assert "# Billing help" in markdown
    assert "## Fees" in markdown
    assert "five to seven business days" in markdown
    assert "evil()" not in markdown
    assert links == ["/billing/refunds", "https://elsewhere.example/ad", "/pricing"]


async def test_a_crawl_follows_only_links_under_the_start_url() -> None:
    """Not a bound, a rule. A help-centre page that links to a marketing site would otherwise put
    marketing copy in a policy corpus, and a passage the agent cites has to be something the
    pack's owner published as policy."""
    fetch = StubFetcher(
        {
            "https://help.example/billing": PAGE,
            "https://help.example/billing/refunds": REFUNDS,
            "https://help.example/pricing": "<html><body><h1>Prices</h1></body></html>",
            "https://elsewhere.example/ad": "<html><body><h1>Buy</h1></body></html>",
        }
    )
    documents = await crawl(crawl_source(depth=2), fetch)
    assert [doc.name for doc in documents] == [
        "https://help.example/billing",
        "https://help.example/billing/refunds",
    ]
    assert "elsewhere.example" not in " ".join(fetch.asked)
    assert "https://help.example/pricing" not in fetch.asked


async def test_a_crawl_stops_at_the_declared_depth_and_page_count() -> None:
    fetch = StubFetcher(
        {"https://help.example/billing": PAGE, "https://help.example/billing/refunds": REFUNDS}
    )
    shallow = await crawl(crawl_source(depth=1), fetch)
    assert [doc.name for doc in shallow] == ["https://help.example/billing"]

    capped = await crawl(crawl_source(depth=2, max_pages=1), StubFetcher(fetch.pages))
    assert len(capped) == 1


async def test_one_unreachable_page_does_not_fail_the_crawl() -> None:
    """A help centre with a broken link is normal; the alternative is a scheduled sync that never
    completes."""
    fetch = StubFetcher({"https://help.example/billing": PAGE})
    documents = await crawl(crawl_source(depth=2), fetch)
    assert [doc.name for doc in documents] == ["https://help.example/billing"]


async def test_a_crawled_source_syncs_into_the_same_tables_as_a_directory(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    fetch = StubFetcher(
        {"https://help.example/billing": PAGE, "https://help.example/billing/refunds": REFUNDS}
    )
    built = ingestor(engine, tmp_path, fetch=fetch)
    result = await built.sync_source(crawl_source(depth=2))
    assert result.documents == 2
    assert result.chunks >= 3
    rows = await chunks(engine, "help-center")
    assert any("five to seven business days" in row["text"] for row in rows)
    assert all(row["locator"].startswith("https://help.example/billing") for row in rows)


# -- retention ---------------------------------------------------------------------------------


async def test_retention_keeps_enough_old_versions_for_an_old_trace(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """Not one version, and not unbounded.

    One would delete the version an old trace names the moment anything is edited; unbounded
    would accumulate a copy of the corpus per day for ever. The store's ``keep`` decides, and the
    rows follow it.
    """
    versions = []
    for marker in ("60", "70", "80", "90", "100"):
        versions.append(
            (await synced(engine, tmp_path, {"policy.md": POLICY_V1.replace("60", marker)})).version
        )
    stored = {row["version"] for row in await chunks(engine)}
    assert versions[-1] in stored
    assert versions[-2] in stored
    assert versions[-3] in stored
    assert versions[0] not in stored
    assert len(stored) == 3


async def test_the_sync_survives_a_vector_store_that_is_not_configured(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """A pack must be able to correct its knowledge with the secondary index absent."""
    result = await synced(engine, tmp_path, {"policy.md": POLICY_V1})
    assert result.vector_error == "no vector store configured"
    assert result.chunks == 2
    assert [row["stale"] for row in await chunks(engine)] == [False, False]


def test_the_sources_schema_is_what_the_sync_reads() -> None:
    """One schema, shared by the validator and the sync, so a pack cannot pass one and fail the
    other."""
    parsed = parse_sources(
        {
            "documents": [
                {"id": "policy-docs", "type": "markdown_dir", "path": "./knowledge/docs"},
                {"id": "help-center", "type": "html_crawl", "url": "https://help.example/x"},
            ]
        }
    )
    assert parsed.source_ids() == ["policy-docs", "help-center"]
    assert isinstance(parsed.document("help-center"), HtmlCrawlSource)


def test_a_chunk_row_carries_a_uuid_primary_key() -> None:
    """The Qdrant payload names chunks by this id and reads their text back from Postgres."""
    assert (
        repo.ChunkRow(
            id=uuid.uuid4(),
            source_id="s",
            source_version="r1-x",
            chunk_index=0,
            locator="p.md#H",
            text="t",
        ).score
        == 0.0
    )
