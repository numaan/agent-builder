"""Splitting a document into retrievable chunks. Implements DESIGN.md section 9.3.

Section 9.3 is one clause: "chunk by headings (target 300 to 500 tokens)". Two things it does not
say, and both have to be decided somewhere:

* **What happens to a section that is 2000 tokens long.** It is split on paragraph boundaries,
  keeping the heading path on every piece. The alternative - one oversized chunk - is worse in
  two places at once: layer 7's budget would evict its own siblings to fit it
  (:func:`support_core.llm.prompt._knowledge` drops passages that do not fit), and a locator that
  names a whole chapter is not the "source version" precision section 9.2 asks for.
* **What happens to a section that is 20 tokens long.** Nothing. It is its own chunk, with its
  own heading path, and it is not padded out by absorbing the section that follows it.

  This was written the other way first, merging a short section forward while the heading path
  still "described" both, and it was wrong in a way worth recording because it looked right. The
  test is what a locator *means*: a passage cited as ``refund-policy.md#Acme refund policy`` is a
  claim that the customer's answer came from that heading. Merging forward made an ancestor path
  swallow one sibling, which then made the *next* sibling a descendant of the merged block too,
  and the sample pack's four policy sections - the refund window, duplicate charges, the
  exclusions and the appeal - came out as one chunk locating to the document's title. Every
  locator was technically true and none of them was useful, which is the failure mode section
  9.2's "traceable to a source version" is trying to prevent one level up.

  So 300 to 500 is a target for a *splitter*, not a floor to reach by gluing. A short section is
  a precise, citable unit, and it is legible on its own because
  :meth:`Chunk.with_heading` puts its whole heading path above the text.

The unit throughout is :func:`support_core.llm.prompt.estimate_tokens` - four characters per
token - and it is the same approximation phase 3 uses for its budgets, with the same honest
caveat. Using a different estimate here would mean a chunk sized against one ruler and budgeted
against another.
"""

import re
from dataclasses import dataclass, field

from support_core.llm.prompt import estimate_tokens

TARGET_MIN_TOKENS = 300
TARGET_MAX_TOKENS = 500
"""DESIGN.md section 9.3's target, verbatim.

``MAX`` is what a section must not exceed, and is enforced by splitting. ``MIN`` is *not*
enforced: nothing pads a short section, for the reason in the module docstring. It is kept as a
named constant because it is half of the design's own sentence and because the ingestion report
and the tests both want to say how many chunks came out under it - a corpus of 40-token sections
is a document written in a shape retrieval will handle badly, and that is worth being able to
see rather than worth silently repairing."""

HARD_MAX_TOKENS = 700
"""The size above which a *paragraph* is split mid-paragraph on sentence boundaries.

A single 3000-token paragraph exists (a table rendered as prose, a wall of terms and conditions)
and paragraph splitting alone cannot bound it. Above ``MAX`` and below this, a chunk is left
slightly over rather than cut, because a cut costs a sentence its context and 500 is a target
rather than a limit of anything."""

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable piece of a document."""

    index: int
    text: str
    heading_path: tuple[str, ...] = ()
    """``("Refunds", "Timing")`` - the headings above this text, outermost first."""

    part: int = 0
    """Which piece of an over-long section this is. Zero when the section fit in one."""

    def locator(self, document: str) -> str:
        """``path#Heading > Subheading`` (DESIGN.md section 9.1: "doc path + heading").

        The part number is appended only when there is more than one piece, so the common case
        reads as a person would write it and the uncommon case is still unambiguous.
        """
        anchor = " > ".join(self.heading_path)
        suffix = f" [{self.part + 1}]" if self.part else ""
        return f"{document}#{anchor}{suffix}" if anchor else f"{document}{suffix}"

    def with_heading(self) -> str:
        """The text as it is indexed and shown to the model: heading path, then body.

        The heading is repeated into every piece of a split section on purpose. A chunk that
        reads "within five business days" with no heading above it is unusable evidence, and a
        retriever that matched the heading but returned only the body would be citing something
        the model cannot check.
        """
        if not self.heading_path:
            return self.text
        return " / ".join(self.heading_path) + "\n\n" + self.text


@dataclass(slots=True)
class _Section:
    heading_path: tuple[str, ...]
    lines: list[str] = field(default_factory=list)

    def body(self) -> str:
        return "\n".join(self.lines).strip()


def split_sections(markdown: str) -> list[_Section]:
    """Cut a document at its headings, tracking the path of enclosing headings.

    Fenced code blocks are passed through untouched: a ``# comment`` inside one is not a heading,
    and treating it as one would cut a code sample in half and give it a heading it never had.
    """
    sections: list[_Section] = [_Section(heading_path=())]
    stack: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines():
        opener = _FENCE.match(line)
        if fence is not None:
            sections[-1].lines.append(line)
            if opener is not None and opener.group(1) == fence:
                fence = None
            continue
        if opener is not None:
            fence = opener.group(1)
            sections[-1].lines.append(line)
            continue
        heading = _HEADING.match(line)
        if heading is None:
            sections[-1].lines.append(line)
            continue
        level = len(heading.group(1))
        title = heading.group(2)
        del stack[level - 1 :]
        while len(stack) < level - 1:
            # A document that jumps from `#` to `###` has no level-2 heading. Padding keeps the
            # path's length equal to the level, so the tree is still readable, and marks the gap
            # rather than silently promoting the heading.
            stack.append("")
        stack.append(title)
        sections.append(_Section(heading_path=tuple(part for part in stack if part)))
    return [section for section in sections if section.body()]


def _paragraphs(text: str) -> list[str]:
    return [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE.split(text) if part.strip()]
    return parts or [text]


def _pack(pieces: list[str], limit: int) -> list[str]:
    """Greedily fill chunks up to ``limit`` tokens, never splitting a piece."""
    packed: list[str] = []
    current: list[str] = []
    used = 0
    for piece in pieces:
        cost = estimate_tokens(piece)
        if current and used + cost > limit:
            packed.append("\n\n".join(current))
            current, used = [], 0
        current.append(piece)
        used += cost
    if current:
        packed.append("\n\n".join(current))
    return packed


def _split_section(body: str) -> list[str]:
    """One section's body, as one or more pieces each at most ``TARGET_MAX_TOKENS``-ish."""
    if estimate_tokens(body) <= TARGET_MAX_TOKENS:
        return [body]
    pieces: list[str] = []
    for paragraph in _paragraphs(body):
        if estimate_tokens(paragraph) <= HARD_MAX_TOKENS:
            pieces.append(paragraph)
        else:
            pieces.extend(_pack(_sentences(paragraph), TARGET_MAX_TOKENS))
    return _pack(pieces, TARGET_MAX_TOKENS)


def chunk_markdown(markdown: str) -> list[Chunk]:
    """Split one markdown document into chunks (DESIGN.md section 9.3).

    Two passes: cut at headings, then split anything too big on paragraph and, failing that,
    sentence boundaries. There is no third pass gluing short sections together - see the module
    docstring for why there was one and why it had to go. A chunk therefore comes from exactly
    one heading, which is what makes its locator worth printing in a trace.
    """
    pieces: list[tuple[tuple[str, ...], str]] = []
    for section in split_sections(markdown):
        for part in _split_section(section.body()):
            pieces.append((section.heading_path, part))

    totals: dict[tuple[str, ...], int] = {}
    for heading_path, _ in pieces:
        totals[heading_path] = totals.get(heading_path, 0) + 1

    chunks: list[Chunk] = []
    counts: dict[tuple[str, ...], int] = {}
    for index, (heading_path, body) in enumerate(pieces):
        ordinal = counts.get(heading_path, 0)
        counts[heading_path] = ordinal + 1
        chunks.append(
            Chunk(
                index=index,
                text=body,
                heading_path=heading_path,
                # A part number only where a section really was cut, so the common locator reads
                # the way a person would write it and the split one is still unambiguous. Two
                # *identically headed* sections in one document count as split for this purpose,
                # which is right: without the number their locators would be the same string.
                part=ordinal if totals[heading_path] > 1 else 0,
            )
        )
    return chunks
