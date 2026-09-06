"""One failing fixture per validator rule (DESIGN.md section 5.2, section 8.2).

Each case is a complete, minimal set of graph files plus the shared tool manifest below. The
assertion is on the stable rule id, so a rename is a deliberate, visible break.

The reference pack ``tests/packs/refund_pack`` (DESIGN.md's own section 6.4 refund workflow) is
the positive case: it must validate with no errors.
"""

import re
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
    confirm_exempt_reason: the customer cannot be asked to confirm a passcode
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
        "graph.llm_output_schema_absent",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: think
nodes:
  think:
    type: llm
    instructions: think
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
    # -- end outputs -------------------------------------------------------------------
    (
        "graph.end_output_unknown",
        {
            "main": """
id: main
outputs: { outcome: str }
state: { charge_id: str | None }
start: finish
nodes:
  finish: { type: end, outputs: { outcome: "done", surprise: "extra" } }
"""
        },
    ),
    (
        "graph.end_output_missing",
        {
            "main": """
id: main
outputs: { outcome: str }
state: { charge_id: str | None }
start: finish
nodes:
  finish: { type: end }
"""
        },
    ),
    # -- expression notes on optional values -------------------------------------------
    (
        "expr.optional_filter_input",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: pick
nodes:
  pick:
    type: router
    edges:
      state.charge_id | lower == "abc": finish
    default: finish
  finish: { type: end }
"""
        },
    ),
    (
        "expr.optional_comparison",
        {
            "main": """
id: main
state: { amount: float | None }
start: pick
nodes:
  pick:
    type: router
    edges:
      state.amount > 10: finish
    default: finish
  finish: { type: end }
"""
        },
    ),
    # -- approval binding on a tool that needs none ------------------------------------
    (
        "graph.approval_not_needed",
        {
            "main": """
id: main
state: { charge_id: str | None }
start: look
nodes:
  look:
    type: tool
    tool: read_tool
    args: { charge_id: state.charge_id }
    requires_approval: look
    next: finish
  finish: { type: end }
"""
        },
    ),
]


@pytest.mark.parametrize(("rule", "graphs"), CASES, ids=[case[0] for case in CASES])
def test_rule_fires(pack_dir: Path, rule: str, graphs: dict[str, str]) -> None:
    assert rule in rules(pack_dir, graphs)


_LITERAL_RULE_ID = re.compile(r'self\.error\(\s*\n?\s*"([a-z_]+\.[a-z_]+)"')


def test_every_error_rule_id_in_rules_py_is_asserted_somewhere_in_this_file() -> None:
    """Phase-1 review F2: five listed rule ids, two of them ERROR, had no test at all.

    BACKLOG.md asks for "one failing fixture per rule" and the exit criterion for "the right
    rule name", so a new ERROR rule in :mod:`support_core.graph.rules` must arrive with a case
    here. Only rule ids written as literals are checked; the ``expr.*`` and ``template.*``
    families are built from a note code and are covered by their own tests.
    """
    source = (REPO_ROOT / "support_core" / "graph" / "rules.py").read_text(encoding="utf-8")
    declared = set(_LITERAL_RULE_ID.findall(source))
    asserted = set(re.findall(r'"([a-z_]+\.[a-z_]+)"', Path(__file__).read_text(encoding="utf-8")))
    missing = sorted(declared - asserted)
    assert not missing, f"ERROR rule ids with no test asserting on them: {missing}"


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


def test_a_prompt_budget_below_the_core_prompt_is_refused(pack_dir: Path) -> None:
    """Review finding V11: a pack can deny itself service through ``llm.prompt_budget``.

    ``core_system: 1`` makes every turn raise ``PromptTooLargeError`` and hand off, which is
    self-inflicted but silent until a customer arrives. It is a static property of the manifest.
    """
    manifest = MANIFEST.model_copy(
        update={"llm": MANIFEST.llm.model_copy(update={"prompt_budget": {"core_system": 1}})}
    )
    (pack_dir / "graphs" / "main.yaml").write_text(MAIN, encoding="utf-8")
    parsed, _ = read_graphs(pack_dir, ["graphs/main.yaml"])
    found = validate_graph_set(parsed, load_tool_manifest(pack_dir), manifest)
    assert "manifest.prompt_budget_too_small" in {f.rule for f in found}

    generous = MANIFEST.model_copy(
        update={"llm": MANIFEST.llm.model_copy(update={"prompt_budget": {"core_system": 1200}})}
    )
    ok = validate_graph_set(parsed, load_tool_manifest(pack_dir), generous)
    assert "manifest.prompt_budget_too_small" not in {f.rule for f in ok}


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


def test_a_call_cycle_is_judged_on_the_path_the_cycle_takes(pack_dir: Path) -> None:
    """Phase-1 deferred finding N3, and the reviewer's hostile case T.

    The rule used to ask whether any graph in the cycle contained a suspending node *anywhere*,
    which two graphs recursing through each other walked straight past by putting an ``ask`` on
    a branch the cycle never takes: the loop waits for nobody and nothing reported it. What
    matters is whether the loop can be travelled without waiting, so each leg is judged on its
    own path from that graph's start to the call that continues the cycle.
    """
    spinning = """
id: main
state: { charge_id: str | None }
start: pick
nodes:
  pick:
    type: router
    edges: { "state.charge_id == null": call }
    default: ask_first
  ask_first:
    type: ask
    slots: [charge_id]
    prompt: "which charge?"
    next: finish
  call: { type: subgraph, graph: other, next: finish }
  finish: { type: end }
"""
    other = """
id: other
state: { charge_id: str | None }
start: call
nodes:
  call: { type: subgraph, graph: main, next: finish }
  finish: { type: end }
"""
    assert "graph.subgraph_cycle" in rules(pack_dir, {"main": spinning, "other": other})


def test_a_call_cycle_that_has_to_wait_on_every_pass_is_not_a_spin(pack_dir: Path) -> None:
    """The other half of N3: the ``ask`` is *on* the path, so the loop is a conversation."""
    waiting = """
id: main
state: { charge_id: str | None }
start: ask_first
nodes:
  ask_first:
    type: ask
    slots: [charge_id]
    prompt: "which charge?"
    next: call
  call: { type: subgraph, graph: other, next: finish }
  finish: { type: end }
"""
    other = """
id: other
state: { charge_id: str | None }
start: call
nodes:
  call: { type: subgraph, graph: main, next: finish }
  finish: { type: end }
"""
    assert "graph.subgraph_cycle" not in rules(pack_dir, {"main": waiting, "other": other})


def test_a_retry_that_repeats_a_side_effect_is_reported(pack_dir: Path) -> None:
    """The load-time half of the shape phase 4's resolution left open.

    A ``tool`` node whose ``on_error`` edge leads back to it runs its tool again on every
    failure, under a fresh idempotency key. For a tool that needs an approval the second attempt
    is refused, and ``graph.approval_reused`` refuses the loop at load; this is the case neither
    covers - a WRITE tool marked ``confirm_exempt``, where nothing stands between the failure
    and the repeat.

    A warning, not an error: re-sending a one-time passcode after a failure is exactly this
    shape and is exactly right. What the author has to do is decide.
    """
    graph = """
id: main
state: { charge_id: str | None, sent: bool | None }
start: send
nodes:
  send:
    type: tool
    tool: exempt_tool
    args: { charge_id: state.charge_id }
    into: { sent: result.sent }
    on_error: ask_again
    next: finish
  ask_again:
    type: ask
    slots: [charge_id]
    prompt: "that failed - try again?"
    next: send
  finish: { type: end }
"""
    assert "graph.on_error_repeats_side_effect" in rules(pack_dir, {"main": graph})


def test_an_on_error_edge_that_does_not_come_back_is_not_reported(pack_dir: Path) -> None:
    """The sample pack's own shape: a failure that gives up rather than retrying."""
    graph = """
id: main
state: { charge_id: str | None, sent: bool | None }
start: send
nodes:
  send:
    type: tool
    tool: exempt_tool
    args: { charge_id: state.charge_id }
    into: { sent: result.sent }
    on_error: give_up
    next: finish
  give_up:
    type: say
    message: "I could not send it."
    next: finish
  finish: { type: end }
"""
    assert "graph.on_error_repeats_side_effect" not in rules(pack_dir, {"main": graph})


def test_the_confirm_exemption_report_names_the_arguments_each_call_site_passes(
    pack_dir: Path,
) -> None:
    """Phase 4's closing recommendation, and its self-critique's attack 7.

    The warning named the tool and not the call, so a reviewer could not tell a model-written
    address from the customer's own record - and the exemption is the reason nothing else is
    between them. Now the call sites and their arguments are in the message.
    """
    graph = """
id: main
state: { charge_id: str | None, sent: bool | None }
start: send
nodes:
  send:
    type: tool
    tool: exempt_tool
    args: { charge_id: state.charge_id }
    into: { sent: result.sent }
    next: finish
  finish: { type: end }
"""
    findings = [
        f
        for f in check(pack_dir, {"main": graph})
        if f.rule == "graph.confirm_exempt" and "exempt_tool" in f.message
    ]
    assert findings, "the exemption was not reported at all"
    assert "main.send(charge_id: 'state.charge_id')" in findings[0].message


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


def test_a_node_between_the_confirm_and_the_call_may_not_rewrite_the_arguments(
    pack_dir: Path,
) -> None:
    """Phase-1 deferred finding H, and the reviewer's hostile case H.

    The argument text on both sides is identical, so ``graph.approval_mismatch`` is happy; what
    is not identical is what ``state.amount`` *means* by the time the call happens.
    """
    graph = confirmed(
        '    edges: { "yes": do_it, "no": finish }',
        '    edges: { "yes": bump, "no": finish }\n'
        "  bump:\n"
        "    type: tool\n"
        "    tool: read_tool\n"
        "    args: { charge_id: state.charge_id }\n"
        "    into: { amount: result.amount }\n"
        "    next: do_it",
    )
    found = rules(pack_dir, {"main": graph})
    assert "graph.approval_args_mutated" in found
    assert "graph.approval_mismatch" not in found, "the argument text is identical on both sides"


def test_a_node_that_writes_something_else_between_confirm_and_call_is_fine(
    pack_dir: Path,
) -> None:
    """The rule is about the fields the *approved arguments* read, not about any write."""
    graph = confirmed(
        '    edges: { "yes": do_it, "no": finish }',
        '    edges: { "yes": note, "no": finish }\n'
        "  note:\n"
        "    type: tool\n"
        "    tool: read_tool\n"
        "    args: { charge_id: state.charge_id }\n"
        "    into: { outcome: result.label }\n"
        "    next: do_it",
    )
    assert rules(pack_dir, {"main": graph}, Severity.ERROR) == set()


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
    # A WARNING from phase 4 (deferred finding J), so `--strict` fails on it and the reason the
    # pack had to write down is in the message.
    exemptions = [f for f in found if f.rule == "graph.confirm_exempt"]
    assert [f.severity for f in exemptions] == [Severity.WARNING]
    assert "cannot be asked to confirm a passcode" in exemptions[0].message


def test_approval_mismatch_on_arguments(pack_dir: Path) -> None:
    graph = confirmed(
        "args: { charge_id: state.charge_id, amount: state.amount }\n    requires_approval",
        "args: { charge_id: state.charge_id, amount: 999 }\n    requires_approval",
    )
    assert "graph.approval_mismatch" in rules(pack_dir, {"main": graph})


@pytest.mark.parametrize(
    ("approved", "called"),
    [
        ("amount: 100", 'amount: "100"'),
        ("amount: 100.0", 'amount: "100.0"'),
    ],
)
def test_a_literal_and_a_string_that_look_alike_are_not_the_same_approval(
    pack_dir: Path, approved: str, called: str
) -> None:
    """F7: the canonical form must classify scalars the way the engine will evaluate them.

    ``{amount: 100}`` is a YAML int and ``{amount: "100"}`` is a string literal, so their
    ``canonical_json`` differs and DESIGN.md 8.2's run-time hash check would refuse the call.
    Comparing them by a different rule here would let the pack load and fail in production.
    """
    graph = CONFIRMED.replace(
        "args: { charge_id: state.charge_id, amount: state.amount }\n    prompt",
        f"args: {{ charge_id: state.charge_id, {approved} }}\n    prompt",
    ).replace(
        "args: { charge_id: state.charge_id, amount: state.amount }\n    requires_approval",
        f"args: {{ charge_id: state.charge_id, {called} }}\n    requires_approval",
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


def test_a_confirm_in_the_calling_graph_does_not_cover_a_call_in_the_sub_graph(
    pack_dir: Path,
) -> None:
    """DESIGN.md 8.2: the approving confirm must be a node in the tool node's own graph.

    The analysis is interprocedural and *can see* the caller's confirm, but the approval hash
    is computed over the argument values the callee evaluates, which the caller's confirm never
    saw. Accepting this shape would produce a pack that cannot execute (BACKLOG decision,
    2026-09-05).
    """
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
    found = check(pack_dir, {"main": caller_graph, "worker": worker})
    ids = {f.rule for f in found}
    assert "graph.unconfirmed_write" in ids
    # It also has no requires_approval, which DESIGN.md 8.2 demands separately.
    assert "graph.approval_missing" in ids
    messages = " ".join(f.message for f in found if f.rule == "graph.unconfirmed_write")
    assert "none of those confirms is a node in this graph" in messages
    assert "cannot see the values the callee computes" in messages


def test_naming_a_confirm_in_the_calling_graph_is_rejected_with_the_same_graph_reason(
    pack_dir: Path,
) -> None:
    """The other half of the same decision: the tool node may not name a caller's confirm."""
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
    requires_approval: confirm_it
    next: finish
  finish: { type: end }
"""
    found = check(pack_dir, {"main": caller_graph, "worker": worker})
    unknown = [f for f in found if f.rule == "graph.approval_unknown"]
    assert unknown, {f.rule for f in found}
    assert "Move the confirm into this graph" in unknown[0].message


def test_a_confirm_in_the_sub_graph_itself_covers_the_call(pack_dir: Path) -> None:
    """The shape a pack author must use instead: confirm and call in one graph."""
    caller_graph = """
id: main
state:
  charge_id: str | None
  amount: float | None
start: call
nodes:
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
start: confirm_it
nodes:
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
    found = rules(pack_dir, {"main": caller_graph, "worker": worker}, Severity.ERROR)
    assert found == set(), found


def test_a_confirm_before_a_sub_graph_call_still_covers_a_later_call_in_the_caller(
    pack_dir: Path,
) -> None:
    """Same-graph means "the same graph", not "no intervening call".

    The confirm and the tool node are both in ``main``; the frame that runs in between returns
    without asking the customer anything, so the approval still holds.
    """
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
    inputs: { charge_id: state.charge_id }
    next: do_it
  do_it:
    type: tool
    tool: high_tool
    args: { charge_id: state.charge_id, amount: state.amount }
    requires_approval: confirm_it
    next: finish
  finish: { type: end }
"""
    worker = """
id: worker
inputs: { charge_id: str | None }
state: { charge_id: str | None }
start: look
nodes:
  look:
    type: tool
    tool: read_tool
    args: { charge_id: state.charge_id }
    next: finish
  finish: { type: end }
"""
    found = rules(pack_dir, {"main": caller_graph, "worker": worker}, Severity.ERROR)
    assert found == set(), found


def test_an_ask_inside_the_called_sub_graph_invalidates_the_callers_approval(
    pack_dir: Path,
) -> None:
    """The customer spoke again inside the callee, so the caller's approval is stale."""
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
    inputs: { charge_id: state.charge_id }
    next: do_it
  do_it:
    type: tool
    tool: high_tool
    args: { charge_id: state.charge_id, amount: state.amount }
    requires_approval: confirm_it
    next: finish
  finish: { type: end }
"""
    worker = """
id: worker
inputs: { charge_id: str | None }
state: { charge_id: str | None }
start: ask_more
nodes:
  ask_more:
    type: ask
    slots: [charge_id]
    prompt: "which charge?"
    next: finish
  finish: { type: end }
"""
    assert "graph.unconfirmed_write" in rules(pack_dir, {"main": caller_graph, "worker": worker})


def test_a_graph_called_only_from_an_unreachable_node_is_still_analysed(pack_dir: Path) -> None:
    """F3: a callee whose only call sites are dead used to be checked by nothing at all."""
    caller_graph = """
id: main
state: { charge_id: str | None }
start: finish
nodes:
  orphan:
    type: subgraph
    graph: worker
    inputs: { charge_id: state.charge_id }
    next: finish
  finish: { type: end }
"""
    worker = """
id: worker
inputs: { charge_id: str | None }
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
    found = rules(pack_dir, {"main": caller_graph, "worker": worker})
    assert "graph.unconfirmed_write" in found
    assert "graph.node_unreachable" in found


def test_a_loop_back_into_a_confirmed_call_reuses_one_approval(pack_dir: Path) -> None:
    """F4: handoff passes both lattices and suspends, so nothing used to notice this loop."""
    graph = confirmed(
        "  finish: { type: end }",
        """  after:
    type: handoff
    reason: check_it
    edges: { resumed: do_it, closed: finish }
  finish: { type: end }""",
    ).replace(
        "requires_approval: confirm_it\n    next: finish",
        "requires_approval: confirm_it\n    next: after",
    )
    found = rules(pack_dir, {"main": graph})
    assert "graph.approval_reused" in found


def test_a_loop_that_passes_the_confirm_again_is_allowed(pack_dir: Path) -> None:
    """Re-entering through the confirm asks the customer again, so the approval is fresh."""
    graph = confirmed("next: finish\n  finish", "next: confirm_it\n  finish")
    found = rules(pack_dir, {"main": graph}, Severity.ERROR)
    assert found == set(), found


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
    assert warnings == {
        "graph.state_type_unresolved",
        "graph.assignment_optional",
        # Phase 4: the exemption is a warning now, and this fixture declares its tools in YAML
        # without exporting them, which is a pack that cannot run a tool at all.
        "graph.confirm_exempt",
        "tools.declared_not_exported",
    }
    # ``graph.node_not_executable`` no longer fires for this fixture: phase 6 made ``handoff``
    # executable, which was the last core type that was not, so every node this pack uses can
    # now run. The rule itself stays for a pack-declared type with no runner.
    infos = {f.rule for f in report.findings if f.severity is Severity.INFO}
    assert "graph.node_not_executable" not in infos


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


# --------------------------------------------------------------------------------------
# Adversarial input: the validator's contract is a report, never an exception.
# --------------------------------------------------------------------------------------


def test_deeply_nested_yaml_is_a_finding_not_a_recursion_error(pack_dir: Path) -> None:
    """PyYAML recurses per nesting level, so this reached the interpreter's stack limit."""
    deep = "a: " + "[" * 20_000 + "]" * 20_000 + "\n"
    (pack_dir / "graphs" / "main.yaml").write_text(deep, encoding="utf-8")
    _parsed, findings = read_graphs(pack_dir, ["graphs/main.yaml"])
    assert {f.rule for f in findings} == {"graph.invalid_yaml"}
    assert "nested too deeply" in findings[0].message


def test_a_yaml_anchor_bomb_does_not_hang(pack_dir: Path) -> None:
    """Alias expansion is bounded here because the result must still be a graph mapping."""
    bomb = (
        "a: &a [x,x,x,x,x,x,x,x,x]\n"
        "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
        "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
        "d: [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
    )
    assert "graph.invalid" in rules(pack_dir, {"main": bomb})


@pytest.mark.parametrize(
    ("literal", "expected_rule"),
    [
        ("Mr. Smith", None),
        ("abandoned", None),
        ("3.5", None),
        ("stat.charge_id", "expr.looks_like_expression"),
        ("statee.charge_id", "expr.looks_like_expression"),
        ("state.charge_id == ", "expr.parse_error"),
    ],
)
def test_literal_versus_expression_classification(
    pack_dir: Path, literal: str, expected_rule: str | None
) -> None:
    graph = f"""
    id: main
    outputs:
      outcome: str
    state:
      charge_id: str | None
    start: finish
    nodes:
      finish: {{ type: end, outputs: {{ outcome: "{literal}" }} }}
    """
    found = rules(pack_dir, {"main": graph})
    if expected_rule is None:
        assert "expr.looks_like_expression" not in found
        assert "expr.parse_error" not in found
    else:
        assert expected_rule in found


def test_expressions_cannot_reach_pydantic_internals(pack_dir: Path) -> None:
    graph = MAIN.replace("state.charge_id != none: finish", "state.model_config == none: finish")
    assert "expr.type_error" in rules(pack_dir, {"main": graph})


def test_a_graph_may_read_the_crm_record_out_of_ctx(pack_dir: Path) -> None:
    """F8: ``ctx.customer.attributes`` is a ``dict[str, Any]`` and must be readable."""
    graph = MAIN.replace(
        "state.charge_id != none: finish", "ctx.customer.attributes.plan == 'pro': finish"
    )
    assert rules(pack_dir, {"main": graph}, Severity.ERROR) == set()


def test_a_node_named_with_a_dunder_is_rejected(pack_dir: Path) -> None:
    graph = MAIN.replace("  finish: { type: end }", "  __init__: { type: end }")
    assert "graph.node_id_invalid" in rules(pack_dir, {"main": graph})


def test_a_high_risk_tool_cannot_be_confirm_exempt(tmp_path: Path) -> None:
    """DESIGN.md 8.2 offers the exemption for write-tier tools only."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "tools.yaml").write_text(
        TOOLS_YAML.replace(
            "  - name: high_tool\n    description: Move money.\n    risk: high\n",
            "  - name: high_tool\n    description: Move money.\n    risk: high\n"
            "    confirm_exempt: true\n    confirm_exempt_reason: because I say so\n",
        ),
        encoding="utf-8",
    )
    (tmp_path / "graphs").mkdir()
    graph = """
    id: main
    state:
      charge_id: str | None
      amount: float | None
    start: ask_which
    nodes:
      ask_which: { type: ask, slots: [charge_id], prompt: "which?", next: do_it }
      do_it:
        type: tool
        tool: high_tool
        args: { charge_id: state.charge_id, amount: state.amount }
        next: finish
      finish: { type: end }
    """
    found = rules(tmp_path, {"main": graph})
    assert "tools.high_risk_exempt" in found
    assert "graph.unconfirmed_write" in found
