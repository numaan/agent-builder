"""One failing fixture per validator rule (DESIGN.md section 5.2, section 8.2).

Each case is a complete, minimal set of graph files plus the shared tool manifest below. The
assertion is on the stable rule id, so a rename is a deliberate, visible break.

The reference pack ``tests/packs/refund_pack`` (DESIGN.md's own section 6.4 refund workflow) is
the positive case: it must validate with no errors.
"""

from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent

import pytest
from click.testing import CliRunner

from support_core.cli.main import EXIT_INVALID, EXIT_OK, cli
from support_core.graph.findings import Finding, Severity
from support_core.graph.manifest import PackManifest
from support_core.graph.rules import validate_graph_set
from support_core.graph.schema import read_graphs
from support_core.graph.tools_manifest import load_tool_manifest
from support_core.graph.validator import validate_pack
from tests.conftest import REPO_ROOT

REFUND_PACK = REPO_ROOT / "tests" / "packs" / "refund_pack"
DETERMINISTIC_PACK = REPO_ROOT / "tests" / "packs" / "deterministic_pack"

TOOLS_YAML = """
tools:
  - name: read_tool
    description: Read something.
    risk: read
    input: { charge_id: str }
    output: { amount: float, label: "str | None" }
  - name: write_tool
    description: Change something reversible.
    risk: write
    input: { charge_id: str }
    output: { ok: bool }
  - name: high_tool
    description: Move money.
    risk: high
    input: { charge_id: str, amount: float }
    output: { receipt: str }
  - name: exempt_tool
    description: Send a one-time passcode.
    risk: write
    confirm_exempt: true
    input: { charge_id: str }
    output: { sent: bool }
"""

MANIFEST = PackManifest.model_validate(
    {
        "id": "fixture",
        "version": "1.0.0",
        "core": ">=0.0.1,<1",
        "entry_graph": "main",
        "channels": ["web_chat"],
        "llm": {"default_model": "claude-sonnet-5"},
        "handoff": {"queue": "q"},
    }
)


@pytest.fixture
def pack_dir(tmp_path: Path) -> Iterator[Path]:
    """A directory holding only ``tools/tools.yaml`` and ``graphs/``; enough for the rules."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "tools.yaml").write_text(TOOLS_YAML, encoding="utf-8")
    (tmp_path / "graphs").mkdir()
    yield tmp_path


def check(pack_dir: Path, graphs: dict[str, str]) -> list[Finding]:
    for name, text in graphs.items():
        (pack_dir / "graphs" / f"{name}.yaml").write_text(dedent(text), encoding="utf-8")
    files = sorted(f"graphs/{name}.yaml" for name in graphs)
    parsed, findings = read_graphs(pack_dir, files)
    tools = load_tool_manifest(pack_dir)
    return findings + validate_graph_set(parsed, tools, MANIFEST)


def rules(pack_dir: Path, graphs: dict[str, str], severity: Severity | None = None) -> set[str]:
    return {f.rule for f in check(pack_dir, graphs) if severity is None or f.severity is severity}


# --------------------------------------------------------------------------------------
# Graph sources used by the cases. ``MAIN`` is well-formed; each case breaks one thing.
# --------------------------------------------------------------------------------------

MAIN = """
id: main
state:
  charge_id: str | None
  ok: bool | None
start: pick
nodes:
  pick:
    type: router
    edges:
      state.charge_id != none: finish
    default: finish
  finish: { type: end }
"""


def main(**overrides: str) -> str:
    """The well-formed graph with one section replaced, so each fixture shows only its defect."""
    text = MAIN
    for marker, replacement in overrides.items():
        text = text.replace(marker.replace("__", " "), replacement)
    return text


CASES: list[tuple[str, dict[str, str]]] = [
    # -- file and schema level ---------------------------------------------------------
    ("graph.invalid_yaml", {"main": "id: main\n  bad: [indent\n"}),
    ("graph.invalid", {"main": "id: main\nstart: a\n"}),
    ("graph.id_mismatch", {"main": MAIN.replace("id: main", "id: other")}),
    (
        "graph.node_type_unknown",
        {"main": MAIN.replace("  finish: { type: end }", "  finish: { type: teleport }")},
    ),
    (
        "graph.node_invalid",
        {"main": MAIN.replace("  finish: { type: end }", "  finish: { type: end, oops: 1 }")},
    ),
    (
        "graph.node_id_invalid",
        {"main": MAIN.replace("  finish: { type: end }", "  Finish!: { type: end }")},
    ),
    ("graph.start_missing", {"main": MAIN.replace("start: pick", "start: nowhere")}),
    (
        "graph.no_end",
        {
            "main": """
id: main
start: a
nodes:
  a: { type: say, message: hi, next: b }
  b: { type: say, message: bye, next: a }
"""
        },
    ),
    (
        "graph.edge_target_missing",
        {"main": MAIN.replace("default: finish", "default: nowhere")},
    ),
    (
        "graph.node_unreachable",
        {
            "main": MAIN.replace(
                "  finish: { type: end }",
                "  finish: { type: end }\n  orphan: { type: say, message: hi, next: finish }",
            )
        },
    ),
    ("graph.router_no_default", {"main": MAIN.replace("    default: finish\n", "")}),
    (
        "graph.router_predicate_not_bool",
        {"main": MAIN.replace("state.charge_id != none: finish", "state.charge_id: finish")},
    ),
    # -- declarations ------------------------------------------------------------------
    (
        "graph.state_type_invalid",
        {"main": MAIN.replace("charge_id: str | None", "charge_id: list[str, int]")},
    ),
    (
        "graph.state_type_unresolved",
        {"main": MAIN.replace("charge_id: str | None", "charge_id: Charge | None")},
    ),
    (
        "graph.state_field_invalid",
        {"main": MAIN.replace("  charge_id: str | None", "  model_id: str | None")},
    ),
    (
        "graph.state_field_not_optional",
        {"main": MAIN.replace("charge_id: str | None", "charge_id: str")},
    ),
    (
        "graph.input_not_in_state",
        {"main": MAIN.replace("state:", "inputs:\n  hint: str | None\nstate:")},
    ),
    # -- expressions and templates -----------------------------------------------------
    (
        "expr.parse_error",
        {"main": MAIN.replace("state.charge_id != none: finish", "state.charge_id ==: finish")},
    ),
    (
        "expr.type_error",
        {"main": MAIN.replace("state.charge_id != none: finish", "state.nope == 1: finish")},
    ),
    (
        "expr.looks_like_expression",
        {
            "main": """
id: main
outputs:
  outcome: str
state:
  charge_id: str | None
start: finish
nodes:
  finish: { type: end, outputs: { outcome: stat.charge_id } }
"""
        },
    ),
    (
        "expr.optional_attribute",
        {
            "main": """
id: main
state:
  charge_id: str | None
start: say_it
nodes:
  say_it: { type: say, message: "{{ ctx.summary.length }}", next: finish }
  finish: { type: end }
"""
        },
    ),
    (
        "template.syntax_error",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: say_it
nodes:
  say_it: { type: say, message: "{{ oops", next: finish }
  finish: { type: end }
"""
        },
    ),
    (
        "template.unsupported",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: say_it
nodes:
  say_it:
    type: say
    message: "{% for c in state %}{{ c }}{% endfor %}"
    next: finish
  finish: { type: end }
"""
        },
    ),
    (
        "template.type",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: say_it
nodes:
  say_it: { type: say, message: "{{ state.chrage_id }}", next: finish }
  finish: { type: end }
"""
        },
    ),
    # -- tools -------------------------------------------------------------------------
    (
        "graph.tool_unknown",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: call
nodes:
  call: { type: tool, tool: nonexistent, args: {}, next: finish }
  finish: { type: end }
"""
        },
    ),
    (
        "graph.tool_arg_unknown",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: call
nodes:
  call:
    type: tool
    tool: read_tool
    args: { charge_id: state.charge_id, extra: "x" }
    next: finish
  finish: { type: end }
"""
        },
    ),
    (
        "graph.tool_arg_missing",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: call
nodes:
  call: { type: tool, tool: read_tool, args: {}, next: finish }
  finish: { type: end }
"""
        },
    ),
    (
        "graph.tool_into_invalid",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: call
nodes:
  call:
    type: tool
    tool: read_tool
    args: { charge_id: state.charge_id }
    into: { nowhere: result.amount }
    next: finish
  finish: { type: end }
"""
        },
    ),
    (
        "graph.llm_tool_unknown",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: think
nodes:
  think:
    type: llm
    instructions: think
    tools: [nonexistent]
    edges: { done: finish }
  finish: { type: end }
"""
        },
    ),
    (
        "graph.llm_tool_not_read",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: think
nodes:
  think:
    type: llm
    instructions: think
    tools: [high_tool]
    edges: { done: finish }
  finish: { type: end }
"""
        },
    ),
    (
        "graph.llm_output_schema_invalid",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: think
nodes:
  think:
    type: llm
    instructions: think
    output_schema: { charge_id: "dict[str]" }
    edges: { done: finish }
  finish: { type: end }
"""
        },
    ),
    (
        "graph.llm_output_not_in_state",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: think
nodes:
  think:
    type: llm
    instructions: think
    output_schema: { unknown_slot: "str | None" }
    edges: { done: finish }
  finish: { type: end }
"""
        },
    ),
    (
        "graph.ask_slot_unknown",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: ask_it
nodes:
  ask_it: { type: ask, slots: [nope], prompt: "which?", next: finish }
  finish: { type: end }
"""
        },
    ),
    # -- gates and sub-graphs ----------------------------------------------------------
    (
        "graph.gate_redirect_unknown",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: gate_it
nodes:
  gate_it:
    type: gate
    predicate: ctx.customer.identity_verified
    redirect: no_such_graph
    next: finish
  finish: { type: end }
"""
        },
    ),
    (
        "graph.gate_predicate_not_bool",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: gate_it
nodes:
  gate_it:
    type: gate
    predicate: ctx.customer.name
    redirect: main
    next: finish
  finish: { type: end }
"""
        },
    ),
    (
        "graph.subgraph_unknown",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: call
nodes:
  call: { type: subgraph, graph: missing, next: finish }
  finish: { type: end }
"""
        },
    ),
]


@pytest.mark.parametrize(("rule", "graphs"), CASES, ids=[case[0] for case in CASES])
def test_rule_fires(pack_dir: Path, rule: str, graphs: dict[str, str]) -> None:
    assert rule in rules(pack_dir, graphs)


# --------------------------------------------------------------------------------------
# Sub-graph mapping rules need two graphs, so they get their own cases.
# --------------------------------------------------------------------------------------

CALLEE = """
id: helper
inputs:
  charge_id: str
outputs:
  verified: bool
state:
  charge_id: str | None
  verified: bool | None
start: finish
nodes:
  finish: { type: end, outputs: { verified: true } }
"""


def caller(inputs: str, outputs: str) -> str:
    return f"""
id: main
state:
  charge_id: str | None
  flag: bool | None
  label: str | None
start: call
nodes:
  call:
    type: subgraph
    graph: helper
    inputs: {inputs}
    outputs: {outputs}
    next: finish
  finish: {{ type: end }}
"""


SUBGRAPH_CASES = [
    ("graph.subgraph_input_unknown", "{ charge_id: state.charge_id, nope: 'x' }", "{}"),
    ("graph.subgraph_input_missing", "{}", "{}"),
    (
        "graph.subgraph_output_unknown",
        "{ charge_id: state.charge_id }",
        "{ flag: not_an_output }",
    ),
    (
        "graph.subgraph_output_field_unknown",
        "{ charge_id: state.charge_id }",
        "{ nowhere: verified }",
    ),
    (
        "graph.subgraph_output_type",
        "{ charge_id: state.charge_id }",
        "{ label: verified }",
    ),
]


@pytest.mark.parametrize(
    ("rule", "inputs", "outputs"), SUBGRAPH_CASES, ids=[c[0] for c in SUBGRAPH_CASES]
)
def test_subgraph_mapping_rules(pack_dir: Path, rule: str, inputs: str, outputs: str) -> None:
    assert rule in rules(pack_dir, {"main": caller(inputs, outputs), "helper": CALLEE})


def test_duplicate_graph_id(pack_dir: Path) -> None:
    assert "graph.duplicate_id" in rules(
        pack_dir, {"main": MAIN, "copy": MAIN.replace("id: main", "id: main")}
    )


def test_unreadable_graph_file(pack_dir: Path) -> None:
    (pack_dir / "graphs" / "main.yaml").write_bytes(b"id: main\n\xff\xfe\n")
    parsed, findings = read_graphs(pack_dir, ["graphs/main.yaml"])
    assert parsed == {}
    assert {f.rule for f in findings} == {"graph.unreadable"}


def test_manifest_interrupt_graph_unknown(pack_dir: Path) -> None:
    manifest = MANIFEST.model_copy(
        update={"interrupts": MANIFEST.interrupts.model_copy(update={"allowed_from": ["ghost"]})}
    )
    (pack_dir / "graphs" / "main.yaml").write_text(MAIN, encoding="utf-8")
    parsed, _ = read_graphs(pack_dir, ["graphs/main.yaml"])
    found = validate_graph_set(parsed, load_tool_manifest(pack_dir), manifest)
    assert "manifest.interrupt_graph_unknown" in {f.rule for f in found}


def test_invalid_tool_manifest_is_reported(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "tools.yaml").write_text("tools: [ {name: 'X!', risk: read} ]", "utf-8")
    (tmp_path / "graphs").mkdir()
    (tmp_path / "graphs" / "main.yaml").write_text(MAIN, encoding="utf-8")
    report = validate_pack(tmp_path)
    assert "tools.manifest_invalid" in {f.rule for f in report.errors}


def test_unresolved_tool_declaration_is_a_warning(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "tools.yaml").write_text(
        "tools:\n  - name: t\n    description: d\n    risk: read\n    output: { c: Charge }\n",
        encoding="utf-8",
    )
    (tmp_path / "graphs").mkdir()
    parsed, _ = read_graphs(tmp_path, [])
    found = validate_graph_set(parsed, load_tool_manifest(tmp_path), MANIFEST)
    assert "tools.declaration_unresolved" in {f.rule for f in found}


# --------------------------------------------------------------------------------------
# Cycles
# --------------------------------------------------------------------------------------


def test_unsuspended_cycle_is_an_error(pack_dir: Path) -> None:
    graph = """
id: main
state: { charge_id: str | None }
start: a
nodes:
  a: { type: say, message: one, next: b }
  b: { type: say, message: two, next: a }
  c: { type: end }
"""
    assert "graph.unsuspended_cycle" in rules(pack_dir, {"main": graph})


def test_a_loop_through_an_ask_node_is_allowed(pack_dir: Path) -> None:
    """DESIGN.md 5.2 forbids loops with no suspending node, not loops as such."""
    graph = """
id: main
state: { charge_id: str | None }
start: a
nodes:
  a: { type: say, message: one, next: b }
  b: { type: ask, slots: [charge_id], prompt: "which?", next: a }
  c: { type: end }
"""
    assert "graph.unsuspended_cycle" not in rules(pack_dir, {"main": graph})


def test_subgraph_cycle_without_a_suspending_node(pack_dir: Path) -> None:
    a = """
id: main
state: { charge_id: str | None }
start: call
nodes:
  call: { type: subgraph, graph: other, next: finish }
  finish: { type: end }
"""
    b = """
id: other
state: { charge_id: str | None }
start: call
nodes:
  call: { type: subgraph, graph: main, next: finish }
  finish: { type: end }
"""
    assert "graph.subgraph_cycle" in rules(pack_dir, {"main": a, "other": b})


# --------------------------------------------------------------------------------------
# The confirm rule (DESIGN.md 5.2 and 8.2): the point of the phase.
# --------------------------------------------------------------------------------------


CONFIRMED = """
id: main
state:
  charge_id: str | None
  amount: float | None
  outcome: str | None
start: ask_which
nodes:
  ask_which:
    type: ask
    slots: [charge_id]
    prompt: "which charge?"
    next: confirm_it
  confirm_it:
    type: confirm
    action:
      tool: high_tool
      args: { charge_id: state.charge_id, amount: state.amount }
    prompt: "Refund it?"
    edges: { "yes": do_it, "no": finish }
  do_it:
    type: tool
    tool: high_tool
    args: { charge_id: state.charge_id, amount: state.amount }
    requires_approval: confirm_it
    next: finish
  finish: { type: end }
"""
"""The shape DESIGN.md section 8.2 prescribes: ask, then confirm, then the bound call."""


def confirmed(old: str = "", new: str = "") -> str:
    """:data:`CONFIRMED` with one exact substring replaced, so a case shows only its defect."""
    assert old in CONFIRMED, old
    return CONFIRMED.replace(old, new) if old else CONFIRMED


def test_a_correctly_confirmed_high_risk_call_passes(pack_dir: Path) -> None:
    found = rules(pack_dir, {"main": CONFIRMED}, Severity.ERROR)
    assert found == set(), found


def test_unconfirmed_write_is_an_error(pack_dir: Path) -> None:
    graph = """
id: main
state: { charge_id: str | None }
start: ask_which
nodes:
  ask_which: { type: ask, slots: [charge_id], prompt: "which?", next: do_it }
  do_it:
    type: tool
    tool: write_tool
    args: { charge_id: state.charge_id }
    next: finish
  finish: { type: end }
"""
    found = rules(pack_dir, {"main": graph})
    assert "graph.unconfirmed_write" in found
    assert "graph.approval_missing" in found


def test_a_confirm_on_only_one_path_is_caught(pack_dir: Path) -> None:
    """The rule is "on all paths", not "somewhere in the graph"."""
    graph = """
id: main
state:
  charge_id: str | None
  amount: float | None
  shortcut: bool | None
start: pick
nodes:
  pick:
    type: router
    edges:
      state.shortcut == true: do_it
    default: confirm_it
  confirm_it:
    type: confirm
    action:
      tool: high_tool
      args: { charge_id: state.charge_id, amount: state.amount }
    prompt: "Refund it?"
    edges: { "yes": do_it, "no": finish }
  do_it:
    type: tool
    tool: high_tool
    args: { charge_id: state.charge_id, amount: state.amount }
    requires_approval: confirm_it
    next: finish
  finish: { type: end }
"""
    assert "graph.unconfirmed_write" in rules(pack_dir, {"main": graph})


def test_an_ask_between_the_confirm_and_the_call_invalidates_it(pack_dir: Path) -> None:
    """The customer spoke again after approving, so the approval no longer covers the call."""
    graph = confirmed(
        'edges: { "yes": do_it, "no": finish }',
        """edges: { "yes": ask_again, "no": finish }
  ask_again:
    type: ask
    slots: [charge_id]
    prompt: "and the charge id?"
    next: do_it""",
    )
    assert "graph.unconfirmed_write" in rules(pack_dir, {"main": graph})


def test_the_no_edge_of_a_confirm_grants_nothing(pack_dir: Path) -> None:
    graph = confirmed('"no": finish', '"no": do_it')
    assert "graph.unconfirmed_write" in rules(pack_dir, {"main": graph})


def test_confirm_exempt_tools_need_no_confirm_and_are_listed(pack_dir: Path) -> None:
    graph = """
id: main
state: { charge_id: str | None }
start: ask_which
nodes:
  ask_which: { type: ask, slots: [charge_id], prompt: "which?", next: do_it }
  do_it:
    type: tool
    tool: exempt_tool
    args: { charge_id: state.charge_id }
    next: finish
  finish: { type: end }
"""
    found = check(pack_dir, {"main": graph})
    assert "graph.unconfirmed_write" not in {f.rule for f in found}
    assert "graph.confirm_exempt" in {f.rule for f in found if f.severity is Severity.INFO}


def test_approval_mismatch_on_arguments(pack_dir: Path) -> None:
    graph = confirmed(
        "args: { charge_id: state.charge_id, amount: state.amount }\n    requires_approval",
        "args: { charge_id: state.charge_id, amount: 999 }\n    requires_approval",
    )
    assert "graph.approval_mismatch" in rules(pack_dir, {"main": graph})


def test_approval_mismatch_on_tool(pack_dir: Path) -> None:
    graph = confirmed("tool: high_tool\n    args", "tool: write_tool\n    args")
    assert "graph.approval_mismatch" in rules(pack_dir, {"main": graph})


def test_approval_names_a_node_that_is_not_a_confirm(pack_dir: Path) -> None:
    graph = confirmed("requires_approval: confirm_it", "requires_approval: finish")
    assert "graph.approval_unknown" in rules(pack_dir, {"main": graph})


def test_approval_unreachable_when_another_confirm_covers_one_path(pack_dir: Path) -> None:
    """Every path has *a* confirm, but not the one this node's approval is bound to."""
    graph = """
id: main
state:
  charge_id: str | None
  amount: float | None
  other: bool | None
start: pick
nodes:
  pick:
    type: router
    edges:
      state.other == true: confirm_other
    default: confirm_it
  confirm_it:
    type: confirm
    action:
      tool: high_tool
      args: { charge_id: state.charge_id, amount: state.amount }
    prompt: "Refund it?"
    edges: { "yes": do_it, "no": finish }
  confirm_other:
    type: confirm
    action:
      tool: high_tool
      args: { charge_id: state.charge_id, amount: state.amount }
    prompt: "Really refund it?"
    edges: { "yes": do_it, "no": finish }
  do_it:
    type: tool
    tool: high_tool
    args: { charge_id: state.charge_id, amount: state.amount }
    requires_approval: confirm_it
    next: finish
  finish: { type: end }
"""
    found = rules(pack_dir, {"main": graph})
    assert "graph.approval_unreachable" in found
    assert "graph.unconfirmed_write" not in found


def test_a_confirm_in_the_calling_graph_covers_a_call_in_the_sub_graph(pack_dir: Path) -> None:
    """The analysis is interprocedural, so a confirm before a subgraph call still counts."""
    caller_graph = """
id: main
state:
  charge_id: str | None
  amount: float | None
start: confirm_it
nodes:
  confirm_it:
    type: confirm
    action:
      tool: high_tool
      args: { charge_id: state.charge_id, amount: state.amount }
    prompt: "Refund it?"
    edges: { "yes": call, "no": finish }
  call:
    type: subgraph
    graph: worker
    inputs: { charge_id: state.charge_id, amount: state.amount }
    next: finish
  finish: { type: end }
"""
    worker = """
id: worker
inputs:
  charge_id: str | None
  amount: float | None
state:
  charge_id: str | None
  amount: float | None
start: do_it
nodes:
  do_it:
    type: tool
    tool: high_tool
    args: { charge_id: state.charge_id, amount: state.amount }
    next: finish
  finish: { type: end }
"""
    found = rules(pack_dir, {"main": caller_graph, "worker": worker})
    assert "graph.unconfirmed_write" not in found
    # It still has no requires_approval, which DESIGN.md 8.2 demands separately.
    assert "graph.approval_missing" in found


def test_an_unconfirmed_second_caller_of_the_sub_graph_is_caught(pack_dir: Path) -> None:
    caller_graph = """
id: main
state:
  charge_id: str | None
  amount: float | None
start: pick
nodes:
  pick:
    type: router
    edges:
      state.charge_id != none: call
    default: call
  call:
    type: subgraph
    graph: worker
    inputs: { charge_id: state.charge_id, amount: state.amount }
    next: finish
  finish: { type: end }
"""
    worker = """
id: worker
inputs:
  charge_id: str | None
  amount: float | None
state:
  charge_id: str | None
  amount: float | None
start: do_it
nodes:
  do_it:
    type: tool
    tool: high_tool
    args: { charge_id: state.charge_id, amount: state.amount }
    next: finish
  finish: { type: end }
"""
    assert "graph.unconfirmed_write" in rules(pack_dir, {"main": caller_graph, "worker": worker})


def test_confirm_over_a_read_tool_is_a_warning(pack_dir: Path) -> None:
    graph = confirmed("tool: high_tool\n      args", "tool: read_tool\n      args")
    assert "graph.confirm_action_is_read" in rules(pack_dir, {"main": graph})


def test_confirm_action_tool_unknown(pack_dir: Path) -> None:
    graph = confirmed("tool: high_tool\n      args", "tool: ghost_tool\n      args")
    assert "graph.confirm_action_tool_unknown" in rules(pack_dir, {"main": graph})


def test_confirm_edges_incomplete(pack_dir: Path) -> None:
    graph = confirmed('{ "yes": do_it, "no": finish }', '{ "yes": do_it }')
    assert "graph.edges_incomplete" in rules(pack_dir, {"main": graph})


def test_yaml_bare_yes_and_no_edge_keys_are_accepted(pack_dir: Path) -> None:
    """YAML 1.1 reads bare ``yes:``/``no:`` as booleans; DESIGN.md 6.4 writes them that way."""
    graph = confirmed('{ "yes": do_it, "no": finish }', "{ yes: do_it, no: finish }")
    found = rules(pack_dir, {"main": graph})
    assert "graph.node_invalid" not in found
    assert "graph.edges_incomplete" not in found


# --------------------------------------------------------------------------------------
# The reference packs
# --------------------------------------------------------------------------------------


def test_reference_pack_validates() -> None:
    """The DESIGN.md section 6.4 refund workflow passes every rule."""
    report = validate_pack(REFUND_PACK)
    assert report.ok, [f.render() for f in report.errors]
    assert not report.empty
    assert {"graphs/refund.yaml", "graphs/root.yaml", "graphs/verify_identity.yaml"} <= set(
        report.graph_files
    )


def test_reference_pack_reports_the_expected_warnings_and_notices() -> None:
    report = validate_pack(REFUND_PACK)
    warnings = {f.rule for f in report.warnings}
    assert warnings == {"graph.state_type_unresolved", "graph.assignment_optional"}
    infos = {f.rule for f in report.findings if f.severity is Severity.INFO}
    assert "graph.confirm_exempt" in infos
    assert "graph.node_not_executable" in infos


def test_findings_name_the_file_and_the_node() -> None:
    report = validate_pack(REFUND_PACK)
    located = [f for f in report.findings if f.node]
    assert located, "phase 1 findings must carry node ids"
    rendered = located[0].render()
    assert f"[{located[0].location}:{located[0].node}]" in rendered


def test_cli_prints_file_node_and_rule() -> None:
    result = CliRunner().invoke(cli, ["pack", "validate", str(REFUND_PACK)])
    assert result.exit_code == EXIT_OK, result.output
    assert "graph.assignment_optional [graphs/refund.yaml:fetch_charge]" in result.output
    assert "refund-pack: well-formed" in result.output


def test_cli_strict_fails_on_warnings_only() -> None:
    strict = CliRunner().invoke(cli, ["pack", "validate", "--strict", str(REFUND_PACK)])
    assert strict.exit_code == EXIT_INVALID


def test_deterministic_pack_validates() -> None:
    report = validate_pack(DETERMINISTIC_PACK)
    assert report.ok, [f.render() for f in report.errors]
