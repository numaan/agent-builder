"""What an ``llm`` node's answer may write into the workflow state. Phase 3 finding V2.

DESIGN.md section 11.3 says ``state_updates`` is "validated against node ``output_schema``". The
reviewed code validated it against the schema only when the node *had* one: with no schema,
``declared = set(self.node.output_schema) or set(values)`` made "whatever the model wrote" the
declaration, and ``build_node_output_model`` left ``state_updates`` as a free-form
``dict[str, Any]``. The review drove the shipped ``packs/acme_billing`` ``chat`` node - which
declares no schema - and the model wrote the graph's own ``outcome`` field, and an ``intent`` of
the wrong type, and both were checkpointed.

These tests are that scenario, on that pack, unchanged. Absence of a schema now means the model
may write *nothing*, which is enforced twice: in the structured schema the provider is given and
answers into, and again in the runner.
"""

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.graph.pack import Pack
from support_core.llm.fake import Rule, ScriptedProvider
from support_core.llm.schemas import build_node_output_model
from support_core.llm.wiring import StructuredSlotExtractor, service_for_pack
from tests.engine_support import Recorder, outbound_texts, run_row, trace_rows

ACME = Path(__file__).resolve().parents[1] / "packs" / "acme_billing"

CLASSIFY = "Read the customer's most recent message"
CHAT = "Reply to the customer's small talk"


def decision(label: str, **updates: Any) -> dict[str, Any]:
    return {
        "message_to_customer": None,
        "decision": label,
        "state_updates": dict(updates),
        "citations": [],
        "confidence": 0.9,
        "needs_handoff": False,
    }


async def _no_sleep(seconds: float) -> None:
    pass


@pytest.fixture
def acme() -> Pack:
    return load_pack(ACME)


def build(pack: Pack, engine: AsyncEngine, rules: list[Rule]) -> tuple[Executor, Recorder]:
    provider = ScriptedProvider(rules)
    service = service_for_pack(pack, provider, sleep=_no_sleep)
    recorder = Recorder()
    hooks = dataclasses.replace(recorder.hooks(), extract_slots=StructuredSlotExtractor(service))
    return Executor(pack, engine, hooks=hooks, llm=service, tool_runner=None), recorder


async def test_a_node_with_no_output_schema_cannot_write_the_graphs_own_outcome(
    acme: Pack, engine: AsyncEngine
) -> None:
    """The review's exact reproduction, on the shipped pack.

    ``chat`` declares no ``output_schema``. A model driving it writes ``outcome: "refunded"`` -
    a field a later router or gate would read as established fact. It must not reach the
    checkpoint, and the turn must end with a human rather than with a decision the graph never
    authorised.
    """
    executor, recorder = build(
        acme,
        engine,
        [
            Rule(when=CLASSIFY, respond=decision("small_talk", intent="small_talk")),
            Rule(when=CHAT, respond=decision("done", outcome="refunded")),
        ],
    )
    conversation_id = await executor.start_conversation()
    outcome = await executor.on_inbound(conversation_id, "hello there")

    assert outcome.status == "waiting_human"
    assert [request.reason for request in recorder.handoffs] == ["llm_invalid_output"]
    row = await run_row(engine, conversation_id)
    patches = [step["state_patch"] for step in await trace_rows(engine, row["id"])]
    assert all("outcome" not in (patch or {}) for patch in patches), patches
    assert await outbound_texts(engine, conversation_id) == []


async def test_a_node_with_no_output_schema_cannot_write_an_ill_typed_field(
    acme: Pack, engine: AsyncEngine
) -> None:
    """The review's second case: ``intent: 12345`` on a node that declares no schema.

    It used to be accepted into the patch and to surface two nodes later as an
    ``IncompatiblePackError`` blaming the pack version.
    """
    executor, recorder = build(
        acme,
        engine,
        [
            Rule(when=CLASSIFY, respond=decision("small_talk", intent="small_talk")),
            Rule(when=CHAT, respond=decision("done", intent=12345)),
        ],
    )
    conversation_id = await executor.start_conversation()
    outcome = await executor.on_inbound(conversation_id, "hello there")

    assert outcome.status == "waiting_human"
    assert [request.reason for request in recorder.handoffs] == ["llm_invalid_output"]


async def test_a_node_with_a_declared_schema_still_writes_what_it_declared(
    acme: Pack, engine: AsyncEngine
) -> None:
    """The other half of the property: declaring a schema still means what it meant."""
    executor, recorder = build(
        acme,
        engine,
        [
            Rule(when=CLASSIFY, respond=decision("small_talk", intent="small_talk")),
            Rule(when=CHAT, respond=decision("done")),
        ],
    )
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "hello there")
    row = await run_row(engine, conversation_id)
    patches = {step["node_id"]: step["state_patch"] for step in await trace_rows(engine, row["id"])}
    assert patches["classify"] == {"intent": "small_talk"}
    assert recorder.handoffs == []


def test_the_schema_the_provider_is_given_closes_state_updates_when_none_is_declared() -> None:
    """Enforced in the schema, not only in the runner.

    A provider that honours the schema cannot be talked into offering the field at all, which is
    the difference between refusing a write and never inviting one.
    """
    model = build_node_output_model("chat", ["done"], None)
    updates = model.model_fields["state_updates"].annotation
    assert updates is not None
    assert not getattr(updates, "model_fields", {"x": 1}), "state_updates should have no fields"
    with pytest.raises(ValueError, match="outcome"):
        model.model_validate({"decision": "done", "state_updates": {"outcome": "refunded"}})
