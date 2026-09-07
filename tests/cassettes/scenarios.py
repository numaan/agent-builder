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
from support_core.engine.interrupts import workflow_intents
from support_core.handoff import HandoffService, HandoffSink, default_sink
from support_core.knowledge.wiring import build_ingestor, build_retriever
from support_core.llm.fake import Rule
from support_core.llm.provider import LLMProvider
from support_core.llm.types import ToolCall
from support_core.llm.wiring import (
    StructuredConfirmClassifier,
    StructuredInterruptCheck,
    StructuredResumeOffer,
    StructuredSlotExtractor,
    service_for_pack,
)
from support_core.memory import LlmSummarizer
from support_core.storage.session import make_session_factory
from support_core.tools.loading import import_pack_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = Path(__file__).resolve().parent
ACME = REPO_ROOT / "packs" / "acme_billing"

CLASSIFY = "Read the customer's most recent message and decide which path"
CHAT = "Reply to the customer's small talk"
EXTRACT = "The workflow asked the customer for specific values"
SUMMARY = "Write a short factual summary"
INTERRUPT = "A workflow is part-way through and has just asked the customer a question"
RESUME_OFFER = "changed the subject, and has now finished the"
HANDOFF = "handed to a human support agent"
COLLECT = "The customer wants to change the postal address on their account"


def _interrupt(kind: str, intent: str | None = None, confidence: float = 0.9) -> dict[str, Any]:
    """One answer from DESIGN.md section 6.6's interrupt check."""
    return {"kind": kind, "intent": intent, "confidence": confidence}


def answer(
    label: str,
    *,
    message: str | None = None,
    confidence: float = 0.9,
    updates: dict[str, Any] | None = None,
    citations: Sequence[str] = (),
) -> dict[str, Any]:
    """One structured answer, as the model would return it.

    ``citations`` is the ids from the node's knowledge block that the message rests on
    (DESIGN.md 9.2). A scripted answer that states a policy, a price or a timing without one is
    refused by the outbound guardrail and the conversation goes to a person - which is correct
    behaviour, and is why the refund scenarios' closing message names ``k1``.
    """
    return {
        "message_to_customer": message,
        "decision": label,
        "state_updates": dict(updates or {}),
        "citations": list(citations),
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
        # The one part of a handoff packet a model writes (DESIGN.md sections 5.1, 13), on the
        # escalation model. Recorded, so the golden conversation proves the packet's summary is
        # the model's and not the fallback the service uses when a summariser is down.
        Rule(
            when=HANDOFF,
            respond={
                "summary": (
                    "The customer asked why they were charged 40 dollars on the 3rd. This pack "
                    "has no workflow for looking up an individual charge, so nothing was checked "
                    "and nothing was changed on the account. Their identity is not verified."
                )
            },
            purpose="handoff",
        ),
    ),
    expected_path=("classify", "no_workflow"),
    expected_authors=("customer", "agent"),
    # An account question this pack has no workflow for used to dead-end, and then said honestly
    # that nobody had been told. Phase 6 makes `no_workflow` a real `handoff` node: the run parks
    # `waiting_human` with a packet on the billing-tier-1 queue, which is what the message now
    # claims and what the desk API can act on.
    expected_status="waiting_human",
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
                citations=["k1"],
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
        # DESIGN.md section 6.6 runs on every reply to a suspended workflow, so it is part of
        # this conversation whether or not the customer changes the subject. Recorded rather than
        # left to fail, so the replay is the conversation the service would really have.
        Rule(when=INTERRUPT, respond=_interrupt("continue"), purpose="interrupt"),
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

# -- the phase 6 exit criterion ---------------------------------------------------------------
#
# DESIGN.md section 19 end to end, including the two things phase 6 owns: the interrupt at step 7
# and the secondary intent at step 15.
#
#   7.  Customer: "sure, me@example.com. Also, can you change my address?"
#   8.  Engine: ... Because `verify_identity` is in `blocked_in`, the engine resumes with a hint.
#   15. `root` loops to classify; the engine surfaces the recorded secondary intent. The model
#       chooses `update_address`. Push that workflow.
#
# The interrupt lands one node later than the design's, because this pack's verify_identity
# sends the passcode to the address already on file rather than asking for the email first
# (phase 4's deviation). Everything else is the design's own conversation.

INTERRUPT_ASK = "The code is 581139. Also, can you change my address while we are at it?"
NEW_ADDRESS = "Yes please - it is 4 Elm Row, Edinburgh, EH7 4AH, United Kingdom."
INTERRUPT_MID_REFUND = (
    "Actually, before that - can you change my address to 4 Elm Row, Edinburgh, EH7 4AH, "
    "United Kingdom?"
)
ADDRESS_FIELDS = {
    "line1": "4 Elm Row",
    "line2": None,
    "city": "Edinburgh",
    "postcode": "EH7 4AH",
    "country": "United Kingdom",
}


ACME_INTERRUPT_DEFERRED = Scenario(
    name="acme_interrupt_deferred",
    pack_path=ACME,
    context={"customer": {"ref": "cus_acme_1", "email": "me@example.com", "name": "Sam"}},
    setup=_reset_acme,
    turns=[REFUND_ASK, INTERRUPT_ASK, "Yes please, go ahead and refund it.", NEW_ADDRESS],
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
        # Step 8. The customer answers the passcode question *and* asks for something else. The
        # check says which workflow; the engine, not the check, decides that verify_identity does
        # not stop for it.
        Rule(
            when=INTERRUPT_ASK,
            respond=_interrupt("new_intent", "update_address"),
            purpose="interrupt",
            uses=1,
        ),
        Rule(when=INTERRUPT, respond=_interrupt("continue"), purpose="interrupt"),
        # The extractor is told a topic change was deferred, so it takes the code and leaves the
        # address alone - the whole reason the hint exists.
        Rule(
            when=EXTRACT,
            respond={"slots": {"code": OTP_CODE}, "unfilled": [], "confidence": 0.95},
            uses=1,
        ),
        Rule(
            when=FIND_CHARGE,
            tool_calls=[ToolCall(id="tu_charges", name="list_recent_charges", arguments={})],
            uses=1,
        ),
        Rule(when=FIND_CHARGE, respond=answer("found", updates={"charge_id": "ch_1002"})),
        Rule(when=CONFIRM, respond={"answer": "yes", "confidence": 0.95}, purpose="confirm"),
        Rule(
            when=TELL_DONE,
            respond=answer(
                "done",
                message=(
                    "That is refunded. It takes five to seven business days to show on your "
                    "statement."
                ),
                citations=["k1"],
            ),
        ),
        # Step 15: the root graph is shown the request it could not take up, and chooses it.
        Rule(
            when="requests noted earlier and not yet done",
            respond=answer("update_address", updates={"intent": "update_address"}),
            purpose="node",
        ),
        Rule(when=COLLECT, respond=answer("complete", updates=ADDRESS_FIELDS)),
        Rule(
            when=EXTRACT,
            respond={"slots": {"anything_else": NEW_ADDRESS}, "unfilled": [], "confidence": 0.9},
        ),
        Rule(when=CLASSIFY, respond=answer("finished", updates={"intent": "finished"})),
        Rule(
            when=SUMMARY,
            respond={
                "summary": (
                    "The customer asked for a refund of a duplicate Pro Plan charge and, while "
                    "verifying their identity, also asked to change their address. The refund "
                    "was approved and issued; the address change was picked up afterwards."
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
        # Turn two: the interrupt is refused by verify_identity, the code is extracted anyway.
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
        # Turn four: classify sees the deferred intent and pushes the address workflow.
        "anything_else",
        "classify",
        "do_update_address",
        "identity_gate",
        "read_current",
        "collect",
        "confirm_change",
    ),
    expected_authors=(
        "customer",
        "agent",
        "customer",
        "agent",
        "agent",
        "customer",
        "agent",
        "agent",
        "customer",
        "agent",
    ),
    expected_status="waiting_customer",
)

ACME_INTERRUPT_SWITCH = Scenario(
    name="acme_interrupt_switch",
    pack_path=ACME,
    # Already verified, so the conversation reaches a suspension *inside the refund graph* - the
    # one place this pack allows an interrupt - in one turn. A scenario may say so where a
    # deployment's configuration may not (DESIGN.md section 10): this is the state the
    # verification workflow would have left, written down instead of walked through again.
    context={
        "customer": {
            "ref": "cus_acme_1",
            "email": "me@example.com",
            "name": "Sam",
            "identity_verified": True,
        }
    },
    setup=_reset_acme,
    turns=[
        REFUND_ASK,
        INTERRUPT_MID_REFUND,
        "Yes, go ahead with the address.",
        "Yes please, back to the refund.",
        "Yes, refund it.",
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
        Rule(
            when=FIND_CHARGE,
            tool_calls=[ToolCall(id="tu_charges", name="list_recent_charges", arguments={})],
            uses=1,
        ),
        Rule(when=FIND_CHARGE, respond=answer("found", updates={"charge_id": "ch_1002"})),
        # The refund graph is in `interrupts.allowed_from`, so this one is taken: the refund is
        # parked mid-confirmation and the address workflow is pushed over it.
        Rule(
            when=INTERRUPT_MID_REFUND,
            respond=_interrupt("new_intent", "update_address"),
            purpose="interrupt",
            uses=1,
        ),
        Rule(when=INTERRUPT, respond=_interrupt("continue"), purpose="interrupt"),
        Rule(when=COLLECT, respond=answer("complete", updates=ADDRESS_FIELDS)),
        Rule(when=CONFIRM, respond={"answer": "yes", "confidence": 0.95}, purpose="confirm"),
        # And when it ends, the engine asks whether to go back to the refund.
        Rule(
            when=RESUME_OFFER,
            respond={"answer": "yes", "confidence": 0.95},
            purpose="resume_offer",
        ),
        Rule(
            when=TELL_DONE,
            respond=answer(
                "done",
                message=(
                    "That is refunded. It takes five to seven business days to show on your "
                    "statement."
                ),
                citations=["k1"],
            ),
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
                    "The customer asked for a refund, changed the subject to their address "
                    "mid-confirmation, completed the address change and then came back to the "
                    "refund and approved it."
                )
            },
        ),
    ),
    expected_path=(
        "classify",
        "do_refund",
        "identity_gate",
        "find_charge",
        "fetch_charge",
        "check_eligibility",
        "eligibility_router",
        "confirm_refund",
        # Turn two: the refund frame is parked and update_address is pushed over it.
        "identity_gate",
        "read_current",
        "collect",
        "confirm_change",
        # Turn three: the address change completes and the parked refund is offered back.
        "confirm_change",
        "apply_change",
        "tell_done",
        "done_changed",
        "__interrupt_return__",
        # Turn four: "yes" - the refund's confirm re-presents its proposal from scratch.
        "__interrupt_return__",
        "confirm_refund",
        # Turn five: the approval is given to the proposal that was just shown.
        "confirm_refund",
        "issue_refund",
        "tell_done",
        "done",
        "anything_else",
    ),
    expected_authors=(
        "customer",
        "agent",
        "customer",
        "agent",
        "agent",
        "customer",
        "agent",
        "agent",
        "customer",
        "agent",
        "customer",
        "agent",
        "agent",
    ),
    expected_status="waiting_customer",
)

SCENARIOS: tuple[Scenario, ...] = (
    ACME_SMALL_TALK,
    ACME_ACCOUNT_QUESTION,
    ACME_UNCLEAR,
    ACME_FINISHED_AT_ONCE,
    ACME_REFUND,
    ACME_INTERRUPT_DEFERRED,
    ACME_INTERRUPT_SWITCH,
)


async def sync_pack_knowledge(pack: Any, engine: AsyncEngine) -> None:
    """Run the real ingestion for every document source the pack declares.

    The real one, not a fixture that inserts rows: a golden conversation is meant to be what the
    service would really do, and the chunk boundaries and locators a customer's citation names
    come out of the chunker rather than out of a test.
    """
    if not pack.knowledge.documents:
        return
    ingestor = build_ingestor(pack.path, make_session_factory(engine), store=None)
    for source in pack.knowledge.documents:
        await ingestor.sync_source(source)


async def play(
    scenario: Scenario,
    engine: AsyncEngine,
    provider: LLMProvider,
    *,
    sink: HandoffSink | None = None,
) -> tuple[uuid.UUID, Executor]:
    """Run a scenario's turns against the real engine and the given provider.

    ``sink`` overrides the queue the handoff packet goes to, which is how a test drives a whole
    real conversation into a desk that is down (review finding P2).
    """
    if scenario.setup is not None:
        scenario.setup()
    pack = load_pack(scenario.pack_path)
    service = service_for_pack(pack, provider)
    # The pack's knowledge, synced into this database before the conversation starts (DESIGN.md
    # 9.3). A golden conversation has to run against a corpus, because two of the sample pack's
    # `llm` nodes carry a `knowledge:` block and the citation guardrail refuses what they say
    # without one - so a scenario played against an unsynced database would record a handoff and
    # call it the worked example.
    #
    # Qdrant is deliberately *not* used here. A recording has to be reproducible on a machine
    # that has only Postgres, and the lexical and dense halves are enough to ground these
    # answers; what a running service does with the vector side beside them is tested in
    # tests/test_knowledge_retrieval.py rather than baked into a fixture.
    await sync_pack_knowledge(pack, engine)
    hooks = EngineHooks(
        extract_slots=StructuredSlotExtractor(service),
        confirm_decision=StructuredConfirmClassifier(service),
        summarize=LlmSummarizer(service, max_chars=pack.manifest.memory.max_summary_chars),
        # DESIGN.md section 6.6's interrupt check and its return offer, wired exactly as
        # `build_runtime` wires them, so a golden conversation exercises what the service runs.
        interrupt_check=StructuredInterruptCheck(
            service, [intent.as_tuple() for intent in workflow_intents(pack)]
        ),
        resume_offer=StructuredResumeOffer(service),
    )
    executor = Executor(
        pack,
        engine,
        hooks=hooks,
        llm=service,
        retriever=build_retriever(
            pack.knowledge, make_session_factory(engine), use_qdrant=False
        ),
    )
    # DESIGN.md section 13, wired after the executor because the sink writes through the
    # executor's own session factory: one database connection story, not two.
    hooks.handoff = HandoffService(
        pack,
        executor.sessions,
        sink=sink or default_sink(executor.sessions),
        llm=service,
        retriever=executor.retriever,
    )
    conversation_id = await executor.start_conversation(
        customer_ref=(scenario.context.get("customer") or {}).get("ref"),
        context=scenario.context or None,
    )
    for message in scenario.turns:
        await executor.on_inbound(conversation_id, message)
    return conversation_id, executor
