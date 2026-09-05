"""``load_pack`` and the graph version pin (DESIGN.md sections 5.2 and 6.7)."""

import shutil
from pathlib import Path

import pytest

from support_core import load_pack
from support_core.graph.loader import PackValidationError, load_pack_report
from support_core.graph.nodes import (
    ConfirmNode,
    EndNode,
    GateNode,
    RouterNode,
    SubgraphNode,
    ToolNode,
)
from support_core.graph.pack import state_shape_hash
from support_core.tools.risk import Risk
from tests.conftest import REPO_ROOT

REFUND_PACK = REPO_ROOT / "tests" / "packs" / "refund_pack"


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    target = tmp_path / "refund_pack"
    shutil.copytree(REFUND_PACK, target)
    return target


def test_load_pack_resolves_everything() -> None:
    pack = load_pack(REFUND_PACK)
    assert pack.id == "refund-pack"
    assert set(pack.graphs) == {"root", "refund", "verify_identity"}
    assert pack.entry_graph.id == "root"
    assert pack.persona.startswith("# Persona")
    assert "Never state a refund" in pack.policies
    assert pack.tools.present
    issue_refund = pack.tools.get("issue_refund")
    assert issue_refund is not None
    assert issue_refund.risk is Risk.HIGH


def test_load_pack_round_trips_the_design_example() -> None:
    """Every node of the DESIGN.md section 6.4 refund graph survives the parse with its fields."""
    refund = load_pack(REFUND_PACK).graphs["refund"]
    assert refund.start == "identity_gate"
    assert refund.description is not None

    gate = refund.nodes["identity_gate"]
    assert isinstance(gate, GateNode)
    assert (gate.predicate, gate.redirect, gate.next) == (
        "ctx.customer.identity_verified",
        "verify_identity",
        "find_charge",
    )

    fetch = refund.nodes["fetch_charge"]
    assert isinstance(fetch, ToolNode)
    assert fetch.tool == "get_charge"
    assert fetch.args == {"charge_id": "state.charge_id"}
    assert fetch.into == "state.charge"
    assert fetch.on_error == "handoff_lookup_failed"

    router = refund.nodes["eligibility_router"]
    assert isinstance(router, RouterNode)
    assert router.edges["state.eligible == true"] == "confirm_refund"

    confirm = refund.nodes["confirm_refund"]
    assert isinstance(confirm, ConfirmNode)
    assert confirm.action.tool == "issue_refund"
    assert confirm.edges == {"yes": "issue_refund", "no": "abandon"}

    issue = refund.nodes["issue_refund"]
    assert isinstance(issue, ToolNode)
    assert issue.requires_approval == "confirm_refund"
    assert issue.into == {"outcome": "refunded"}

    abandon = refund.nodes["abandon"]
    assert isinstance(abandon, EndNode)
    assert abandon.outputs == {"outcome": "abandoned"}

    root = load_pack(REFUND_PACK).graphs["root"]
    call = root.nodes["do_refund"]
    assert isinstance(call, SubgraphNode)
    assert call.graph == "refund"
    assert call.outputs == {"refund_outcome": "outcome"}


def test_state_models_are_usable() -> None:
    refund = load_pack(REFUND_PACK).graphs["refund"]
    state = refund.state.model()
    assert state.model_dump()["charge_id"] is None
    state = refund.state.model(charge_id="ch_1")
    assert state.model_dump()["charge_id"] == "ch_1"


def test_load_pack_raises_with_the_whole_report(pack_copy: Path) -> None:
    (pack_copy / "graphs" / "refund.yaml").write_text(
        (pack_copy / "graphs" / "refund.yaml")
        .read_text(encoding="utf-8")
        .replace("next: find_charge", "next: no_such_node"),
        encoding="utf-8",
    )
    with pytest.raises(PackValidationError) as exc:
        load_pack(pack_copy)
    assert "graph.edge_target_missing" in str(exc.value)
    assert exc.value.report.errors


def test_load_pack_report_does_not_raise(pack_copy: Path) -> None:
    (pack_copy / "graphs" / "refund.yaml").unlink()
    pack, report = load_pack_report(pack_copy)
    assert pack is None
    assert not report.ok


# --------------------------------------------------------------------------------------
# DESIGN.md 6.7: graph version pinning
# --------------------------------------------------------------------------------------


def test_pin_records_every_graph() -> None:
    pin = load_pack(REFUND_PACK).pin
    assert pin.pack_id == "refund-pack"
    assert pin.pack_version == "0.1.0"
    assert set(pin.graphs) == {"root", "refund", "verify_identity"}
    assert pin.graph("refund") is not None
    assert pin.graph("ghost") is None


def test_pin_is_deterministic() -> None:
    assert load_pack(REFUND_PACK).pin == load_pack(REFUND_PACK).pin
    assert load_pack(REFUND_PACK).pin.fingerprint == load_pack(REFUND_PACK).pin.fingerprint


def test_a_comment_change_changes_the_pin_but_not_the_state_shape(pack_copy: Path) -> None:
    before = load_pack(pack_copy).pin
    path = pack_copy / "graphs" / "refund.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\n# a new comment\n", encoding="utf-8")
    after = load_pack(pack_copy).pin

    assert after.fingerprint != before.fingerprint
    assert after.changed_graphs(before) == {"refund"}
    assert after.state_compatible(before, "refund")
    assert after.state_compatible(before, "root")


def test_a_state_change_makes_the_pins_state_incompatible(pack_copy: Path) -> None:
    path = pack_copy / "graphs" / "refund.yaml"
    before = load_pack(pack_copy).pin
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  denial_reason: str | None", "  denial_reason: str | None\n  extra: str | None"
        ),
        encoding="utf-8",
    )
    after = load_pack(pack_copy).pin
    assert not after.state_compatible(before, "refund")
    assert after.state_compatible(before, "root")


def test_a_removed_graph_is_not_state_compatible() -> None:
    pin = load_pack(REFUND_PACK).pin
    trimmed = pin.model_copy(
        update={"graphs": {k: v for k, v in pin.graphs.items() if k != "refund"}}
    )
    assert not trimmed.state_compatible(pin, "refund")
    assert trimmed.changed_graphs(pin) == {"refund"}


def test_state_shape_hash_ignores_declaration_order() -> None:
    assert state_shape_hash({"a": "str | None", "b": "int"}) == state_shape_hash(
        {"b": "int", "a": "str | None"}
    )
    assert state_shape_hash({"a": "str | None"}) != state_shape_hash({"a": "str"})
