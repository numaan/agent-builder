"""The outbound citation guardrail. Implements DESIGN.md section 9.2 and the first bullet of
section 14's "Outbound" list.

    The outbound guardrail rejects factual claims about policy, pricing, or timing with no
    citation, and the engine re-prompts once before routing to handoff. - DESIGN.md 9.2

This module is the "rejects" half. The re-prompt is
:meth:`support_core.llm.service.LlmService.decide`'s existing retry loop - the one phase 3 built
and phase 3's review hardened - and the handoff is phase 6's, reached through
``NodeError(reason="uncited_claim")``. Nothing new was invented for either, on purpose: a second
way to re-prompt and a second way to hand off would be two more things that can disagree with the
first.

**How the detector works, and what that means.** It is rule-based, as BACKLOG.md's checklist asks
("factual-claim classifier (rule-based first)"). It splits the outbound message into sentences and
asks of each one whether it asserts something about policy, pricing or timing, using three
families of cue patterns. It is a *detector of claims*, not a verifier of them: it cannot tell
whether the passage that was cited actually supports the sentence, and a rule-based detector never
will. What it can do, and does, is refuse the two shapes that are checkable:

* a message that contains a claim and cites nothing at all;
* a message that cites an id the node was never given - a fabricated citation, which is worse
  than no citation, because it looks like grounding.

The self-critique in reviews/phase-5.md lists what it misses, in detail and without softening it.
Two things are worth stating here as well, because a reader of this file should not have to go
looking:

1. **It is a keyword detector and it will have false positives and false negatives.** A false
   positive costs a turn and, on the second attempt, a handoff - annoying, and safe. A false
   negative is a claim that reaches a customer uncited, which is the failure this exists to
   prevent and which it will sometimes fail to prevent.
2. **It checks presence, not attribution.** One valid citation clears a message with four claims
   in it. Per-claim attribution needs a model, and a model marking its own homework is not a
   guardrail; DESIGN.md 16.1's node evals and 8's LLM judge are where that belongs.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class CitationPolicy(BaseModel):
    """Pack-supplied configuration for this guardrail (DESIGN.md section 14).

    Section 14 says guardrails are "core code with pack-supplied configuration" and that "packs
    cannot disable structural guardrails". This one is not structural - it is a heuristic over
    natural language - so a pack may turn it off, and the default is *on*: a check a pack has to
    remember to enable protects the packs that did not need it.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    extra_claim_patterns: list[str] = Field(default_factory=list)
    """Regular expressions a pack adds to the detector, for its own domain's claim vocabulary.

    Additive only. There is deliberately no way to *remove* a core pattern: a pack that could
    delete the timing family could make "your refund arrives tomorrow" not a claim, which is
    exactly the sentence this exists for. A pack that finds the detector too eager turns the whole
    guardrail off, in one visible line, rather than quietly hollowing it out."""

    allow_uncited_questions: bool = True
    """Whether a sentence ending in a question mark can be a claim.

    On by default: "Is it the £29 charge from Tuesday you mean?" repeats a number the workflow
    already has and asserts nothing. Off for a pack that has found the opposite."""


CLAIM_FAMILIES: dict[str, tuple[str, ...]] = {
    "policy": (
        r"\b(?:our|the|company|acme)\s+polic(?:y|ies)\b",
        r"\b(?:is|are|was|were)\s+(?:not\s+)?(?:eligible|refundable|covered|permitted|allowed)\b",
        r"\b(?:you|customers?)\s+(?:can(?:not)?|can't|cannot|must|may(?: not)?|are (?:not )?"
        r"(?:able|entitled|eligible|required))\b",
        r"\bwe\s+(?:do not|don't|cannot|can't|are unable to|never|only|always|require)\b",
        r"\b(?:terms and conditions|terms of service|under (?:the|our) (?:terms|policy))\b",
        r"\b(?:qualifies|qualify|qualifies for|not qualify)\b",
    ),
    "pricing": (
        r"[$£€]\s?\d",
        r"\b\d+(?:\.\d+)?\s*(?:usd|gbp|eur|dollars?|pounds?|euros?)\b",
        r"\b\d+(?:\.\d+)?\s*%",
        r"\b(?:costs?|charged?|charges|fee|fees|price[sd]?|pricing|surcharge|refund amount)\b",
        r"\bfree of charge\b",
        r"\bno (?:extra )?(?:cost|charge|fee)\b",
    ),
    "timing": (
        r"\b\d+\s*(?:to\s*\d+\s*)?(?:business\s+|working\s+)?"
        r"(?:second|minute|hour|day|week|month|year)s?\b",
        r"\b(?:five|seven|ten|thirty|sixty|ninety)\s+(?:business\s+|working\s+)?"
        r"(?:day|hour|week|month)s?\b",
        r"\bwithin\s+(?:a|an|one|two|\d+)\b",
        r"\b(?:immediately|instantly|straight away|same day|next business day|overnight)\b",
        r"\b(?:takes?|taking)\s+(?:up to\s+)?(?:about\s+)?\d",
        r"\b(?:deadline|cut[- ]?off|expires?|expiry|valid for)\b",
    ),
}
"""The three families DESIGN.md section 9.2 names, and nothing else.

Written as three named groups rather than one pattern so that a finding can say *which* kind of
claim it found, which is what makes the correction to the model specific enough to act on and the
trace readable to a person.

**The order is load-bearing**, because a sentence is named by the first family that matches and
the families overlap on the word "charge". Policy is first because its patterns are the most
specific - they need a modal or an eligibility word beside the noun - so "that charge is not
eligible for a refund" is a policy claim rather than a pricing one, which is what a person
reading the trace would call it. Pricing is last because its vocabulary is the broadest. Getting
a family wrong never changes whether a message is refused; it changes what the correction says
and what the trace reads like, which is why this is a comment and not a mechanism.
"""

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    family: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
    for family, patterns in CLAIM_FAMILIES.items()
}

_HEDGED = re.compile(
    r"\b(?:i (?:do not|don't) know|i cannot (?:say|tell)|i am not sure|i'm not sure|"
    r"let me (?:check|find out)|i (?:will|'ll) (?:check|find out|look)|"
    r"a (?:colleague|specialist|person|human) will)\b",
    re.IGNORECASE,
)
"""Sentences that decline to assert.

"I do not know how long that takes" contains a timing cue and asserts nothing; refusing it would
train the model away from the one answer DESIGN.md's core prompt rule 4 explicitly asks for
("say you do not know rather than guessing"). A guardrail that punishes honesty is worse than
none."""

_ACTION = re.compile(
    r"^\s*(?:i(?: have|'ve| will|'ll| am|'m)?\b|we(?: have| 've| will|'ll)?\b)"
    r".*\b(?:issued|refunded|updated|changed|sent|cancelled|canceled|passed|created|"
    r"booked|logged|raised|recorded)\b",
    re.IGNORECASE,
)
"""First-person statements about what the agent just did.

They are claims, and they are somebody else's guardrail: DESIGN.md section 14's "forbidden
promises ... require a corresponding action record" checks them against the tool ledger, which is
a stronger check than a citation, and section 3's principle 3 puts every side effect in the
trace. Making them require a *knowledge* citation would be wrong twice over - there is no policy
document that says a particular refund was issued, and it would teach a pack to cite a policy for
an action. The forbidden-promise check is phase 7's; this is recorded here so that phase does not
have to rediscover the boundary."""

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True, slots=True)
class Claim:
    """One sentence the detector reads as a factual claim."""

    sentence: str
    family: str
    """``pricing``, ``timing``, ``policy``, or ``pack`` for a pack-supplied pattern."""


@dataclass(frozen=True, slots=True)
class CitationVerdict:
    """What the guardrail found. Empty ``problems`` means the message may be sent."""

    claims: tuple[Claim, ...] = ()
    unknown_citations: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def families(self) -> list[str]:
        return sorted({claim.family for claim in self.claims})

    def correction(self) -> str:
        """What the model is told on the one retry (DESIGN.md section 9.2).

        Core-written and drawn from a closed vocabulary. Phase 3's review finding V5 established
        that a correction lands in layer 5, which is trusted and unfenced, so nothing
        model-controlled may be interpolated into it - not the offending sentence, not a citation
        id the model invented. What goes in is the *kind* of claim and the ids that were offered,
        both of which core knows independently of what the model said.
        """
        parts: list[str] = []
        if self.claims:
            kinds = ", ".join(self.families())
            parts.append(
                f"Your previous answer stated something about {kinds} without citing a knowledge "
                f"passage. Every factual claim about policy, pricing or timing must name the id "
                f"of a passage in the knowledge block that supports it."
            )
        if self.unknown_citations:
            parts.append(
                "It also cited an id that is not in the knowledge block. Cite only the ids you "
                "were given."
            )
        parts.append(
            "Answer again: either cite a passage that supports the claim, or say you do not know "
            "and leave the claim out."
        )
        return " ".join(parts)

    def detail(self) -> str:
        """The engine's own words, for the trace and the handoff packet. Not for a prompt."""
        parts: list[str] = []
        for claim in self.claims:
            parts.append(f"[{claim.family}] {claim.sentence}")
        if self.unknown_citations:
            parts.append(f"citations naming no offered passage: {list(self.unknown_citations)}")
        return "; ".join(parts)


def sentences(message: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(message) if part.strip()]


def find_claims(message: str, policy: CitationPolicy | None = None) -> list[Claim]:
    """Every sentence of ``message`` that reads as a factual claim (DESIGN.md section 9.2)."""
    settings = policy or CitationPolicy()
    extra = tuple(re.compile(pattern, re.IGNORECASE) for pattern in settings.extra_claim_patterns)
    found: list[Claim] = []
    for sentence in sentences(message):
        if settings.allow_uncited_questions and sentence.endswith("?"):
            continue
        if _HEDGED.search(sentence) or _ACTION.match(sentence):
            continue
        family = _family(sentence, extra)
        if family is not None:
            found.append(Claim(sentence=sentence, family=family))
    return found


def _family(sentence: str, extra: Sequence[re.Pattern[str]]) -> str | None:
    for name, patterns in _COMPILED.items():
        if any(pattern.search(sentence) for pattern in patterns):
            return name
    if any(pattern.search(sentence) for pattern in extra):
        return "pack"
    return None


def check_citations(
    message: str | None,
    citations: Iterable[str],
    offered: Iterable[str],
    policy: CitationPolicy | None = None,
) -> CitationVerdict:
    """The guardrail, in one call (DESIGN.md sections 9.2, 14).

    ``offered`` is the ids of the passages this node was actually given, which is the only thing
    that makes a citation checkable at all. A node with an empty knowledge block that makes a
    claim therefore fails - and that is right rather than harsh: it is a claim with nothing behind
    it, and the customer reaches a person instead of an invented answer. It is also what the whole
    chain exists to produce, and what "a wrong answer is traceable to a source version" means when
    the source does not exist.
    """
    settings = policy or CitationPolicy()
    if not settings.enabled or not message or not message.strip():
        return CitationVerdict()
    available = {value for value in offered if value}
    named = [value for value in citations if value]
    unknown = tuple(sorted({value for value in named if value not in available}))
    claims = tuple(find_claims(message, settings))
    supported = [value for value in named if value in available]

    problems: list[str] = []
    if claims and not supported:
        kinds = ", ".join(sorted({claim.family for claim in claims}))
        problems.append(
            f"the message makes {len(claims)} factual claim(s) about {kinds} and cites no "
            f"passage that was offered"
        )
    if unknown:
        problems.append(f"the message cites {list(unknown)}, which no offered passage matches")
    return CitationVerdict(claims=claims, unknown_citations=unknown, problems=tuple(problems))
