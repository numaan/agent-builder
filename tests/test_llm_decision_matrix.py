"""The review's decision-constraint matrix, as a test. DESIGN.md principle 2 and section 11.3.

    The LLM chooses among transitions the graph allows ... It never invents a transition.

reviews/phase-3.md drove twenty-five hostile payloads through the real ``LlmRunner`` with a
provider that returns exactly what it is given. Twenty-three were refused; two - a state write on
a node that declares no ``output_schema``, and an ill-typed one - were routed and checkpointed,
which is finding V2. The probe was written under a scratch directory and deleted, so it lives
here now, all twenty-five, and it is what says the count went from 23/25 refused to 25/25.

Every case runs the real executor against real Postgres. The provider is scripted, because the
subject is what the *engine* does with an answer, not what a model would answer.
"""

import dataclasses
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.graph.pack import Pack
from support_core.llm.fake import Rule, ScriptedProvider
from support_core.llm.types import CompletionResponse
from support_core.llm.wiring import StructuredSlotExtractor, service_for_pack
from tests.engine_support import PACKS, Recorder, run_row, trace_rows

LLM_PACK = PACKS / "llm_pack"
CLASSIFY = "Classify the customer's latest message"
CHAT = "Reply to the small talk"

ZWSP = "​"
DOTLESS_I = "ı"  # noqa: RUF001 - the look-alike is the point: it is the review's own payload


def payload(**overrides: Any) -> dict[str, Any]:
    """One :class:`~support_core.llm.schemas.LlmNodeOutput`, as a model would return it."""
    base: dict[str, Any] = {
        "message_to_customer": None,
        "decision": "small_talk",
        "state_updates": {},
        "citations": [],
        "confidence": 0.9,
        "needs_handoff": False,
    }
    base.update(overrides)
    return base


async def _no_sleep(seconds: float) -> None:
    pass


@pytest.fixture
def pack() -> Pack:
    return load_pack(LLM_PACK)


@dataclasses.dataclass(slots=True)
class Outcome:
    status: str | None
    reasons: list[str]
    calls: int
    patches: list[dict[str, Any] | None]
    edges: list[str | None]


async def drive(pack: Pack, engine: AsyncEngine, rules: list[Rule]) -> Outcome:
    provider = ScriptedProvider(rules)
    service = service_for_pack(pack, provider, sleep=_no_sleep)
    recorder = Recorder()
    hooks = dataclasses.replace(recorder.hooks(), extract_slots=StructuredSlotExtractor(service))
    executor = Executor(pack, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation()
    result = await executor.on_inbound(conversation_id, "hello there")
    row = await run_row(engine, conversation_id)
    steps = await trace_rows(engine, row["id"])
    return Outcome(
        status=result.status,
        reasons=[request.reason or "" for request in recorder.handoffs],
        calls=len(provider.calls),
        patches=[step["state_patch"] for step in steps],
        edges=[step["edge"] for step in steps],
    )


# -- the twenty-five payloads ------------------------------------------------------------------
# Each is (id, the answer the provider returns at `classify`), and every one of them must be
# refused: a NodeError with an accurate reason, never a guess and never a third attempt.

REFUSED: list[tuple[str, dict[str, Any]]] = [
    ("undeclared-edge", payload(decision="refund_now")),
    ("case-differs", payload(decision="Small_Talk")),
    ("trailing-whitespace", payload(decision="small_talk ")),
    ("leading-newline", payload(decision="\nsmall_talk")),
    ("empty-decision", payload(decision="")),
    ("null-decision", payload(decision=None)),
    ("decision-is-a-list", payload(decision=["small_talk"])),
    ("decision-is-an-int", payload(decision=1)),
    ("no-decision-key", {"confidence": 0.9}),
    ("unicode-lookalike", payload(decision=f"small_ta{DOTLESS_I}k")),
    ("zwsp-in-the-label", payload(decision=f"small{ZWSP}_talk")),
    ("confidence-above-one", payload(confidence=1.5)),
    ("confidence-below-zero", payload(confidence=-1.0)),
    ("extra-field", {**payload(), "authorised": True}),
    ("state-write-outside-the-schema", payload(state_updates={"outcome": "refunded"})),
    ("state-write-of-the-wrong-type", payload(state_updates={"intent": 12345})),
    ("state-write-of-an-unknown-field", payload(state_updates={"nonexistent": "x"})),
]


@pytest.mark.parametrize(("case", "answer"), REFUSED, ids=[c[0] for c in REFUSED])
async def test_a_hostile_decision_is_refused_and_never_guessed_at(
    case: str, answer: dict[str, Any], pack: Pack, engine: AsyncEngine
) -> None:
    outcome = await drive(pack, engine, [Rule(when=CLASSIFY, respond=answer)])
    assert outcome.status == "waiting_human", case
    assert outcome.reasons == ["llm_invalid_output"], case
    # DESIGN.md 11.3: retried once with the reason stated back, then given up on. Never a third.
    assert outcome.calls == 2, case
    assert all(not (patch or {}) for patch in outcome.patches), case


async def test_a_structured_payload_that_is_not_an_object_is_rejected_earlier_still(
    pack: Pack, engine: AsyncEngine
) -> None:
    """The review's one case that never reaches the node: the wire type refuses it."""
    with pytest.raises(ValueError, match="structured"):
        CompletionResponse(model="m", structured=[1, 2])


async def test_malformed_twice_is_a_handoff_and_not_a_third_attempt(
    pack: Pack, engine: AsyncEngine
) -> None:
    outcome = await drive(
        pack, engine, [Rule(when=CLASSIFY, respond=payload(decision="refund_now"))]
    )
    assert outcome.calls == 2
    assert outcome.reasons == ["llm_invalid_output"]


async def test_malformed_then_valid_is_accepted_because_the_retry_is_real(
    pack: Pack, engine: AsyncEngine
) -> None:
    outcome = await drive(
        pack,
        engine,
        [
            Rule(when="Your previous answer was rejected", respond=payload(), uses=1),
            Rule(when=CLASSIFY, respond=payload(decision="refund_now"), uses=1),
            Rule(when=CHAT, respond=payload(decision="done", message_to_customer="Hi.")),
        ],
    )
    assert outcome.reasons == []
    assert "small_talk" in outcome.edges


async def test_low_confidence_with_no_unclear_edge_asks_for_a_human(
    pack: Pack, engine: AsyncEngine
) -> None:
    """``chat`` declares one edge, so the graph gave the model no way to be unsure."""
    outcome = await drive(
        pack,
        engine,
        [
            Rule(when=CLASSIFY, respond=payload(state_updates={"intent": "small_talk"})),
            Rule(when=CHAT, respond=payload(decision="done", confidence=0.1)),
        ],
    )
    assert outcome.status == "waiting_human"
    assert outcome.reasons == ["low_confidence"]


async def test_low_confidence_with_an_unclear_edge_routes_to_it(
    pack: Pack, engine: AsyncEngine
) -> None:
    outcome = await drive(pack, engine, [Rule(when=CLASSIFY, respond=payload(confidence=0.1))])
    assert outcome.reasons == []
    assert outcome.edges[0] == "unclear"


async def test_the_model_may_ask_for_a_human_and_the_engine_decides(
    pack: Pack, engine: AsyncEngine
) -> None:
    outcome = await drive(pack, engine, [Rule(when=CLASSIFY, respond=payload(needs_handoff=True))])
    assert outcome.status == "waiting_human"
    assert outcome.reasons == ["model_requested_handoff"]
    assert outcome.calls == 1


# -- the two the review found routed (finding V2) ----------------------------------------------

NO_SCHEMA: list[tuple[str, dict[str, Any]]] = [
    ("undeclared-field-with-no-schema", {"outcome": "refunded"}),
    ("wrong-type-with-no-schema", {"intent": 12345}),
]


@pytest.mark.parametrize(("case", "updates"), NO_SCHEMA, ids=[c[0] for c in NO_SCHEMA])
async def test_a_state_write_on_a_node_with_no_output_schema_is_refused(
    case: str, updates: dict[str, Any], pack: Pack, engine: AsyncEngine
) -> None:
    """The two cases that were ROUTED, patch applied, against the reviewed code (V2).

    ``chat`` declares no fields it may write. Under the reviewed code that meant *any* field of
    the frame state, at any type; it now means none.
    """
    outcome = await drive(
        pack,
        engine,
        [
            Rule(when=CLASSIFY, respond=payload(state_updates={"intent": "small_talk"})),
            Rule(when=CHAT, respond=payload(decision="done", state_updates=updates)),
        ],
    )
    assert outcome.status == "waiting_human", case
    assert outcome.reasons == ["llm_invalid_output"], case
    assert all(set(patch or {}) <= {"intent"} for patch in outcome.patches), case
