"""Chunking by heading. DESIGN.md section 9.3: "chunk by headings (target 300 to 500 tokens)".

The property that matters most here is not the size. It is that **a chunk comes from exactly one
heading**, because a chunk's locator is what a trace prints and what somebody investigating a
wrong answer opens. A chunker that merges a short section into its neighbour produces locators
that are technically true and useless, and the first version of this module did exactly that -
the sample pack's refund window, duplicate-charge rule, exclusions and appeal route all came out
as one chunk locating to the document's title. The first test below is the one that failed
against that version.
"""

import pytest

from support_core.knowledge.chunking import (
    HARD_MAX_TOKENS,
    TARGET_MAX_TOKENS,
    Chunk,
    chunk_markdown,
    split_sections,
)
from support_core.llm.prompt import estimate_tokens

SIBLINGS = """# Refund policy

Acme refunds charges under the rules below.

## Refund window

A charge can be refunded within 60 days.

## Duplicate charges

A duplicate charge is always refundable.

## Exclusions

Setup fees are not refundable.
"""


def locators(markdown: str, name: str = "policy.md") -> list[str]:
    return [chunk.locator(name) for chunk in chunk_markdown(markdown)]


def test_short_sibling_sections_stay_separate_and_keep_their_own_headings() -> None:
    """The regression that removed the merge pass.

    Four sections, none of them near 300 tokens. Merging them would put one locator on all four,
    so a customer told "setup fees are not refundable" would be cited a passage named "Refund
    policy" - which is where the sentence lives, and is not where a person can check it.
    """
    assert locators(SIBLINGS) == [
        "policy.md#Refund policy",
        "policy.md#Refund policy > Refund window",
        "policy.md#Refund policy > Duplicate charges",
        "policy.md#Refund policy > Exclusions",
    ]


def test_every_chunk_comes_from_exactly_one_heading() -> None:
    """Stated as a property over the text rather than over the count.

    Each section's own distinctive sentence appears in exactly one chunk, and that chunk's
    heading path is that section's.
    """
    chunks = chunk_markdown(SIBLINGS)
    for sentence, heading in (
        ("within 60 days", "Refund window"),
        ("always refundable", "Duplicate charges"),
        ("Setup fees", "Exclusions"),
    ):
        owning = [chunk for chunk in chunks if sentence in chunk.text]
        assert len(owning) == 1, f"{sentence!r} is in {len(owning)} chunks"
        assert owning[0].heading_path[-1] == heading


def test_the_heading_path_is_carried_into_the_indexed_text() -> None:
    """A chunk is indexed and shown with its headings above it (``with_heading``).

    Without them a passage reading "within 60 days" is unusable evidence: the model cannot tell
    what the 60 days are about, and nor can the person checking the citation.
    """
    chunk = next(c for c in chunk_markdown(SIBLINGS) if "60 days" in c.text)
    assert chunk.with_heading().startswith("Refund policy / Refund window")
    assert "within 60 days" in chunk.with_heading()


def test_a_section_over_the_target_is_split_on_paragraph_boundaries() -> None:
    paragraph = "Refunds are processed by the billing team on the day of approval. " * 12
    markdown = f"# Policy\n\n## Long\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}\n"
    chunks = chunk_markdown(markdown)
    long = [chunk for chunk in chunks if chunk.heading_path[-1] == "Long"]
    assert len(long) > 1
    assert all(estimate_tokens(chunk.text) <= TARGET_MAX_TOKENS for chunk in long)
    # Every piece keeps the heading, because a fragment with no heading above it cannot be cited.
    assert all(chunk.heading_path == ("Policy", "Long") for chunk in long)


def test_a_single_enormous_paragraph_is_split_on_sentences() -> None:
    """Paragraph splitting alone cannot bound a wall of terms and conditions."""
    sentence = "The customer agrees that no refund is due in this circumstance. "
    markdown = "# Terms\n\n" + sentence * 200 + "\n"
    chunks = chunk_markdown(markdown)
    assert len(chunks) > 1
    assert all(estimate_tokens(chunk.text) <= HARD_MAX_TOKENS for chunk in chunks)


def test_split_pieces_are_numbered_and_unsplit_ones_are_not() -> None:
    """A locator has to be unique, and the common case has to read the way a person writes it."""
    assert Chunk(index=0, text="x", heading_path=("A", "B")).locator("p.md") == "p.md#A > B"
    assert Chunk(index=1, text="x", heading_path=("A", "B"), part=1).locator("p.md") == (
        "p.md#A > B [2]"
    )


def test_two_identically_headed_sections_get_distinct_locators() -> None:
    """Two ``## Notes`` in one document would otherwise be one string naming two chunks."""
    markdown = (
        "# Doc\n\n## Notes\n\nFirst note.\n\n## Other\n\nMiddle.\n\n## Notes\n\nSecond note.\n"
    )
    found = locators(markdown)
    assert len(found) == len(set(found)), found


def test_a_hash_inside_a_fenced_code_block_is_not_a_heading() -> None:
    """Cutting there would halve a code sample and give it a heading it never had."""
    markdown = "# Doc\n\n```sh\n# not a heading\necho hi\n```\n\nAfter the fence.\n"
    chunks = chunk_markdown(markdown)
    assert len(chunks) == 1
    assert "# not a heading" in chunks[0].text
    assert chunks[0].heading_path == ("Doc",)


def test_a_document_that_skips_a_heading_level_keeps_a_readable_path() -> None:
    markdown = "# Top\n\nIntro.\n\n### Deep\n\nBody.\n"
    paths = [chunk.heading_path for chunk in chunk_markdown(markdown)]
    assert paths == [("Top",), ("Top", "Deep")]


def test_text_before_the_first_heading_is_kept() -> None:
    """Front matter is content. Dropping it would lose a document with no headings at all."""
    chunks = chunk_markdown("Some prose with no heading at all.\n")
    assert len(chunks) == 1
    assert chunks[0].heading_path == ()
    assert chunks[0].locator("p.md") == "p.md"


@pytest.mark.parametrize("markdown", ["", "\n\n", "   \n\t\n"])
def test_an_empty_document_produces_no_chunks(markdown: str) -> None:
    assert chunk_markdown(markdown) == []
    assert split_sections(markdown) == []
