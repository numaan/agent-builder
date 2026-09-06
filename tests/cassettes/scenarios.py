"""The conversations the cassettes record, defined once.

Imported by both :mod:`tests.cassettes.build_cassettes` (which records them) and
``tests/test_golden_conversation.py`` (which replays them), so the two cannot drift: a scenario
is a pack, a list of customer messages, and - for an offline recording - the rules the scripted
stand-in answers with. Recording the same scenario against the live API is the same code with a
different provider, which is what PLAN.md's "live calls only in a marked ``live`` test group"
needs to be true without a second definition of the conversation.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.engine.hooks import EngineHooks
from support_core.llm.fake import Rule
from support_core.llm.provider import LLMProvider
from support_core.llm.types import ToolCall
from support_core.llm.wiring import (
    StructuredConfirmClassifier,
    StructuredSlotExtractor,
    service_for_pack,
)
from support_core.memory import LlmSummarizer
from support_core.tools.loading import import_pack_tools

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

    context: dict[str, Any] = field(default_factory=dict)
    """The ``ConversationContext`` the conversation starts with: who the customer is, and
    whether they are already verified. A refund needs a customer with an email on file."""

    setup: Callable[[], None] | None = None
    """Reset whatever the pack's tools remember, so a scenario that moves money starts from the
    same account every time. A recording of a conversation against a *drifted* backend would
    replay against a different one."""

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

# -- the phase 4 exit criterion ---------------------------------------------------------------
#
# "The refund graph runs with the fake provider through confirm and issue_refund." This is the
# whole of DESIGN.md section 19 that phase 4 owns: a gate that pushes identity verification, a
# WRITE tool exempt from confirmation, a READ tool loop inside an `llm` node, a `confirm` whose
# yes produces an ActionApproval, and the one HIGH-risk call the approval authorises.

FIND_CHARGE = "Identify which charge the customer wants refunded"
TELL_DONE = "Tell the customer the refund is on its way"
CONFIRM = "The workflow proposed one specific action to the customer"
REFUND_ASK = "I got charged twice for the Pro Plan this month; can I have one of them back?"
OTP_CODE = "581139"
"""The passcode the pack's fake sends to ``me@example.com``; see the tool's own docstring for
why it is a function of the address. Written out rather than computed, so that a change to the
fake breaks this loudly instead of silently re-recording a different conversation."""


def _reset_acme() -> None:
    """Reset the pack's fakes *through the module the registry imported*.

    ``import_pack_tools`` loads a pack under a name derived from its path, so
    ``packs.acme_billing.tools.billing`` and the registry's copy are two module objects with two
    ``BILLING`` singletons. Resetting the wrong one is invisible until a second run finds the
    charge already refunded and takes the denial path - which is exactly what happened while
    this scenario was being written.
    """
    module = import_pack_tools(ACME)
    module.reset_backend()


ACME_REFUND = Scenario(
    name="acme_refund",
    pack_path=ACME,
    context={
        "customer": {"ref": "cus_acme_1", "email": "me@example.com", "name": "Sam"},
    },
    setup=_reset_acme,
    turns=[
        REFUND_ASK,
        f"The code is {OTP_CODE}.",
        "Yes please, go ahead and refund it.",
        "No, that is all. Thanks!",
    ],
    rules=(
        Rule(
            when=REFUND_ASK,
            respond=answer(
                "refund",
                updates={"intent": "refund", "charge_hint": "one of two Pro Plan charges"},
            ),
            purpose="node",
            uses=1,
        ),
        # The model looks at the account before it picks a charge: DESIGN.md 8.4's bounded
        # READ-only loop, running the pack's real `list_recent_charges` against the fake billing
        # system. One use, so the loop makes progress rather than asking for ever.
        Rule(
            when=FIND_CHARGE,
            tool_calls=[ToolCall(id="tu_charges", name="list_recent_charges", arguments={})],
            uses=1,
        ),
        Rule(when=FIND_CHARGE, respond=answer("found", updates={"charge_id": "ch_1002"})),
        Rule(
            when=CONFIRM,
            respond={"answer": "yes", "confidence": 0.95},
            purpose="confirm",
        ),
        Rule(
            when=TELL_DONE,
            respond=answer(
                "done",
                message=(
                    "That is refunded. It takes five to seven business days to show on your "
                    "statement."
                ),
            ),
        ),
        Rule(
            when=EXTRACT,
            respond={"slots": {"code": OTP_CODE}, "unfilled": [], "confidence": 0.95},
            uses=1,
        ),
        Rule(
            when=EXTRACT,
            respond={"slots": {"anything_else": "no"}, "unfilled": [], "confidence": 0.9},
        ),
        Rule(when=CLASSIFY, respond=answer("finished", updates={"intent": "finished"})),
        Rule(
            when=SUMMARY,
            respond={
                "summary": (
                    "The customer asked for a refund of one of two duplicate Pro Plan charges. "
                    "They verified their identity with a one-time passcode and approved the "
                    "refund of the 3 September charge."
                )
            },
        ),
    ),
    expected_path=(
        "classify",
        "do_refund",
        "identity_gate",
        "send_code",
        "ask_code",
        "ask_code",
        "check_code",
        "verified_router",
        "verified",
        "identity_gate",
        "find_charge",
        "fetch_charge",
        "check_eligibility",
        "eligibility_router",
        "confirm_refund",
        "confirm_refund",
        "issue_refund",
        "tell_done",
        "done",
        "anything_else",
        "anything_else",
        "classify",
        "done_finished",
    ),
    expected_authors=(
        "customer",
        "agent",
        "customer",
        "agent",
        "customer",
        "agent",
        "agent",
        "customer",
    ),
)

SCENARIOS: tuple[Scenario, ...] = (
    ACME_SMALL_TALK,
    ACME_ACCOUNT_QUESTION,
    ACME_UNCLEAR,
    ACME_FINISHED_AT_ONCE,
    ACME_REFUND,
)


async def play(
    scenario: Scenario, engine: AsyncEngine, provider: LLMProvider
) -> tuple[uuid.UUID, Executor]:
    """Run a scenario's turns against the real engine and the given provider."""
    if scenario.setup is not None:
        scenario.setup()
    pack = load_pack(scenario.pack_path)
    service = service_for_pack(pack, provider)
    hooks = EngineHooks(
        extract_slots=StructuredSlotExtractor(service),
        confirm_decision=StructuredConfirmClassifier(service),
        summarize=LlmSummarizer(service, max_chars=pack.manifest.memory.max_summary_chars),
    )
    executor = Executor(pack, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation(
        customer_ref=(scenario.context.get("customer") or {}).get("ref"),
        context=scenario.context or None,
    )
    for message in scenario.turns:
        await executor.on_inbound(conversation_id, message)
    return conversation_id, executor
