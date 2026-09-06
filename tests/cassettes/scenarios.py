"""The conversations the cassettes record, defined once.

Imported by both :mod:`tests.cassettes.build_cassettes` (which records them) and
``tests/test_golden_conversation.py`` (which replays them), so the two cannot drift: a scenario
is a pack, a list of customer messages, and - for an offline recording - the rules the scripted
stand-in answers with. Recording the same scenario against the live API is the same code with a
different provider, which is what PLAN.md's "live calls only in a marked ``live`` test group"
needs to be true without a second definition of the conversation.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.engine.hooks import EngineHooks
from support_core.llm.fake import Rule
from support_core.llm.provider import LLMProvider
from support_core.llm.wiring import StructuredSlotExtractor, service_for_pack
from support_core.memory import LlmSummarizer

REPO_ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = Path(__file__).resolve().parent
ACME = REPO_ROOT / "packs" / "acme_billing"

CLASSIFY = "Read the customer's most recent message and decide which path"
CHAT = "Reply to the customer's small talk"
EXTRACT = "The workflow asked the customer for specific values"
SUMMARY = "Write a short factual summary"


def answer(
    label: str,
    *,
    message: str | None = None,
    confidence: float = 0.9,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "message_to_customer": message,
        "decision": label,
        "state_updates": dict(updates or {}),
        "citations": [],
        "confidence": confidence,
        "needs_handoff": False,
    }


@dataclass(frozen=True, slots=True)
class Scenario:
    """One scripted conversation (DESIGN.md section 16.1's "golden conversations", in miniature)."""

    name: str
    pack_path: Path
    turns: Sequence[str]
    rules: Sequence[Rule] = field(default_factory=tuple)
    expected_path: Sequence[str] = ()
    """The node ids a *scripted* recording produces, in order. A live recording may legitimately
    take another path - that is what asking a real model means - so the golden test only asserts
    this when the cassette says it was recorded offline."""

    expected_authors: Sequence[str] = ()
    """The transcript, by author, in order: who said what and how many times."""

    expected_status: str = "done"

    @property
    def cassette_path(self) -> Path:
        return CASSETTE_DIR / f"{self.name}.json"


ACME_SMALL_TALK = Scenario(
    name="acme_small_talk",
    pack_path=ACME,
    turns=["Hello there!", "No thanks, that is everything."],
    rules=(
        # The customer opens with small talk, and after the reply says they need nothing else.
        Rule(when=CLASSIFY, respond=answer("small_talk", updates={"intent": "small_talk"}), uses=1),
        Rule(when=CLASSIFY, respond=answer("finished", updates={"intent": "finished"})),
        Rule(
            when=CHAT,
            respond=answer("done", message="Hello. How can I help with your Acme billing today?"),
        ),
        Rule(
            when=EXTRACT,
            respond={"slots": {"anything_else": "no"}, "unfilled": [], "confidence": 0.9},
        ),
        Rule(
            when=SUMMARY,
            respond={
                "summary": (
                    "The customer greeted the assistant and then said they needed nothing "
                    "further. No account action was requested or taken."
                )
            },
        ),
    ),
    expected_path=(
        "classify",
        "chat",
        "anything_else",
        "anything_else",
        "classify",
        "done_finished",
    ),
    expected_authors=("customer", "agent", "agent", "customer"),
)

# The three remaining edges of the exit criterion's classify node (review finding V7: the node
# declares four and one golden conversation drove one of them, so `escalate`, `puzzled` and
# `done_finished`-from-the-first-turn were never executed by a golden conversation). Each is
# keyed on the *customer's own words* rather than on the node's instructions, so the scripted
# rules have to tell the four messages apart the way a classifier would.
ACCOUNT_QUESTION = "Why was I charged 40 dollars on the 3rd?"
NONSENSE = "sdfkj lkj ??"
DONE_AT_ONCE = "Nothing needed, just closing the loop. Bye."

ACME_ACCOUNT_QUESTION = Scenario(
    name="acme_account_question",
    pack_path=ACME,
    turns=[ACCOUNT_QUESTION],
    rules=(
        Rule(
            when=ACCOUNT_QUESTION,
            respond=answer("account_question", updates={"intent": "account_question"}),
            purpose="node",
        ),
    ),
    expected_path=("classify", "escalate", "done_escalated"),
    expected_authors=("customer", "agent"),
)

ACME_UNCLEAR = Scenario(
    name="acme_unclear",
    pack_path=ACME,
    turns=[NONSENSE, "Actually nothing, thanks."],
    rules=(
        Rule(
            when=NONSENSE,
            respond=answer("unclear", updates={"intent": "unclear"}, confidence=0.8),
            purpose="node",
            uses=1,
        ),
        Rule(
            when=EXTRACT,
            respond={"slots": {"anything_else": "nothing"}, "unfilled": [], "confidence": 0.9},
        ),
        Rule(when=CLASSIFY, respond=answer("finished", updates={"intent": "finished"})),
        Rule(when=SUMMARY, respond={"summary": "The customer's first message was unreadable."}),
    ),
    expected_path=(
        "classify",
        "puzzled",
        "anything_else",
        "anything_else",
        "classify",
        "done_finished",
    ),
    expected_authors=("customer", "agent", "agent", "customer"),
)

ACME_FINISHED_AT_ONCE = Scenario(
    name="acme_finished_at_once",
    pack_path=ACME,
    turns=[DONE_AT_ONCE],
    rules=(
        Rule(
            when=DONE_AT_ONCE,
            respond=answer("finished", updates={"intent": "finished"}),
            purpose="node",
        ),
    ),
    expected_path=("classify", "done_finished"),
    expected_authors=("customer",),
)

SCENARIOS: tuple[Scenario, ...] = (
    ACME_SMALL_TALK,
    ACME_ACCOUNT_QUESTION,
    ACME_UNCLEAR,
    ACME_FINISHED_AT_ONCE,
)


async def play(
    scenario: Scenario, engine: AsyncEngine, provider: LLMProvider
) -> tuple[uuid.UUID, Executor]:
    """Run a scenario's turns against the real engine and the given provider."""
    pack = load_pack(scenario.pack_path)
    service = service_for_pack(pack, provider)
    hooks = EngineHooks(
        extract_slots=StructuredSlotExtractor(service),
        summarize=LlmSummarizer(service, max_chars=pack.manifest.memory.max_summary_chars),
    )
    executor = Executor(pack, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation()
    for message in scenario.turns:
        await executor.on_inbound(conversation_id, message)
    return conversation_id, executor
