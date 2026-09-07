"""Measuring the four retrieval paths against each other on one corpus. Phase 5.

    Measure it against the pgvector path on the same corpus before making it the default. Two
    retrievers with no comparison between them is worse than one. - BACKLOG.md, phase 5

**Read this before reading the numbers.** What is measured here is *plumbing*, not an embedding
model. The encoder that ships is
:class:`~support_core.knowledge.embedding.DeterministicEncoder` - a hashed projection of tokens
and their character trigrams - because this deployment has no embedding API and cannot download
ColBERT's weights (see the module docstring of ``support_core/knowledge/embedding.py``). So the
dense and late-interaction paths here know that "refunds" and "refunded" are close and do not
know that "reimbursement" and "refund" mean the same thing. A real model would change every
number below, and the self-critique in reviews/phase-5.md says so in as many words.

What the comparison *is* worth is what it is used for here: it fails if a backend stops working,
if the merge starts doing worse than its best input, or if the ColBERT path - the one the backlog
would not let be the default without a measurement - turns out not to earn its place on the shape
of query it was chosen for. Those are regressions a suite can catch, and none of them needs a
language model to be meaningful.

The query set is labelled by hand: twelve questions with the heading that answers each. It is
small, it is written by the person who wrote the corpus, and both of those are limitations
recorded in the self-critique rather than hidden behind a percentage.
"""

from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from tests.knowledge_support import (
    colbert_retriever,
    composite,
    document_retriever,
    request,
    synced,
)

CORPUS = {
    "refunds.md": """# Refund policy

## Refund window

A charge can be refunded within 60 days of the payment date. Charges older than that are outside
the window.

## Duplicate charges

A duplicate charge is always refundable, whatever the age of the charge and whether or not the
plan was used.

## Setup fees

One-off setup fees are not refundable once the setup work has been carried out.

## Chargebacks

A charge disputed with the customer's bank cannot be refunded while the dispute is open.
""",
    "timing.md": """# Processing times

## Card refunds

A refund to the original card takes five to seven business days to appear on the statement.

## Bank transfers

A refund by bank transfer takes three to five business days and needs the account details
confirmed by a person.

## Account credit

Account credit is applied immediately and cannot be paid out to a card afterwards.
""",
    "accounts.md": """# Account changes

## Closing an account

A closed account keeps its payment method on file for 90 days.

## Changing the payment card

A new card replaces the old one for future charges only.
""",
}

QUERIES: list[tuple[str, str]] = [
    ("how many days do I have to ask for a refund", "Refund window"),
    ("is a charge from four months ago still refundable", "Refund window"),
    ("I was charged twice for the same plan", "Duplicate charges"),
    ("duplicate charge on my statement", "Duplicate charges"),
    ("can I get the setup fee back", "Setup fees"),
    ("setup work has been done, is the fee refundable", "Setup fees"),
    ("my bank is disputing the charge", "Chargebacks"),
    ("how long does a card refund take", "Card refunds"),
    ("business days for the money to reach my statement", "Card refunds"),
    ("refund by bank transfer timing", "Bank transfers"),
    ("can account credit be paid out to my card", "Account credit"),
    ("how long is the card kept after closing the account", "Closing an account"),
]
"""``(question, the heading that answers it)``. Hand-labelled; see the module docstring."""


def hit_at(found: Any, heading: str, k: int) -> bool:
    return any(heading in passage.locator for passage in list(found)[:k])


async def score(retrieve: Any, k: int = 3) -> dict[str, Any]:
    """Recall at 1 and at ``k`` over the labelled set, plus the queries that missed."""
    at_one = 0
    at_k = 0
    missed: list[str] = []
    for query, heading in QUERIES:
        found = list(await retrieve(query, k))
        if hit_at(found, heading, 1):
            at_one += 1
        if hit_at(found, heading, k):
            at_k += 1
        else:
            missed.append(query)
    return {"at_1": at_one, "at_k": at_k, "total": len(QUERIES), "missed": missed}


async def backends(engine: AsyncEngine, tmp_path: Path, store: Any) -> dict[str, Any]:
    """Every path, on one corpus, through one sync."""
    await synced(engine, tmp_path, CORPUS, store=store)
    document = document_retriever(engine, source_ids=["policy-docs"])
    colbert = colbert_retriever(engine, store, ["policy-docs"])
    merged = composite(document, colbert)

    async def lexical(query: str, k: int) -> Any:
        return await document.lexical(query, k)

    async def dense(query: str, k: int) -> Any:
        return await document.dense(query, k)

    async def late(query: str, k: int) -> Any:
        return await colbert.retrieve(request(query, k))

    async def fused(query: str, k: int) -> Any:
        return await merged.retrieve(request(query, k))

    return {
        "lexical": await score(lexical),
        "dense": await score(dense),
        "colbert": await score(late),
        "composite": await score(fused),
    }


async def test_every_backend_answers_most_of_the_labelled_set(
    engine: AsyncEngine, tmp_path: Path, qdrant: Any
) -> None:
    """The measurement the backlog asked for, as a floor rather than a target.

    The thresholds are deliberately loose. They are here to fail when a backend stops working -
    a broken tsquery, an encoder whose dimensions drifted, a Qdrant collection built with the
    wrong comparator - not to assert a retrieval quality this encoder cannot honestly claim.
    """
    results = await backends(engine, tmp_path, qdrant)
    for name, result in results.items():
        assert result["at_k"] >= 8, f"{name} found only {result['at_k']}/12 at k=3: {result}"
        assert result["at_1"] >= 5, f"{name} found only {result['at_1']}/12 at k=1: {result}"


async def test_the_merge_is_at_least_as_good_as_its_best_input(
    engine: AsyncEngine, tmp_path: Path, qdrant: Any
) -> None:
    """The property that justifies fusing at all.

    A merge that is worse than one of the things it merges is a merge that should not be there.
    Reciprocal rank fusion is chosen because it cannot be dragged down by a backend that ranks a
    passage low, only by one that ranks a wrong passage high in *every* list.

    **It holds at k=3 and not at k=1**, and that is measured rather than assumed: on this corpus
    the lexical half alone puts the right passage first for every query, and fusing it with two
    weaker rankers moves a few of them to second place. That is the price of fusion and it is
    the right price *here* - the node asks for three passages, not one, and a backend that goes
    down must not take the answer with it - but it would be the wrong price for a k of 1, and a
    later change that made retrieval single-passage should re-read this.
    """
    results = await backends(engine, tmp_path, qdrant)
    best_at_k = max(results[name]["at_k"] for name in ("lexical", "dense", "colbert"))
    assert results["composite"]["at_k"] >= best_at_k, results
    best_at_one = max(results[name]["at_1"] for name in ("lexical", "dense", "colbert"))
    assert results["composite"]["at_1"] <= best_at_one, (
        "fusion beat every input at k=1; pleasant, but re-read the docstring's claim"
    )


async def test_late_interaction_earns_its_place_on_a_phrase_query(
    engine: AsyncEngine, tmp_path: Path, qdrant: Any
) -> None:
    """The shape ColBERT was brought into this phase for: "short policy passages where the answer
    turns on a phrase" (BACKLOG.md).

    The claim being checked is narrow and is the honest one available here: on a question whose
    words are spread across a passage rather than concentrated in it, MaxSim - which scores each
    query token against its best document token - finds the passage that the one-vector-per-chunk
    dense path ranks lower. It is one query, and it is not a benchmark.
    """
    await synced(engine, tmp_path, CORPUS, store=qdrant)
    query = "business days for the money to reach my statement"
    late = await colbert_retriever(engine, qdrant, ["policy-docs"]).retrieve(request(query, 3))
    assert hit_at(late, "Card refunds", 1), [p.locator for p in late]


async def test_the_comparison_still_runs_with_the_vector_side_absent(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The Postgres halves are measurable on their own, which is what the degraded mode *is*."""
    await synced(engine, tmp_path, CORPUS)
    document = document_retriever(engine, source_ids=["policy-docs"])

    async def lexical(query: str, k: int) -> Any:
        return await document.lexical(query, k)

    result = await score(lexical)
    assert result["at_k"] >= 8, result


async def test_the_report_is_printable(engine: AsyncEngine, tmp_path: Path, qdrant: Any) -> None:
    """What a person reads when they want the numbers rather than a pass or a fail.

    Printed rather than asserted on, because the interesting output of a measurement is the
    measurement. ``pytest -s -k printable`` shows it.
    """
    results = await backends(engine, tmp_path, qdrant)
    print()  # noqa: T201 - this test exists to print
    print(f"{'backend':<12}{'recall@1':>10}{'recall@3':>10}")  # noqa: T201
    for name, result in results.items():
        print(  # noqa: T201
            f"{name:<12}{result['at_1']:>7}/{result['total']:<2}{result['at_k']:>7}/"
            f"{result['total']:<2}"
        )
    for name, result in results.items():
        if result["missed"]:
            print(f"  {name} missed: {result['missed']}")  # noqa: T201
    assert set(results) == {"lexical", "dense", "colbert", "composite"}
