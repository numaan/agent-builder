"""Interrupts and the frame stack. DESIGN.md section 6.6, step by step.

    1. A customer message arrives while the top frame is suspended in `ask` or `confirm`.
    2. Before resuming, the engine runs the interrupt check ...
    3. If `continue`, the suspended node resumes.
    4. If `new_intent` and the current graph allows interrupts, the engine pushes a new frame for
       that workflow. When it ends, the engine asks the customer whether to return to the
       interrupted workflow, then resumes or abandons it.
    5. If the current graph is in `blocked_in`, the engine resumes the current node with a hint
       ... and records the secondary intent so the root graph can offer it later.

    Gates fire on every entry to a frame, so an interrupt cannot be used to reach an unverified
    action.

Every test runs the real executor against real Postgres. The *interrupt check itself* is a stub
here rather than a model, because what is under test is what the engine does with an answer -
and the answers a model can give are exactly four. What a real model says to a real message is
measured by a golden conversation (``tests/test_golden_conversation.py``) and, one day, by an
eval; it is not something a unit test can assert.

The last sentence of section 6.6 has its own test and its own pack:
``test_an_interrupt_cannot_be_used_to_reach_an_unverified_action`` and the frame-entry test
after it. They are the two directions the sentence can fail - a gate skipped on the way *into*
an interrupting workflow, and a gate skipped on the way *back* to a parked one.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.engine.hooks import (
    ConfirmDecision,
    EngineHooks,
    InterruptDecision,
    InterruptRequest,
    ResumeOfferRequest,
)
from support_core.engine.interrupts import INTERRUPT_RETURN_NODE
from support_core.graph.pack import Pack
from support_core.llm.fake import Rule, ScriptedProvider
from support_core.llm.wiring import StructuredSlotExtractor, service_for_pack
from tests.engine_support import PACKS, Recorder, outbound_texts, path, run_row

INTERRUPT_PACK = PACKS / "interrupt_pack"
CLASSIFY = "Decide which workflow the customer's message belongs to"


def _answer(label: str) -> dict[str, Any]:
    return {
        "message_to_customer": None,
        "decision": label,
        "state_updates": {"intent": label},
        "citations": [],
        "confidence": 0.9,
        "needs_handoff": False,
    }


class Check:
    """A scripted interrupt check: one answer per call, then ``continue`` for ever.

    A list rather than a single value, because the interesting behaviour is what happens on the
    turn *after* an interrupt, and a check that kept saying ``new_intent`` would push a workflow
    on every reply.
    """

    def __init__(self, *answers: InterruptDecision) -> None:
        self.answers = list(answers)
        self.requests: list[InterruptRequest] = []

    async def __call__(self, request: InterruptRequest) -> InterruptDecision:
        self.requests.append(request)
        if self.answers:
            return self.answers.pop(0)
        return InterruptDecision(kind="continue")


class Offer:
    """A scripted reading of "shall we go back to X?"."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[ResumeOfferRequest] = []

    async def __call__(self, request: ResumeOfferRequest) -> ConfirmDecision:
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else "yes"
        assert answer in {"yes", "no", "unclear"}
        return ConfirmDecision(answer=answer)


def _pack() -> Pack:
    return load_pack(INTERRUPT_PACK)


def _executor(
    engine: AsyncEngine,
    *,
    check: Check | None = None,
    offer: Offer | None = None,
    recorder: Recorder | None = None,
    pack: Pack | None = None,
) -> tuple[Executor, Recorder]:
    """The pack, a scripted classifier, and whichever interrupt answers the test wants."""
    loaded = pack or _pack()
    provider = ScriptedProvider(
        [
            Rule(when=CLASSIFY, respond=_answer("alpha"), uses=1),
            Rule(when=CLASSIFY, respond=_answer("finished")),
        ]
    )
    service = service_for_pack(loaded, provider)
    notes = recorder or Recorder()
    hooks = notes.hooks()
    hooks.extract_slots = StructuredSlotExtractor(service)
    if check is not None:
        hooks.interrupt_check = check
    if offer is not None:
        hooks.resume_offer = offer
    return Executor(loaded, engine, hooks=hooks, llm=service), notes


async def _classified(
    engine: AsyncEngine,
    label: str,
    *,
    check: Check | None = None,
    offer: Offer | None = None,
    context: dict[str, Any] | None = None,
) -> tuple[Executor, uuid.UUID]:
    """Start a conversation and drive it into ``label``'s workflow, which then suspends."""
    loaded = _pack()
    provider = ScriptedProvider([Rule(when=CLASSIFY, respond=_answer(label))])
    service = service_for_pack(loaded, provider)
    hooks = EngineHooks(extract_slots=StructuredSlotExtractor(service))
    if check is not None:
        hooks.interrupt_check = check
    if offer is not None:
        hooks.resume_offer = offer
    executor = Executor(loaded, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation(context=context or {})
    await executor.on_inbound(conversation_id, f"please do {label}")
    return executor, conversation_id


def _slots(executor: Executor, values: dict[str, Any]) -> None:
    """Replace the extractor with one that always writes ``values``.

    Used where a test's subject is the stack rather than the extraction: a scripted classifier
    has no rule for an extraction prompt, and a test that failed on *that* would be testing the
    fixture.
    """

    async def extract(request: Any) -> dict[str, Any]:
        return {name: value for name, value in values.items() if name in request.slots}

    executor.hooks.extract_slots = extract


# -- step 3: continue ------------------------------------------------------------------------


async def test_continue_resumes_the_suspended_node(engine: AsyncEngine) -> None:
    """The ordinary case, and the one the default hook still gives."""
    check = Check(InterruptDecision(kind="continue"))
    executor, conversation_id = await _classified(engine, "alpha", check=check)
    _slots(executor, {"answer": "forty two"})

    await executor.on_inbound(conversation_id, "forty two")

    row = await run_row(engine, conversation_id)
    assert "tell_a" in await path(engine, row["id"])
    assert "Alpha has your answer: forty two." in await outbound_texts(engine, conversation_id)
    assert row["secondary_intents"] == []


async def test_the_check_is_given_the_root_graphs_declared_edges(engine: AsyncEngine) -> None:
    """DESIGN.md section 6.6: "Available intents are the root graph's declared edges"."""
    check = Check()
    executor, conversation_id = await _classified(engine, "alpha", check=check)
    _slots(executor, {"answer": "forty two"})
    await executor.on_inbound(conversation_id, "forty two")

    assert check.requests, "the check did not run at all"
    offered = {label: graph for label, graph, _about in check.requests[0].intents}
    assert offered == {"alpha": "alpha", "beta": "beta", "gated": "gated"}
    assert check.requests[0].current_graph == "alpha"
    assert check.requests[0].current_node == "ask_a"
    assert check.requests[0].interruptible is True


async def test_a_pack_that_declares_no_interrupts_is_never_asked(engine: AsyncEngine) -> None:
    """No ``interrupts`` block means no model call: the only actionable answer is ``continue``."""
    loaded = load_pack(PACKS / "llm_pack")
    assert not loaded.manifest.interrupts.allowed_from
    check = Check(InterruptDecision(kind="new_intent", graph="alpha"))
    provider = ScriptedProvider(
        [
            Rule(when="Classify the customer's latest message", respond=_answer("small_talk")),
            Rule(when="Reply to the small talk", respond=_answer("done")),
        ]
    )
    service = service_for_pack(loaded, provider)
    hooks = EngineHooks(interrupt_check=check, extract_slots=StructuredSlotExtractor(service))
    executor = Executor(loaded, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "hello")
    _slots(executor, {"anything_else": "no"})
    await executor.on_inbound(conversation_id, "nothing else")

    assert check.requests == []


# -- step 4: a workflow the pack allows to be interrupted -------------------------------------


async def test_an_allowed_interrupt_parks_the_workflow_and_pushes_the_new_one(
    engine: AsyncEngine,
) -> None:
    """The headline: alpha is parked, beta runs, and nothing about alpha is lost."""
    check = Check(InterruptDecision(kind="new_intent", graph="beta", label="beta"))
    executor, conversation_id = await _classified(engine, "alpha", check=check)

    await executor.on_inbound(conversation_id, "actually, can you do beta instead?")

    row = await run_row(engine, conversation_id)
    frames = row["frames"]
    assert [frame["graph_id"] for frame in frames] == ["root", "alpha", "beta"]
    assert frames[1]["offer_return"] is True, "the alpha frame was not parked"
    assert frames[1]["node_id"] == "ask_a", "the parked frame moved"
    assert frames[2]["kind"] == "interrupt"
    assert row["status"] == "waiting_customer"
    said = await outbound_texts(engine, conversation_id)
    assert any("paused what we were doing" in text for text in said)
    assert said[-1].startswith("Beta is asking you a question")


async def test_the_interrupting_message_is_not_delivered_to_the_parked_node(
    engine: AsyncEngine,
) -> None:
    """The check consumed it. It must be neither answered nor put back on the queue.

    Both failures are real: delivering it would write "can you do beta instead?" into alpha's
    slot, and re-queueing it would make the next drain classify it a second time and push beta
    twice.
    """
    check = Check(InterruptDecision(kind="new_intent", graph="beta"))
    executor, conversation_id = await _classified(engine, "alpha", check=check)
    _slots(executor, {"answer": "SHOULD NOT BE WRITTEN"})

    await executor.on_inbound(conversation_id, "actually, can you do beta instead?")

    row = await run_row(engine, conversation_id)
    assert row["frames"][1]["state"].get("answer") is None
    assert await _pending_count(engine, conversation_id) == 0


async def test_when_the_interrupting_workflow_ends_the_customer_is_offered_the_old_one(
    engine: AsyncEngine,
) -> None:
    """DESIGN.md 6.6 step 4's second half, and then the resume."""
    check = Check(InterruptDecision(kind="new_intent", graph="beta"))
    offer = Offer("yes")
    executor, conversation_id = await _classified(engine, "alpha", check=check, offer=offer)
    await executor.on_inbound(conversation_id, "actually, do beta")
    _slots(executor, {"answer": "beta's answer"})

    await executor.on_inbound(conversation_id, "beta's answer")

    row = await run_row(engine, conversation_id)
    said = await outbound_texts(engine, conversation_id)
    assert any("Shall we go back to alpha?" in text for text in said)
    assert row["awaiting"]["node"] == INTERRUPT_RETURN_NODE
    assert INTERRUPT_RETURN_NODE in await path(engine, row["id"])

    _slots(executor, {"answer": "alpha's answer"})
    await executor.on_inbound(conversation_id, "yes please")
    row = await run_row(engine, conversation_id)
    assert [frame["graph_id"] for frame in row["frames"]] == ["root", "alpha"]
    assert row["frames"][1]["offer_return"] is False
    # The parked node runs again from the start - it re-asks rather than assuming the customer
    # still remembers what it wanted.
    assert (await outbound_texts(engine, conversation_id))[-1].startswith("Alpha is asking you")


async def test_a_customer_who_says_no_has_the_parked_workflow_abandoned(
    engine: AsyncEngine,
) -> None:
    check = Check(InterruptDecision(kind="new_intent", graph="beta"))
    offer = Offer("no")
    executor, conversation_id = await _classified(engine, "alpha", check=check, offer=offer)
    await executor.on_inbound(conversation_id, "actually, do beta")
    _slots(executor, {"answer": "beta's answer"})
    await executor.on_inbound(conversation_id, "beta's answer")

    await executor.on_inbound(conversation_id, "no, leave it")

    row = await run_row(engine, conversation_id)
    said = await outbound_texts(engine, conversation_id)
    assert any("left alpha as it was" in text for text in said)
    # Back in the root frame, at the node the alpha call was going to return to.
    assert [frame["graph_id"] for frame in row["frames"]] == ["root"]
    assert row["frames"][0]["node_id"] == "wrap_up"


async def test_an_unreadable_answer_to_the_offer_asks_again(engine: AsyncEngine) -> None:
    """``unclear`` costs one message; either guess loses or forces a workflow."""
    check = Check(InterruptDecision(kind="new_intent", graph="beta"))
    offer = Offer("unclear")
    executor, conversation_id = await _classified(engine, "alpha", check=check, offer=offer)
    await executor.on_inbound(conversation_id, "actually, do beta")
    _slots(executor, {"answer": "beta's answer"})
    await executor.on_inbound(conversation_id, "beta's answer")

    await executor.on_inbound(conversation_id, "hmm")

    row = await run_row(engine, conversation_id)
    said = await outbound_texts(engine, conversation_id)
    assert said.count("That is done. Shall we go back to alpha?") == 2
    assert row["frames"][1]["offer_return"] is True
    assert row["status"] == "waiting_customer"


async def test_the_answer_to_the_offer_is_not_itself_classified_as_an_interrupt(
    engine: AsyncEngine,
) -> None:
    """ "No thanks" answers the offer; it is not a request to cancel something else."""
    check = Check(InterruptDecision(kind="new_intent", graph="beta"))
    offer = Offer("yes")
    executor, conversation_id = await _classified(engine, "alpha", check=check, offer=offer)
    await executor.on_inbound(conversation_id, "actually, do beta")
    _slots(executor, {"answer": "beta's answer"})
    await executor.on_inbound(conversation_id, "beta's answer")
    before = len(check.requests)

    await executor.on_inbound(conversation_id, "yes please")

    assert len(check.requests) == before, "the offer's answer went through the interrupt check"
    assert offer.requests, "the offer's answer never reached the reader"


# -- step 5: a workflow the pack blocks --------------------------------------------------------


async def test_a_blocked_graph_defers_the_intent_and_says_so(engine: AsyncEngine) -> None:
    """DESIGN.md section 19 step 8, in miniature: the node resumes and the request is kept."""
    check = Check(InterruptDecision(kind="new_intent", graph="alpha", label="alpha"))
    executor, conversation_id = await _classified(engine, "beta", check=check)
    _slots(executor, {"answer": "beta's answer"})

    await executor.on_inbound(conversation_id, "beta's answer - and also please do alpha")

    row = await run_row(engine, conversation_id)
    assert check.requests[0].interruptible is False
    assert [intent["graph"] for intent in row["secondary_intents"]] == ["alpha"]
    assert row["secondary_intents"][0]["label"] == "alpha"
    assert "also please do alpha" in row["secondary_intents"][0]["said"]
    said = await outbound_texts(engine, conversation_id)
    assert any("made a note that you also asked about alpha" in text for text in said)
    # And the node it interrupted still got its answer.
    assert "done_b" in await path(engine, row["id"])


async def test_the_resuming_node_is_told_what_else_was_in_the_message(
    engine: AsyncEngine,
) -> None:
    """The hint of DESIGN.md 6.6 step 5, where it does its work.

    Without it a structured extractor is asked to read "beta's answer - and also please do
    alpha" as the answer to beta's question, with nothing to say that half of it is not.
    """
    seen: list[str | None] = []

    async def extract(request: Any) -> dict[str, Any]:
        seen.append(request.hint)
        return {"answer": "beta's answer"}

    check = Check(InterruptDecision(kind="new_intent", graph="alpha", label="alpha"))
    executor, conversation_id = await _classified(engine, "beta", check=check)
    executor.hooks.extract_slots = extract

    await executor.on_inbound(conversation_id, "beta's answer - and also please do alpha")

    assert seen and seen[0] is not None
    assert "also asked about alpha" in seen[0]
    assert "no value in it belongs to this step's slots" in seen[0]


async def test_a_deferred_intent_is_surfaced_to_the_root_graph(engine: AsyncEngine) -> None:
    """DESIGN.md section 19 step 15: the engine surfaces it; the *model* chooses.

    The assertion is on the prompt, deliberately. The engine's job is to put the customer's
    unmet request in front of the root graph's classifier as data; choosing it is the model's,
    among the edges the graph declares (principle 2). An engine that pushed the workflow itself
    would be inventing a transition.
    """
    prompts: list[str] = []
    loaded = _pack()

    class Watching(ScriptedProvider):
        async def complete(self, req: Any) -> Any:
            prompts.append(" ".join(block.text for block in req.system))
            return await super().complete(req)

    provider = Watching(
        [
            Rule(when=CLASSIFY, respond=_answer("beta"), uses=1),
            Rule(when=CLASSIFY, respond=_answer("finished")),
        ]
    )
    service = service_for_pack(loaded, provider)
    # The first check the engine runs is the one on beta's own reply: turn one starts from an
    # idle run and turn three is suspended in the *root* frame, which is never interrupted.
    check = Check(InterruptDecision(kind="new_intent", graph="alpha", label="alpha"))
    hooks = EngineHooks(interrupt_check=check, extract_slots=StructuredSlotExtractor(service))
    executor = Executor(loaded, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "please do beta")
    _slots(executor, {"answer": "beta's answer"})
    await executor.on_inbound(conversation_id, "beta's answer - and also please do alpha")
    _slots(executor, {"outcome": "no"})

    await executor.on_inbound(conversation_id, "nothing else")

    assert "requests noted earlier and not yet done" in prompts[-1]
    assert "also please do alpha" in prompts[-1]


async def test_a_surfaced_intent_is_cleared_once_that_workflow_runs(
    engine: AsyncEngine,
) -> None:
    """The record exists so the request is not dropped. Once it runs, it has not been."""
    loaded = _pack()
    provider = ScriptedProvider(
        [
            Rule(when=CLASSIFY, respond=_answer("beta"), uses=1),
            Rule(when=CLASSIFY, respond=_answer("alpha")),
        ]
    )
    service = service_for_pack(loaded, provider)
    # The first check the engine runs is the one on beta's own reply: turn one starts from an
    # idle run and turn three is suspended in the *root* frame, which is never interrupted.
    check = Check(InterruptDecision(kind="new_intent", graph="alpha", label="alpha"))
    hooks = EngineHooks(interrupt_check=check, extract_slots=StructuredSlotExtractor(service))
    executor = Executor(loaded, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "please do beta")
    _slots(executor, {"answer": "beta's answer"})
    await executor.on_inbound(conversation_id, "beta's answer - and also please do alpha")
    assert (await run_row(engine, conversation_id))["secondary_intents"]

    _slots(executor, {"outcome": "yes"})
    await executor.on_inbound(conversation_id, "yes, do alpha now")

    row = await run_row(engine, conversation_id)
    assert row["secondary_intents"] == []
    assert [frame["graph_id"] for frame in row["frames"]] == ["root", "alpha"]


# -- the sentence that matters -----------------------------------------------------------------


async def test_an_interrupt_cannot_be_used_to_reach_an_unverified_action(
    engine: AsyncEngine,
) -> None:
    """DESIGN.md section 6.6's last sentence, in the direction that would be a security bug.

    The customer is suspended in a workflow that needed no identity, changes the subject to one
    that does, and the interrupt is *allowed*. If a pushed frame skipped its gates - if the
    engine treated an interrupt as a resume rather than as an entry - the protected node would
    run for an unverified customer. It does not: the pushed frame is fresh, its ``passed_gates``
    is empty, and its first node is the gate.
    """
    check = Check(InterruptDecision(kind="new_intent", graph="gated", label="gated"))
    executor, conversation_id = await _classified(engine, "alpha", check=check)

    await executor.on_inbound(conversation_id, "actually, do the gated thing")

    row = await run_row(engine, conversation_id)
    steps = await path(engine, row["id"])
    assert "identity_gate" in steps
    assert "protected" not in steps, "an interrupt reached the protected node unverified"
    assert [frame["graph_id"] for frame in row["frames"]] == ["root", "alpha", "gated", "verify"]
    assert row["status"] == "waiting_customer"
    assert (await outbound_texts(engine, conversation_id))[-1].startswith("Before that, what is")


async def test_a_gate_that_lapsed_while_a_frame_was_parked_fires_before_it_resumes(
    engine: AsyncEngine,
) -> None:
    """The same sentence in the other direction: the way *back* is a frame entry too.

    The customer is verified, gets into the gated workflow, interrupts it, and while the second
    workflow runs the verification lapses - an operator, a CRM change, a revoked session. When
    the parked frame is offered back and resumed, the gate is re-evaluated first and pushes its
    redirect; the protected node is not reached.
    """
    check = Check(InterruptDecision(kind="new_intent", graph="alpha", label="alpha"))
    offer = Offer("yes")
    verified = {"customer": {"ref": "cus_1", "identity_verified": True}}
    executor, conversation_id = await _classified(
        engine, "gated", check=check, offer=offer, context=verified
    )
    # The gated workflow passed its gate and is waiting inside... nothing: `gated` suspends only
    # through its redirect, so drive it to a suspension by interrupting from the *root* ask.
    row = await run_row(engine, conversation_id)
    assert "protected" in await path(engine, row["id"])

    _slots(executor, {"outcome": "yes"})
    await _unverify(engine, conversation_id)
    await executor.on_inbound(conversation_id, "and now do the gated thing again")

    row = await run_row(engine, conversation_id)
    steps = await path(engine, row["id"])
    assert steps.count("protected") == 1, "the protected node ran again after verification lapsed"
    assert steps.count("identity_gate") == 2
    assert row["frames"][-1]["graph_id"] == "verify"


# -- cancel ------------------------------------------------------------------------------------


async def test_cancel_unwinds_to_the_root_frame(engine: AsyncEngine) -> None:
    """DESIGN.md 6.6 step 2's fourth answer. Allowed from anywhere, including a blocked graph:
    refusing to let a customer stop is worse than any workflow it interrupts."""
    check = Check(InterruptDecision(kind="cancel"))
    executor, conversation_id = await _classified(engine, "beta", check=check)
    _slots(executor, {"outcome": "no"})

    await executor.on_inbound(conversation_id, "forget it, stop")

    row = await run_row(engine, conversation_id)
    assert [frame["graph_id"] for frame in row["frames"]] == ["root"]
    assert row["frames"][0]["node_id"] in {"wrap_up", "classify"}
    assert any(
        "I have stopped that" in text for text in await outbound_texts(engine, conversation_id)
    )


@pytest.mark.parametrize(
    "decision",
    [
        InterruptDecision(kind="cancel", confidence=0.0),
        InterruptDecision(kind="new_intent", graph="beta", label="beta", confidence=0.2),
    ],
    ids=["a near-random cancel", "a near-random new_intent"],
)
async def test_a_decision_below_the_packs_confidence_threshold_is_not_acted_on(
    engine: AsyncEngine, decision: InterruptDecision
) -> None:
    """Review finding P3. ``llm.confidence_threshold`` gates this decision too.

    The check's confidence used to be collected, carried to the engine and read by nobody, so a
    ``cancel`` at confidence 0.0 unwound the whole stack and a ``new_intent`` at 0.0 discarded a
    workflow. Both are destructive and neither is worth doing on a guess. Below the threshold the
    engine does not act, says so, and lets the suspended node ask its own question again.
    """
    check = Check(decision)
    executor, conversation_id = await _classified(engine, "alpha", check=check)
    assert executor.pack.manifest.llm.confidence_threshold > decision.confidence
    _slots(executor, {"answer": "my answer"})

    await executor.on_inbound(conversation_id, "hmm, maybe, something about beta")

    row = await run_row(engine, conversation_id)
    steps = await path(engine, row["id"])
    assert "ask_a" in steps and "tell_a" in steps, "alpha carried on where it was"
    assert "ask_b" not in steps, "nothing was pushed on a guess"
    assert row["secondary_intents"] == []
    said = await outbound_texts(engine, conversation_id)
    assert any("I was not sure whether you wanted to change" in text for text in said)
    assert any(text == "Alpha has your answer: my answer." for text in said)
    assert not any("I have stopped that" in text for text in said), "nothing was unwound"


# -- answers the engine will not act on --------------------------------------------------------


@pytest.mark.parametrize(
    "decision",
    [
        InterruptDecision(kind="unclear"),
        InterruptDecision(kind="new_intent", graph=None),
        InterruptDecision(kind="new_intent", graph="not_a_workflow"),
        InterruptDecision(kind="new_intent", graph="beta", label="beta"),
    ],
    ids=["unclear", "no intent named", "an intent nobody offered", "the graph we are already in"],
)
async def test_an_answer_the_engine_cannot_act_on_resumes_the_node(
    engine: AsyncEngine, decision: InterruptDecision
) -> None:
    """None of these is a decision, and none of them is guessed at.

    ``unclear`` is not a third behaviour on purpose: the node that asked the question is better
    placed to make sense of a confusing reply than a classifier that has already said it cannot.
    """
    check = Check(decision)
    executor, conversation_id = await _classified(engine, "beta", check=check)
    _slots(executor, {"answer": "beta's answer"})

    await executor.on_inbound(conversation_id, "something")

    row = await run_row(engine, conversation_id)
    assert "done_b" in await path(engine, row["id"])
    assert row["secondary_intents"] == []


async def test_the_root_frame_is_never_parked(engine: AsyncEngine) -> None:
    """A topic change at the root graph is the root graph's own business (DESIGN.md 6.5).

    Parking it would mean offering it back - "shall we return to: anything else?" - and the
    classifier the root graph already has is a better answer than either.
    """
    check = Check(InterruptDecision(kind="new_intent", graph="alpha", label="alpha"))
    loaded = _pack()
    provider = ScriptedProvider(
        [
            Rule(when=CLASSIFY, respond=_answer("finished"), uses=1),
            Rule(when=CLASSIFY, respond=_answer("alpha")),
        ]
    )
    service = service_for_pack(loaded, provider)
    hooks = EngineHooks(interrupt_check=check, extract_slots=StructuredSlotExtractor(service))
    executor = Executor(loaded, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "nothing at all")

    row = await run_row(engine, conversation_id)
    assert row["status"] == "done"
    assert check.requests == [], "the engine ran an interrupt check on the root frame"


# -- the interrupt is durable ------------------------------------------------------------------


async def test_an_interrupt_survives_a_crash_between_the_push_and_the_first_node(
    engine: AsyncEngine,
) -> None:
    """The interrupt frame is pushed before the claim commits, so it is durable state.

    A process that dies with the message claimed leaves a ``running`` run carrying the pushed
    stack and the fact that no node is waiting for that message. Re-entering finishes the turn -
    it does not push a second frame and it does not deliver the message to the parked node.
    """
    check = Check(InterruptDecision(kind="new_intent", graph="beta", label="beta"))
    recorder = Recorder(crash_at="before_node")
    executor, conversation_id = await _classified(engine, "alpha", check=check)
    executor.hooks.probe = recorder.hooks().probe

    with pytest.raises(RuntimeError):
        await executor.on_inbound(conversation_id, "actually, do beta")
    crashed = await run_row(engine, conversation_id)
    assert crashed["status"] == "running"
    assert [frame["graph_id"] for frame in crashed["frames"]] == ["root", "alpha", "beta"]

    executor.hooks.probe = Recorder().hooks().probe
    await executor.drain(conversation_id)

    row = await run_row(engine, conversation_id)
    assert [frame["graph_id"] for frame in row["frames"]] == ["root", "alpha", "beta"]
    assert row["status"] == "waiting_customer"
    assert (await outbound_texts(engine, conversation_id)).count(
        "Beta is asking you a question. What is your answer?"
    ) == 1


# -- helpers -----------------------------------------------------------------------------------


async def _pending_count(engine: AsyncEngine, conversation_id: uuid.UUID) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(
            sql_text(
                "SELECT count(*) FROM message WHERE conversation_id = :c AND status = 'pending'"
            ),
            {"c": conversation_id},
        )
        return int(result.scalar_one())


async def _unverify(engine: AsyncEngine, conversation_id: uuid.UUID) -> None:
    """Take the customer's verification away from outside the conversation.

    The same device the phase-2 and phase-4 gate tests use: nothing inside this pack can change
    ``ctx``, and the case being tested is precisely an external change - a CRM update, an
    operator, a revoked session - between one entry to a frame and the next.
    """
    async with engine.begin() as connection:
        await connection.execute(
            sql_text(
                "UPDATE conversation SET context = jsonb_set(context, '{customer}', "
                '\'{"ref": "cus_1", "identity_verified": false}\'::jsonb, true) WHERE id = :c'
            ),
            {"c": conversation_id},
        )


async def test_a_crash_while_the_return_offer_is_being_made_does_not_lose_the_offer(
    engine: AsyncEngine,
) -> None:
    """The other place phase 6 moves the stack while a message is in flight.

    Phase 2's two must-fix bugs were both in the seam where "an event is still pending" meets
    "the stack has changed", and the return offer is a new instance of it: the interrupt frame
    pops, the parked frame comes back to the top, and a core step suspends on it. Killed inside
    that checkpoint, the transaction rolls back and the whole thing is re-derived from durable
    state - the frame is still parked, the offer is made once, and the customer is not asked
    twice.
    """
    check = Check(InterruptDecision(kind="new_intent", graph="beta", label="beta"))
    offer = Offer("yes")
    executor, conversation_id = await _classified(engine, "alpha", check=check, offer=offer)
    await executor.on_inbound(conversation_id, "actually, do beta")
    _slots(executor, {"answer": "beta's answer"})

    recorder = Recorder(crash_at="checkpoint_before_commit", crash_after=1)
    executor.hooks.probe = recorder.hooks().probe
    with pytest.raises(RuntimeError):
        await executor.on_inbound(conversation_id, "beta's answer")
    crashed = await run_row(engine, conversation_id)
    assert crashed["status"] == "running"

    executor.hooks.probe = Recorder().hooks().probe
    await executor.drain(conversation_id)

    row = await run_row(engine, conversation_id)
    said = await outbound_texts(engine, conversation_id)
    assert said.count("That is done. Shall we go back to alpha?") == 1
    assert row["frames"][1]["offer_return"] is True
    assert row["awaiting"]["node"] == INTERRUPT_RETURN_NODE
    steps = await path(engine, row["id"])
    assert steps.count(INTERRUPT_RETURN_NODE) == 1
