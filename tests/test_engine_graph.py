"""The executor walks a graph. Implements the phase-1 exit criterion under the phase-2 engine.

Phase 1's exit criterion was "a deterministic graph using only ``router``, ``say``,
``subgraph``, ``end`` executes in a unit test through a minimal in-memory stepper". The stepper
is gone; these tests make the same assertions - the same pack, the same path, the same messages,
the same outputs - against the real executor and real Postgres, so the criterion survives the
thing that replaced it.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.engine.runners import NotExecutableRunner, build_runner
from support_core.graph.pack import Pack
from tests.engine_support import (
    DETERMINISTIC_PACK,
    PACKS,
    Recorder,
    outbound_texts,
    path,
    run_row,
    trace_rows,
)


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack(DETERMINISTIC_PACK)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def executor(pack: Pack, engine: AsyncEngine, recorder: Recorder) -> Executor:
    return Executor(pack, engine, hooks=recorder.hooks())


async def _run(executor: Executor, amount: float, **kwargs: object) -> tuple[uuid.UUID, uuid.UUID]:
    conversation_id = await executor.start_conversation(inputs={"amount": amount}, **kwargs)  # type: ignore[arg-type]
    outcome = await executor.on_inbound(conversation_id, "hello")
    assert outcome.run_id is not None
    return conversation_id, outcome.run_id


async def test_high_tier_path(executor: Executor, engine: AsyncEngine) -> None:
    conversation_id = await executor.start_conversation(
        inputs={"amount": 250.0}, context={"customer": {"name": "Ada"}}
    )
    outcome = await executor.on_inbound(conversation_id, "hello")

    assert outcome.run_id is not None
    assert await path(engine, outcome.run_id) == [
        "greet",
        "classify",
        "decide",
        "high",
        "tier_router",
        "escalate",
        "done",
    ]
    assert await outbound_texts(engine, conversation_id) == [
        "Hello Ada, that charge is 250.00.",
        "A specialist will look at this personally.",
    ]
    assert outcome.status == "done"


async def test_low_tier_path_and_the_default_template_value(
    executor: Executor, engine: AsyncEngine
) -> None:
    conversation_id, run_id = await _run(executor, 20.0)
    assert (await path(engine, run_id))[-2:] == ["settle", "done"]
    greeting = (await outbound_texts(engine, conversation_id))[0]
    assert greeting == "Hello there, that charge is 20.00."


async def test_sub_graph_outputs_land_in_the_caller_state(
    executor: Executor, engine: AsyncEngine
) -> None:
    _, run_id = await _run(executor, 250.0)
    steps = await trace_rows(engine, run_id)
    tier_router = next(step for step in steps if step["node_id"] == "tier_router")
    assert tier_router["edge"] == "state.tier == 'high'"


async def test_the_run_is_deterministic(pack: Pack, engine: AsyncEngine) -> None:
    """Two conversations of the same pack produce the same path, edges and messages."""
    first_executor = Executor(pack, engine, hooks=Recorder().hooks())
    second_executor = Executor(pack, engine, hooks=Recorder().hooks())
    first_conversation, first_run = await _run(first_executor, 101.0)
    second_conversation, second_run = await _run(second_executor, 101.0)

    def shape(rows: list[dict[str, object]]) -> list[tuple[object, object, object]]:
        return [(row["seq"], row["node_id"], row["edge"]) for row in rows]

    assert shape(await trace_rows(engine, first_run)) == shape(await trace_rows(engine, second_run))
    assert await outbound_texts(engine, first_conversation) == await outbound_texts(
        engine, second_conversation
    )


async def test_a_router_without_a_matching_branch_hands_off(
    engine: AsyncEngine, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The engine refuses to guess. DESIGN.md section 7.3 routes a node failure to a handoff."""
    import shutil

    target = tmp_path_factory.mktemp("packs") / "pack"
    shutil.copytree(DETERMINISTIC_PACK, target)
    tier = target / "graphs" / "tier.yaml"
    tier.write_text(
        tier.read_text(encoding="utf-8").replace("    default: low\n", ""), encoding="utf-8"
    )
    recorder = Recorder()
    executor = Executor(load_pack(target), engine, hooks=recorder.hooks())
    conversation_id, run_id = await _run(executor, 1.0)

    assert [request.reason for request in recorder.handoffs] == ["node_error"]
    assert "no router branch matched" in (recorder.handoffs[0].detail or "")
    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human"
    assert row["awaiting"]["reason"] == "node_error"
    failed = (await trace_rows(engine, run_id))[-1]
    assert failed["node_id"] == "decide"
    assert "node_error" in failed["error"]


async def test_the_engine_refuses_node_types_core_cannot_run_yet() -> None:
    """Core does not pretend to run a node type it has not implemented.

    ``llm`` became executable in phase 3 and ``tool`` and ``confirm`` in phase 4, so ``handoff``
    is the last one left; the refusal is asserted where it lives, in the runner registry. The
    standing rule about tools is no longer about *whether* a tool can run - it can - and is
    asserted in ``tests/test_tool_policy.py`` and ``tests/test_adversarial_approvals.py``.
    """
    pack = load_pack(PACKS / "refund_pack")
    graph = pack.graphs["refund"]
    runner = build_runner("handoff_dispute", graph.nodes["handoff_dispute"])
    assert isinstance(runner, NotExecutableRunner)
    assert runner.phase == 6
    for node_id in ("fetch_charge", "confirm_refund"):
        assert not isinstance(build_runner(node_id, graph.nodes[node_id]), NotExecutableRunner)


async def test_an_llm_node_without_a_provider_hands_off_rather_than_guessing(
    engine: AsyncEngine,
) -> None:
    """An executor with no LLM layer must not silently walk past a prompted node.

    DESIGN.md section 7.3 has no "carry on regardless" rung: a node that cannot do its job is a
    node error, and a node error with nowhere to route is a handoff.
    """
    recorder = Recorder()
    executor = Executor(load_pack(PACKS / "llm_pack"), engine, hooks=recorder.hooks())
    conversation_id = await executor.start_conversation()
    outcome = await executor.on_inbound(conversation_id, "hello")

    assert outcome.status == "waiting_human"
    assert [request.reason for request in recorder.handoffs] == ["llm_unavailable"]
    assert "needs an LLM provider" in (recorder.handoffs[0].detail or "")


async def test_a_runaway_graph_hits_the_per_turn_limit(
    engine: AsyncEngine, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """DESIGN.md section 7.3: limits hit means handoff with reason ``limit_exceeded``."""
    import shutil

    target = tmp_path_factory.mktemp("packs") / "pack"
    shutil.copytree(DETERMINISTIC_PACK, target)
    manifest = target / "pack.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "limits:\n  max_nodes_per_turn: 3\n",
        encoding="utf-8",
    )
    recorder = Recorder()
    executor = Executor(load_pack(target), engine, hooks=recorder.hooks())
    conversation_id, _ = await _run(executor, 250.0)

    assert [request.reason for request in recorder.handoffs] == ["limit_exceeded"]
    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human"
    assert row["turn_nodes"] == 4  # three nodes, then the step that records the refusal


async def test_an_output_mapping_the_caller_no_longer_declares_is_a_node_error(
    engine: AsyncEngine, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Independent review finding R12.

    The validator refuses a ``subgraph`` node whose ``outputs`` name a state field the caller
    does not declare, so the only way to reach one at run time is a pack redeployed while a run
    is suspended inside the callee: the *frame* carries the old mapping. Writing the value in
    regardless makes the caller's own state model reject it one node later, which is reported to
    the operator as ``pack_incompatible`` - a diagnosis that sends them looking at the stored
    state instead of at the mapping that is actually wrong.
    """
    import shutil

    from tests.engine_support import custom_node_types, set_context

    with custom_node_types():
        before = tmp_path_factory.mktemp("packs") / "before"
        shutil.copytree(PACKS / "custom_pack", before)
        context = {"customer": {"identity_verified": True, "attributes": {"mode": "gate"}}}
        executor = Executor(load_pack(before), engine, hooks=Recorder().hooks())
        conversation_id = await executor.start_conversation(context=context)
        assert (await executor.on_inbound(conversation_id, "go")).status == "waiting_customer"

        # Redeployed with the caller's state field renamed. The new pack is valid; it is the
        # frame already suspended inside the callee that still maps into the old name.
        after = tmp_path_factory.mktemp("packs") / "after"
        shutil.copytree(PACKS / "custom_pack", after)
        root = after / "graphs" / "root.yaml"
        root.write_text(
            root.read_text(encoding="utf-8")
            .replace("  reached: bool | None", "  arrived: bool | None")
            .replace("outputs: { reached: reached }", "outputs: { arrived: reached }"),
            encoding="utf-8",
        )
        await set_context(engine, conversation_id, context)
        recorder = Recorder()
        upgraded = Executor(load_pack(after), engine, hooks=recorder.hooks())
        outcome = await upgraded.on_inbound(conversation_id, "42")

    assert outcome.status == "waiting_human"
    assert [request.reason for request in recorder.handoffs] == ["node_error"]
    detail = recorder.handoffs[0].detail or ""
    assert "['reached']" in detail and "root does not declare" in detail
