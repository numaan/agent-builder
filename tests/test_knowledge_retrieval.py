"""The four retrieval backends and the merge. DESIGN.md section 9.1, and BACKLOG.md's 2026-09-06
Qdrant and ColBERT decisions.

    Qdrant owns the vector side (dense and ColBERT multivector), Postgres full-text owns the
    lexical side. ``CompositeRetriever`` merges them. This buys graceful degradation - with
    Qdrant unavailable the lexical half still answers. - BACKLOG.md

Three claims are being checked and they are not equally important:

1. **Retrieval degrades rather than dies.** This is the one the whole second-store decision was
   justified on, and it is checked against a Qdrant that genuinely is not there rather than
   against a mock that raises. It is also the only test here that never skips.
2. **Versions are collections, and old ones survive a re-sync.** What makes the exit criterion
   exact instead of approximate.
3. Each backend finds what it should, and the merge prefers what more than one of them found.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.knowledge.composite import CompositeRetriever
from support_core.knowledge.document import fuse
from support_core.knowledge.embedding import DeterministicEncoder, maxsim, tokenize
from support_core.knowledge.qdrant import QdrantStore, alias_name, collection_name
from support_core.knowledge.types import Passage, RetrieverUnavailable
from tests.knowledge_support import (
    FakeRetriever,
    colbert_retriever,
    composite,
    document_retriever,
    passage,
    request,
    synced,
    unreachable_store,
)

CORPUS = {
    "refunds.md": """# Refund policy

## Refund window

A charge can be refunded within 60 days of the payment date.

## Duplicate charges

A duplicate charge is always refundable, whatever the age of the charge.

## Exclusions

One-off setup fees are not refundable once the setup work is done.
""",
    "timing.md": """# Processing times

## Card refunds

A refund to the original card takes five to seven business days to reach the statement.

## Bank transfers

A refund by bank transfer takes three to five business days.
""",
}


def locators(passages: Sequence[Passage]) -> list[str]:
    return [p.locator for p in passages]


# -- the Postgres halves -----------------------------------------------------------------------


async def test_the_lexical_half_finds_the_section_whose_words_match(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    await synced(engine, tmp_path, CORPUS)
    found = await document_retriever(engine).lexical("duplicate charge", 3)
    assert found
    assert "Duplicate charges" in found[0].locator
    assert found[0].backend == "lexical"
    assert found[0].source_version.startswith("r1-")


async def test_the_dense_half_finds_the_same_section_without_the_exact_words(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The stand-in encoder is a hashed projection with character trigrams, so it matches
    morphological variants - "refundable" against "refunded" - and not synonyms. That limit is
    the reason ColBERT was brought forward into this phase, and it is written down rather than
    hidden behind a test that only ever asks about words already present."""
    await synced(engine, tmp_path, CORPUS)
    found = await document_retriever(engine).dense("refunding duplicated charges", 3)
    assert found
    assert any("Duplicate charges" in row.locator for row in found)
    assert found[0].backend == "dense"


async def test_a_lexical_query_of_pure_punctuation_returns_nothing_rather_than_anything(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """An empty tsquery matches everything at rank zero, which is an arbitrary k rows presented
    as evidence."""
    await synced(engine, tmp_path, CORPUS)
    assert await document_retriever(engine).lexical("   ...  ", 3) == []


async def test_a_retriever_scoped_to_a_source_cannot_see_another(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    await synced(engine, tmp_path, CORPUS)
    scoped = document_retriever(engine, source_ids=["some-other-pack"])
    assert await scoped.lexical("duplicate charge", 3) == []


async def test_stale_chunks_are_not_retrieved(engine: AsyncEngine, tmp_path: Path) -> None:
    """The live version is what a customer's next question meets; the old rows stay for the
    trace."""
    await synced(engine, tmp_path, CORPUS)
    edited = dict(CORPUS)
    edited["refunds.md"] = CORPUS["refunds.md"].replace("60 days", "90 days")
    second = await synced(engine, tmp_path, edited)

    found = await document_retriever(engine).lexical("refunded within days of the payment", 5)
    assert found
    assert {row.source_version for row in found} == {second.version}
    assert all("60 days" not in row.text for row in found)


# -- the vector side ---------------------------------------------------------------------------


async def test_colbert_scores_late_interaction_over_qdrant(
    engine: AsyncEngine, tmp_path: Path, qdrant: QdrantStore
) -> None:
    """MaxSim over a multivector collection, which is the whole reason Qdrant is here."""
    await synced(engine, tmp_path, CORPUS, store=qdrant)
    found = await colbert_retriever(engine, qdrant, ["policy-docs"]).retrieve(
        request("how many business days does a card refund take")
    )
    assert found
    assert "Card refunds" in found[0].locator
    assert found[0].backend == "colbert"
    # The text and the version come from Postgres, never from the payload: a derived index must
    # not be the only copy of what a citation names.
    assert found[0].text.startswith("Processing times")
    assert found[0].source_version.startswith("r1-")


async def test_a_sync_builds_a_new_collection_and_flips_the_alias(
    engine: AsyncEngine, tmp_path: Path, qdrant: QdrantStore
) -> None:
    """Versions are collections, not rows. This is what makes "an older trace still names the old
    version" exact: the collection that produced the old answer is still there, unchanged."""
    first = await synced(engine, tmp_path, CORPUS, store=qdrant)
    edited = dict(CORPUS)
    edited["refunds.md"] = CORPUS["refunds.md"].replace("60 days", "90 days")
    second = await synced(engine, tmp_path, edited, store=qdrant)

    v1 = collection_name("policy-docs", 1, prefix=qdrant.prefix)
    v2 = collection_name("policy-docs", 2, prefix=qdrant.prefix)
    assert first.collection == v1
    assert second.collection == v2
    assert await qdrant.collection_exists(v1)
    assert await qdrant.collection_exists(v2)

    # The alias resolves to the new one, and queries through it see only the new corpus.
    alias = alias_name("policy-docs", prefix=qdrant.prefix)
    encoder = DeterministicEncoder()
    hits = await qdrant.search_colbert(
        alias,
        dense=await encoder.embed_query("refund window"),
        colbert=await encoder.encode_query("refund window"),
        limit=10,
    )
    assert hits
    assert {hit.payload["source_version"] for hit in hits} == {second.version}


async def test_retention_drops_the_oldest_collection_and_keeps_the_rest(
    engine: AsyncEngine, tmp_path: Path, qdrant: QdrantStore
) -> None:
    for marker in ("60", "70", "80", "90"):
        edited = dict(CORPUS)
        edited["refunds.md"] = CORPUS["refunds.md"].replace("60 days", f"{marker} days")
        await synced(engine, tmp_path, edited, store=qdrant)
    assert not await qdrant.collection_exists(
        collection_name("policy-docs", 1, prefix=qdrant.prefix)
    )
    for revision in (2, 3, 4):
        assert await qdrant.collection_exists(
            collection_name("policy-docs", revision, prefix=qdrant.prefix)
        )


# -- degradation -------------------------------------------------------------------------------


async def test_the_lexical_half_still_answers_when_qdrant_is_not_there(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The claim the second store was accepted on, against a port nothing listens on.

    Never skipped. A suite that could only check this when Qdrant was *running* would be checking
    the wrong thing.
    """
    down = unreachable_store()
    result = await synced(engine, tmp_path, CORPUS, store=down)
    assert result.vector_error, "the sync should report the vector index it could not build"
    assert result.chunks > 0, "and should still commit the Postgres halves"

    merged = composite(
        document_retriever(engine, source_ids=["policy-docs"]),
        colbert_retriever(engine, down, ["policy-docs"]),
    )
    retrieval = await merged.gather(request("duplicate charge"))
    assert retrieval.degraded == ("colbert",)
    assert retrieval.passages, "the answer is degraded, not empty"
    assert any("Duplicate charges" in p.locator for p in retrieval.passages)
    assert all(p.backend != "colbert" for p in retrieval.passages)


async def test_with_every_backend_down_the_result_is_empty_and_says_so() -> None:
    """Not papered over. An empty knowledge block plus a factual claim is what the citation
    guardrail refuses, so the conversation reaches a person rather than a guess."""
    merged = composite(
        FakeRetriever(name="lexical", fails=True), FakeRetriever(name="colbert", fails=True)
    )
    retrieval = await merged.gather(request("anything"))
    assert retrieval.passages == ()
    assert set(retrieval.degraded) == {"lexical", "colbert"}
    assert len(retrieval.errors) == 2


async def test_a_backend_that_raises_something_other_than_unavailable_is_a_bug_and_propagates() -> (
    None
):
    """Swallowing every exception would turn each future programming error in a retriever into a
    quietly worse answer."""

    class Broken:
        name = "broken"

        async def retrieve(self, req: object) -> list[Passage]:
            raise KeyError("a typo, not an outage")

    with pytest.raises(KeyError):
        await composite(Broken()).gather(request("x"))


async def test_a_missing_collection_degrades_that_source_and_not_the_backend(
    engine: AsyncEngine, tmp_path: Path, qdrant: QdrantStore
) -> None:
    """A pack that declares a source nobody has synced yet must not lose the vector side for the
    sources that *are* there."""
    await synced(engine, tmp_path, CORPUS, store=qdrant)
    both = colbert_retriever(engine, qdrant, ["policy-docs", "never-synced"])
    found = await both.retrieve(request("card refund business days"))
    assert found

    neither = colbert_retriever(engine, qdrant, ["never-synced"])
    with pytest.raises(RetrieverUnavailable):
        await neither.retrieve(request("card refund business days"))


# -- the merge ---------------------------------------------------------------------------------


def test_the_merge_prefers_a_passage_more_than_one_backend_found() -> None:
    """Rank fusion, not score fusion: ``ts_rank_cd``, cosine and MaxSim are three incomparable
    units, and only the orderings mean anything."""
    agreed = passage("agreed", locator="a.md#A")
    lexical_only = passage("lexical", locator="b.md#B")
    dense_only = passage("dense", locator="c.md#C")
    merged = fuse([[lexical_only, agreed], [dense_only, agreed]])
    assert merged[0].locator == "a.md#A"
    assert merged[0].backend == ""  # no backend recorded on these fixtures
    assert locators(merged[1:]) == ["b.md#B", "c.md#C"]


def test_the_merge_records_every_backend_that_found_a_passage() -> None:
    """ "Both halves agreed" is the most useful thing a trace can say about a passage."""
    one = passage("x", locator="a.md#A")
    one.backend = "lexical"
    two = passage("x", locator="a.md#A")
    two.backend = "colbert"
    merged = fuse([[one], [two]])
    assert len(merged) == 1
    assert merged[0].backend == "lexical+colbert"


def test_the_merge_deduplicates_on_source_version_and_locator() -> None:
    """The same text at the same locator of two *versions* is two passages, because a trace has
    to be able to tell them apart."""
    old = passage("x", version="r1-aaaaaaaaaaaa", locator="a.md#A")
    new = passage("x", version="r2-bbbbbbbbbbbb", locator="a.md#A")
    assert len(fuse([[old], [new]])) == 2
    assert len(fuse([[old], [old.model_copy()]])) == 1


async def test_ids_are_assigned_last_in_final_rank_order() -> None:
    """``k1``, ``k2``, ... The composite is the only place that knows the whole set the model
    sees, which is what an inline id has to be unique within (DESIGN.md 9.2)."""
    merged = composite(
        FakeRetriever([passage("first", locator="a.md#A")], name="one"),
        FakeRetriever([passage("second", locator="b.md#B")], name="two"),
    )
    retrieval = await merged.gather(request("x", k=2))
    assert [p.id for p in retrieval.passages] == ["k1", "k2"]


async def test_a_composite_with_no_backends_answers_empty_rather_than_failing() -> None:
    assert (await CompositeRetriever([]).gather(request("x"))).passages == ()


async def test_the_composite_never_returns_more_than_k(engine: AsyncEngine, tmp_path: Path) -> None:
    await synced(engine, tmp_path, CORPUS)
    merged = composite(document_retriever(engine, source_ids=["policy-docs"]))
    assert len(await merged.retrieve(request("refund", k=2))) <= 2


# -- the encoder -------------------------------------------------------------------------------


def test_the_encoder_is_stable_across_processes() -> None:
    """Keyed on ``blake2b``, never on Python's salted ``hash``. A corpus indexed by one process
    has to be findable by the next, and the fake provider replays by request fingerprint."""
    import subprocess
    import sys

    script = (
        "import asyncio;"
        "from support_core.knowledge.embedding import DeterministicEncoder as E;"
        "print(asyncio.run(E().embed_query('refund policy'))[:4])"
    )
    first = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout
    second = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout
    assert first == second


async def test_maxsim_rewards_the_document_whose_tokens_cover_the_query() -> None:
    encoder = DeterministicEncoder()
    query = await encoder.encode_query("business days")
    good = (await encoder.encode_documents(["a refund takes five business days"]))[0]
    bad = (await encoder.encode_documents(["setup fees are not refundable"]))[0]
    assert maxsim(query, good) > maxsim(query, bad)


def test_tokenisation_is_the_rule_the_lexical_side_would_recognise() -> None:
    assert tokenize("Refunds take 5-7 business days!") == [
        "refunds",
        "take",
        "5",
        "7",
        "business",
        "days",
    ]
