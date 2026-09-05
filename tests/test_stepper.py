"""The phase-1 exit criterion (BACKLOG.md).

"A deterministic graph using only ``router``, ``say``, ``subgraph``, ``end`` executes in a unit
test through a minimal in-memory stepper." The stepper is ``tests/stepper.py``, a test utility;
phase 2 owns the real executor.
"""

import shutil
from pathlib import Path

import pytest

from support_core import load_pack
from support_core.graph.context import ConversationContext, CustomerContext
from support_core.graph.pack import Pack
from tests import stepper
from tests.conftest import REPO_ROOT

DETERMINISTIC_PACK = REPO_ROOT / "tests" / "packs" / "deterministic_pack"
REFUND_PACK = REPO_ROOT / "tests" / "packs" / "refund_pack"


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack(DETERMINISTIC_PACK)


def test_high_tier_path(pack: Pack) -> None:
    ctx = ConversationContext(customer=CustomerContext(name="Ada"))
    result = stepper.run(pack, ctx=ctx, inputs={"amount": 250.0})

    assert result.path == [
        ("root", "greet"),
        ("root", "classify"),
        ("tier", "decide"),
        ("tier", "high"),
        ("root", "tier_router"),
        ("root", "escalate"),
        ("root", "done"),
    ]
    assert result.messages == [
        "Hello Ada, that charge is 250.00.",
        "A specialist will look at this personally.",
    ]
    assert result.outputs == {"tier": "high"}


def test_low_tier_path_and_the_default_template_value(pack: Pack) -> None:
    result = stepper.run(pack, inputs={"amount": 20.0})
    assert result.path[-2:] == [("root", "settle"), ("root", "done")]
    assert result.messages[0] == "Hello there, that charge is 20.00."
    assert result.outputs == {"tier": "low"}


def test_the_run_is_deterministic(pack: Pack) -> None:
    first = stepper.run(pack, inputs={"amount": 101.0})
    second = stepper.run(pack, inputs={"amount": 101.0})
    assert first == second


def test_sub_graph_outputs_land_in_the_caller_state(pack: Pack) -> None:
    result = stepper.run(pack, inputs={"amount": 250.0})
    assert result.final_state["tier"] == "high"
    assert result.final_state["amount"] == 250.0


def test_a_router_without_a_matching_branch_and_no_default_stops(tmp_path: Path) -> None:
    """The stepper refuses to guess; the validator warns about this shape in advance."""
    target = tmp_path / "pack"
    shutil.copytree(DETERMINISTIC_PACK, target)
    tier = target / "graphs" / "tier.yaml"
    tier.write_text(
        tier.read_text(encoding="utf-8").replace("    default: low\n", ""), encoding="utf-8"
    )
    broken = load_pack(target)
    with pytest.raises(stepper.StepperError, match="no router branch matched"):
        stepper.run(broken, inputs={"amount": 1.0})


def test_the_stepper_refuses_node_types_core_cannot_run_yet() -> None:
    """PLAN.md's standing rule: no side effect without an ActionApproval, and phase 1 has none."""
    pack = load_pack(REFUND_PACK)
    with pytest.raises(stepper.NotExecutableError) as exc:
        stepper.run(pack)
    assert "'llm' is not executable until phase 3" in str(exc.value)


def test_a_runaway_graph_hits_the_step_limit(pack: Pack) -> None:
    with pytest.raises(stepper.StepperError, match="more than 2 steps"):
        stepper.run(pack, inputs={"amount": 1.0}, max_steps=2)
