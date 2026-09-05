"""Graph file schema and parsing. Implements DESIGN.md sections 6.1 and 6.4.

A graph file is::

    id: refund
    description: Handle a refund request.
    inputs:   { charge_hint: "str | None" }
    outputs:  { outcome: 'Literal["refunded", "denied"]' }
    state:    { charge_id: "str | None" }
    start: identity_gate
    nodes:
      identity_gate: { type: gate, ... }

Edges are declared inside nodes (``next``, ``edges``, ``on_error``, ``default``), exactly as
DESIGN.md section 6.4 shows, so there is no separate top-level ``edges`` key.

Parsing is deliberately split from validating: :func:`parse_graph` turns text into a
:class:`Graph` (or into findings if it cannot), and :mod:`support_core.graph.rules` decides
whether the resulting graph is *sound*. That split is what lets the validator report ten
problems in one run instead of dying on the first.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from support_core.graph.expr import ParseError, parse
from support_core.graph.expr.syntax import Expr
from support_core.graph.findings import Finding, Severity
from support_core.graph.nodes import NODE_TYPES, ConfirmNode, NodeBase, Scalar
from support_core.graph.types import BuiltModel, build_model

GRAPH_ID = re.compile(r"^[a-z][a-z0-9_]*$")
NODE_ID = re.compile(r"^[a-z][a-z0-9_]*$")

EXPRESSION_START = re.compile(r"^\s*(state|ctx|result)\b")
"""A scalar that starts with a root is an expression and must parse as one."""

LOOKS_LIKE_EXPRESSION = re.compile(
    r"(==|!=|<=|>=|\s<\s|\s>\s|\s\|\s|\bnot\s|\sand\s|\sor\s)"
    r"|^\s*[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_]"
)
"""A scalar that resembles an expression but does not start with a root is almost certainly a
typo (``stat.charge_id``), not a literal. DESIGN.md section 6.4 uses the same key for both
(``into: { eligible: result.eligible }`` and ``into: { outcome: "refunded" }``), so without this
check a misspelt root would silently become the string ``'stat.charge_id'``."""


class GraphFileSchema(BaseModel):
    """The top level of a graph YAML file. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=GRAPH_ID.pattern)
    description: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    state: dict[str, str] = Field(default_factory=dict)
    start: str = Field(min_length=1)
    nodes: dict[str, Any] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class Value:
    """One entry of an ``args``/``into``/``outputs``/``inputs`` mapping.

    Either an expression (``result.eligible``) or a literal (``"refunded"``). See
    :func:`parse_value` for how the two are told apart.
    """

    raw: str
    expression: Expr | None
    literal: Any = None

    @property
    def is_expression(self) -> bool:
        return self.expression is not None


class ValueLooksLikeExpression(ValueError):
    """A scalar resembles an expression but does not start with a known root."""


def parse_value(raw: Scalar) -> Value:
    """Classify a mapping value as an expression or a literal.

    Only a string can be an expression: a YAML ``false`` or ``12`` is always a literal of that
    type. Raises :class:`~support_core.graph.expr.ParseError` when a string that starts with a
    root does not parse, and :class:`ValueLooksLikeExpression` when one that does not start with
    a root still looks like one.
    """
    if not isinstance(raw, str):
        return Value(raw=repr(raw), expression=None, literal=raw)
    if EXPRESSION_START.match(raw):
        return Value(raw=raw, expression=parse(raw))
    if LOOKS_LIKE_EXPRESSION.search(raw):
        try:
            expression = parse(raw)
        except ParseError as exc:
            msg = (
                f"{raw!r} looks like an expression but does not start with a root "
                f"(state, ctx, result): {exc}. Quote it differently if it really is a literal."
            )
            raise ValueLooksLikeExpression(msg) from exc
        return Value(raw=raw, expression=expression)
    return Value(raw=raw, expression=None, literal=raw)


@dataclass(slots=True)
class Graph:
    """One parsed graph. Sound or not: :mod:`support_core.graph.rules` decides that."""

    id: str
    file: str
    """Path relative to the pack directory, for findings."""

    description: str | None
    start: str
    nodes: dict[str, NodeBase]
    inputs: BuiltModel
    outputs: BuiltModel
    state: BuiltModel
    source_hash: str
    """SHA-256 of the file text, for the version pin (DESIGN.md section 6.7)."""

    state_shape: dict[str, str] = field(default_factory=dict)
    """The declared state field types, for the state-shape hash of the version pin."""

    def node_ids(self) -> list[str]:
        return list(self.nodes)


def parse_graph(text: str, *, file: str) -> tuple[Graph | None, list[Finding]]:
    """Parse one graph file. Returns ``(graph, findings)``; ``graph`` is ``None`` on a hard stop.

    A hard stop is a file that is not YAML, not a mapping, or does not satisfy
    :class:`GraphFileSchema`. Everything else (a node with an unknown type, a bad state type)
    is a finding and the graph is still returned so later rules can run.
    """
    findings: list[Finding] = []
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [
            Finding(
                severity=Severity.ERROR,
                rule="graph.invalid_yaml",
                message=f"invalid YAML: {exc}",
                location=file,
            )
        ]
    except RecursionError:
        # PyYAML recurses per nesting level, so a file of ten thousand open brackets is a
        # RecursionError rather than a parse error. The validator's contract is a report, not
        # an exception, so it becomes a finding like any other malformed file.
        return None, [
            Finding(
                severity=Severity.ERROR,
                rule="graph.invalid_yaml",
                message="YAML is nested too deeply to parse",
                location=file,
            )
        ]
    if raw is None or not isinstance(raw, dict):
        return None, [
            Finding(
                severity=Severity.ERROR,
                rule="graph.invalid",
                message="top level must be a mapping with id, start and nodes",
                location=file,
            )
        ]
    try:
        schema = GraphFileSchema.model_validate(raw)
    except ValidationError as exc:
        return None, [
            Finding(
                severity=Severity.ERROR,
                rule="graph.invalid",
                message="; ".join(
                    f"{'.'.join(str(loc) for loc in err['loc']) or '<root>'}: {err['msg']}"
                    for err in exc.errors()
                ),
                location=file,
            )
        ]

    nodes: dict[str, NodeBase] = {}
    for node_id, block in schema.nodes.items():
        node, node_findings = _parse_node(node_id, block, file=file)
        findings.extend(node_findings)
        if node is not None:
            nodes[node_id] = node

    inputs = build_model(f"{schema.id.title()}Inputs", schema.inputs)
    outputs = build_model(f"{schema.id.title()}Outputs", schema.outputs)
    state = build_model(f"{schema.id.title()}State", schema.state, all_optional=True)
    for kind, built in (("inputs", inputs), ("outputs", outputs), ("state", state)):
        findings.extend(_declaration_findings(kind, built, file=file))

    graph = Graph(
        id=schema.id,
        file=file,
        description=schema.description,
        start=schema.start,
        nodes=nodes,
        inputs=inputs,
        outputs=outputs,
        state=state,
        source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        state_shape=dict(schema.state),
    )
    return graph, findings


def _parse_node(node_id: str, block: Any, *, file: str) -> tuple[NodeBase | None, list[Finding]]:
    if not NODE_ID.match(node_id):
        return None, [
            Finding(
                severity=Severity.ERROR,
                rule="graph.node_id_invalid",
                message=f"node id {node_id!r} must match {NODE_ID.pattern}",
                location=file,
                node=node_id,
            )
        ]
    if not isinstance(block, dict):
        return None, [
            Finding(
                severity=Severity.ERROR,
                rule="graph.node_invalid",
                message="node definition must be a mapping",
                location=file,
                node=node_id,
            )
        ]
    node_type = block.get("type")
    if not isinstance(node_type, str) or node_type not in NODE_TYPES:
        known = ", ".join(sorted(NODE_TYPES))
        return None, [
            Finding(
                severity=Severity.ERROR,
                rule="graph.node_type_unknown",
                message=f"unknown node type {node_type!r}; known types are {known}",
                location=file,
                node=node_id,
            )
        ]
    block = _normalise_confirm_edges(node_type, block)
    try:
        node = NODE_TYPES[node_type].model.model_validate(block)
    except ValidationError as exc:
        return None, [
            Finding(
                severity=Severity.ERROR,
                rule="graph.node_invalid",
                message="; ".join(
                    f"{'.'.join(str(loc) for loc in err['loc']) or '<root>'}: {err['msg']}"
                    for err in exc.errors()
                ),
                location=file,
                node=node_id,
            )
        ]
    return node, []


_YAML_BOOL_EDGE_LABELS = {True: "yes", False: "no"}
"""YAML 1.1 reads a bare ``yes:``/``no:`` key as a boolean, and DESIGN.md section 6.4 writes
confirm edges exactly that way. Rather than making every pack author quote them, the keys are
translated back before validation."""


def _normalise_confirm_edges(node_type: str, block: dict[str, Any]) -> dict[str, Any]:
    if node_type != ConfirmNode.model_fields["type"].default and node_type != "confirm":
        return block
    edges = block.get("edges")
    if not isinstance(edges, dict):
        return block
    if not any(isinstance(key, bool) for key in edges):
        return block
    fixed = dict(block)
    fixed["edges"] = {
        _YAML_BOOL_EDGE_LABELS.get(k, k) if isinstance(k, bool) else k: v for k, v in edges.items()
    }
    return fixed


_DECLARATION_RULES = {
    "bad_name": ("graph.state_field_invalid", Severity.ERROR),
    "invalid_type": ("graph.state_type_invalid", Severity.ERROR),
    "unresolved_type": ("graph.state_type_unresolved", Severity.WARNING),
    "not_optional": ("graph.state_field_not_optional", Severity.WARNING),
}


def _declaration_findings(kind: str, built: BuiltModel, *, file: str) -> list[Finding]:
    findings: list[Finding] = []
    for issue in built.issues:
        rule, severity = _DECLARATION_RULES[issue.code]
        findings.append(
            Finding(
                severity=severity,
                rule=rule,
                message=f"{kind}.{issue.field}: {issue.message}",
                location=file,
            )
        )
    return findings


def read_graphs(
    pack_path: Path,
    graph_files: Iterable[str],
    *,
    sources: dict[str, str] | None = None,
) -> tuple[dict[str, Graph], list[Finding]]:
    """Read and parse every graph file of a pack.

    Returns the graphs by id plus the findings raised while parsing them. A file that cannot be
    read or parsed contributes findings and no graph; two files declaring the same id keep the
    first and report the second, so later rules see a consistent set.

    ``sources`` is a snapshot of file text keyed by the pack-relative path. A file already in
    it is parsed from the snapshot rather than re-read; a file read here is added to it. That
    is what stops a pack edited on disk between two reads producing a
    :class:`~support_core.graph.pack.PackPin` whose hashes describe a mix of two versions
    (phase-1 deferred finding P1): the validator fills the snapshot and the loader parses the
    same bytes it hashes.
    """
    graphs: dict[str, Graph] = {}
    findings: list[Finding] = []
    for rel in graph_files:
        text = sources.get(rel) if sources is not None else None
        if text is None:
            try:
                text = (pack_path / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        rule="graph.unreadable",
                        message=f"cannot be read as UTF-8 text: {exc}",
                        location=rel,
                    )
                )
                continue
            if sources is not None:
                sources[rel] = text
        graph, graph_findings = parse_graph(text, file=rel)
        findings.extend(graph_findings)
        if graph is None:
            continue
        if graph.id in graphs:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="graph.duplicate_id",
                    message=f"graph id {graph.id!r} is already defined by {graphs[graph.id].file}",
                    location=rel,
                )
            )
            continue
        graphs[graph.id] = graph
    return graphs, findings
