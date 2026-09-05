"""A differential test for the confirm-on-all-paths dataflow (DESIGN.md sections 5.2 and 8.2).

The implementer's self-critique named this as the test they would write first, and the
independent reviewer wrote one in scratch and asked for it in the suite. It generates random
acyclic graphs and compares the fixpoint analysis in :mod:`support_core.graph.rules` against a
brute-force enumeration of every path from the start node, written here from the rule text
rather than from the implementation:

    a WRITE or HIGH ``tool`` node is unconfirmed when some path from the start node to it
    carries no confirmation, where a ``confirm``'s ``yes`` edge grants one, and a ``confirm``'s
    ``no`` edge, an ``ask`` and the start of the graph all revoke it.

Scope: one graph, acyclic, so the enumeration terminates and no cycle rule interferes. The
cross-graph half of the rule (a confirm must be in the tool node's own graph) is covered by the
worked fixtures in ``tests/test_graph_validator.py``.
"""

import random
from pathlib import Path
from textwrap import indent

import pytest

from support_core.graph.findings import Severity
from support_core.graph.manifest import PackManifest
from support_core.graph.rules import validate_graph_set
from support_core.graph.schema import read_graphs
from support_core.graph.tools_manifest import load_tool_manifest
from tests.test_graph_validator import MANIFEST, TOOLS_YAML

GRAPHS = 300
"""How many random graphs to compare. Fast: the whole test runs in well under a second."""

MAX_PATHS = 20000
"""Enumeration guard. A generated graph never approaches this; a bug in the generator would."""


class _Node:
    """One generated node: its kind and its labelled outgoing edges."""

    def __init__(self, kind: str, edges: list[tuple[str, str]]) -> None:
        self.kind = kind
        self.edges = edges


def _generate(rng: random.Random) -> dict[str, _Node]:
    """A random acyclic graph. Every edge points forwards, so ``finish`` is always reachable."""
    count = rng.randint(3, 7)
    ids = [f"n{i}" for i in range(count)] + ["finish"]
    nodes: dict[str, _Node] = {}
    for index in range(count):
        later = ids[index + 1 :]
        kind = rng.choice(["say", "router", "ask", "confirm", "tool", "tool"])
        if kind == "router":
            edges = [
                ("state.flag == true", rng.choice(later)),
                ("default", rng.choice(later)),
            ]
        elif kind == "confirm":
            edges = [("yes", rng.choice(later)), ("no", rng.choice(later))]
        else:
            edges = [("next", rng.choice(later))]
        nodes[ids[index]] = _Node(kind, edges)
    nodes["finish"] = _Node("end", [])
    return nodes


def _render(nodes: dict[str, _Node]) -> str:
    """The generated graph as a YAML file."""
    body = ""
    for node_id, node in nodes.items():
        if node.kind == "say":
            block = f'type: say\nmessage: "hello"\nnext: {node.edges[0][1]}'
        elif node.kind == "router":
            branches = "\n".join(f"  {label}: {target}" for label, target in node.edges[:1])
            block = f"type: router\nedges:\n{branches}\ndefault: {node.edges[1][1]}"
        elif node.kind == "ask":
            block = f'type: ask\nslots: [charge_id]\nprompt: "which?"\nnext: {node.edges[0][1]}'
        elif node.kind == "confirm":
            block = (
                "type: confirm\naction:\n  tool: high_tool\n"
                "  args: { charge_id: state.charge_id, amount: state.amount }\n"
                f'prompt: "ok?"\nedges: {{ "yes": {node.edges[0][1]}, "no": {node.edges[1][1]} }}'
            )
        elif node.kind == "tool":
            block = (
                "type: tool\ntool: high_tool\n"
                "args: { charge_id: state.charge_id, amount: state.amount }\n"
                f"next: {node.edges[0][1]}"
            )
        else:
            block = "type: end"
        body += f"  {node_id}:\n{indent(block, '    ')}\n"
    return (
        "id: main\n"
        "state:\n"
        "  charge_id: str | None\n"
        "  amount: float | None\n"
        "  flag: bool | None\n"
        "start: n0\n"
        f"nodes:\n{body}"
    )


def _expected_unconfirmed(nodes: dict[str, _Node]) -> set[str]:
    """Brute force: every path from ``n0``, tracking whether a confirmation is currently held.

    A tool node is unconfirmed if *any* path reaches it while holding none. Written from the
    rule text, deliberately not sharing anything with the analysis under test.
    """
    unconfirmed: set[str] = set()
    paths = 0
    stack: list[tuple[str, bool]] = [("n0", False)]
    while stack:
        node_id, held = stack.pop()
        paths += 1
        assert paths < MAX_PATHS, "generated graph is not acyclic"
        node = nodes[node_id]
        if node.kind == "tool" and not held:
            unconfirmed.add(node_id)
        for label, target in node.edges:
            if node.kind == "confirm":
                outgoing = label == "yes"
            elif node.kind == "ask":
                outgoing = False
            else:
                outgoing = held
            stack.append((target, outgoing))
    return unconfirmed


def _analysed_unconfirmed(pack_dir: Path, source: str) -> set[str]:
    (pack_dir / "graphs" / "main.yaml").write_text(source, encoding="utf-8")
    parsed, findings = read_graphs(pack_dir, ["graphs/main.yaml"])
    findings += validate_graph_set(parsed, load_tool_manifest(pack_dir), MANIFEST)
    assert not [
        f
        for f in findings
        if f.severity is Severity.ERROR and f.rule.startswith(("expr.", "graph.invalid"))
    ], "the generator produced a graph that does not parse"
    return {f.node for f in findings if f.rule == "graph.unconfirmed_write" and f.node is not None}


@pytest.fixture
def pack_dir(tmp_path: Path) -> Path:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "tools.yaml").write_text(TOOLS_YAML, encoding="utf-8")
    (tmp_path / "graphs").mkdir()
    return tmp_path


def test_the_dataflow_agrees_with_brute_force_path_enumeration(pack_dir: Path) -> None:
    rng = random.Random(20260905)  # seeded, so a failure is reproducible
    compared = 0
    for _ in range(GRAPHS):
        nodes = _generate(rng)
        source = _render(nodes)
        expected = _expected_unconfirmed(nodes)
        actual = _analysed_unconfirmed(pack_dir, source)
        assert actual == expected, f"disagreement on:\n{source}\nexpected {expected}, got {actual}"
        compared += 1
    assert compared == GRAPHS


def test_the_generator_actually_produces_both_verdicts(pack_dir: Path) -> None:
    """A differential test that only ever compares empty sets proves nothing."""
    rng = random.Random(20260905)
    verdicts = set()
    for _ in range(GRAPHS):
        nodes = _generate(rng)
        verdicts.add(bool(_expected_unconfirmed(nodes)))
    assert verdicts == {True, False}


def test_manifest_fixture_is_the_shared_one() -> None:
    """Pin the import so a change in the validator fixtures does not silently weaken this."""
    assert isinstance(MANIFEST, PackManifest)
    assert MANIFEST.entry_graph == "main"
