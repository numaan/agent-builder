"""Graph validation rules. Implements every check in DESIGN.md section 5.2, plus the approval
binding of section 8.2.

Section 5.2, rule by rule, and the rule id that implements it:

===================================================================  ==========================
DESIGN.md 5.2                                                        rule id
===================================================================  ==========================
Every edge target exists                                             ``graph.edge_target_missing``
Every graph has exactly one ``start``                                ``graph.start_missing``
... and at least one ``end``                                         ``graph.no_end``
Every ``tool`` node references a registered tool                     ``graph.tool_unknown``
Argument expressions type-check against the tool's input model       ``graph.tool_arg_*``
Every ``gate`` node has a ``redirect`` graph                         ``graph.gate_redirect_unknown``
Every write/high tool node has a ``confirm`` on all paths            ``graph.unconfirmed_write``
Sub-graph input and output mappings type-check                       ``graph.subgraph_*``
No graph reaches itself without passing a suspending node            ``graph.unsuspended_cycle``,
                                                                     ``graph.subgraph_cycle``
Confirm exemptions are listed in the report                          ``graph.confirm_exempt``
===================================================================  ==========================

The confirm rule is the one PLAN.md calls the most valuable in the system, so it is a real
analysis rather than a pattern match. See :meth:`_Rules.confirm_coverage`.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from support_core.graph.context import ConversationContext
from support_core.graph.expr import ParseError, TypeError_, infer, model_type_env, parse
from support_core.graph.expr.syntax import Expr, literal_source, unparse
from support_core.graph.expr.typecheck import TypeEnv, TypeInfo, TypeNote, from_annotation
from support_core.graph.findings import Finding, Severity
from support_core.graph.manifest import PackManifest
from support_core.graph.nodes import (
    NODE_TYPES,
    AskNode,
    ConfirmNode,
    EndNode,
    GateNode,
    HandoffNode,
    LlmNode,
    NodeBase,
    RouterNode,
    SayNode,
    Scalar,
    SubgraphNode,
    ToolNode,
    edge_targets,
    graph_references,
)
from support_core.graph.schema import Graph, ValueLooksLikeExpression, parse_value
from support_core.graph.templates import make_environment
from support_core.graph.templates import validate as validate_template
from support_core.graph.tools_manifest import ToolManifest, ToolSpec
from support_core.graph.types import build_model
from support_core.tools.risk import MODEL_CALLABLE, Risk

Point = tuple[str, str]
"""``(graph id, node id)``: one node of the interprocedural control-flow graph."""

SAME_GRAPH_REASON = (
    "DESIGN.md section 8.2 hashes the tool name together with the canonical argument values, "
    "and a confirm in a calling graph cannot see the values the callee computes, so a "
    "cross-graph approval could never be verified at run time."
)
"""Why an approval must be bound to a confirm in the tool node's own graph.

Recorded as a decision in BACKLOG.md (2026-09-05) and stated in DESIGN.md section 8.2. Every
message that rejects a cross-graph approval quotes it, so a pack author is not left guessing.
"""


def validate_graph_set(
    graphs: dict[str, Graph], tools: ToolManifest, manifest: PackManifest | None
) -> list[Finding]:
    """Run every DESIGN.md section 5.2 rule over an already-parsed set of graphs."""
    return _Rules(graphs, tools, manifest).run()


@dataclass(slots=True)
class _Edge:
    """One outgoing edge of a point, with the label that selects it."""

    label: str
    target: Point


class _Rules:
    def __init__(
        self, graphs: dict[str, Graph], tools: ToolManifest, manifest: PackManifest | None
    ) -> None:
        self.graphs = graphs
        self.tools = tools
        self.manifest = manifest
        self.findings: list[Finding] = []
        self.jinja = make_environment()
        """This validation run's own Jinja environment: template caches and filter tables are
        never shared between packs (phase-1 deferred finding P2)."""

    # -- reporting -----------------------------------------------------------------------

    def report(
        self,
        severity: Severity,
        rule: str,
        message: str,
        *,
        graph: Graph | None = None,
        node: str | None = None,
        location: str | None = None,
    ) -> None:
        self.findings.append(
            Finding(
                severity=severity,
                rule=rule,
                message=message,
                location=location if location is not None else (graph.file if graph else None),
                node=node,
            )
        )

    def error(self, rule: str, message: str, **kwargs: object) -> None:
        self.report(Severity.ERROR, rule, message, **kwargs)  # type: ignore[arg-type]

    def warn(self, rule: str, message: str, **kwargs: object) -> None:
        self.report(Severity.WARNING, rule, message, **kwargs)  # type: ignore[arg-type]

    def info(self, rule: str, message: str, **kwargs: object) -> None:
        self.report(Severity.INFO, rule, message, **kwargs)  # type: ignore[arg-type]

    # -- entry point ---------------------------------------------------------------------

    def run(self) -> list[Finding]:
        self.tool_declaration_issues()
        for graph in self.graphs.values():
            self.graph_shape(graph)
            for node_id, node in graph.nodes.items():
                self.node_targets(graph, node_id, node)
                self.node_body(graph, node_id, node)
            self.reachability(graph)
            self.end_outputs(graph)
            self.intra_graph_cycles(graph)
        self.call_cycles()
        self.confirm_coverage()
        self.manifest_graph_references()
        self.not_executable_notice()
        self.exemptions_notice()
        return self.findings

    # -- 5.2: graph shape ----------------------------------------------------------------

    def graph_shape(self, graph: Graph) -> None:
        stem = graph.file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem != graph.id:
            self.error(
                "graph.id_mismatch",
                f"graph id {graph.id!r} does not match its file name {stem!r}; sub-graph "
                "references use the file name",
                graph=graph,
            )
        if graph.start not in graph.nodes:
            self.error(
                "graph.start_missing",
                f"start node {graph.start!r} is not defined in nodes",
                graph=graph,
            )
        state_fields = graph.state.model.model_fields
        for name in graph.inputs.model.model_fields:
            if name not in state_fields:
                self.warn(
                    "graph.input_not_in_state",
                    f"declared input {name!r} has no state field of the same name, so the value "
                    "the caller passes has nowhere to land and no node can read it",
                    graph=graph,
                )
        if not any(isinstance(node, EndNode) for node in graph.nodes.values()):
            self.error(
                "graph.no_end",
                "graph has no 'end' node, so a frame invoking it can never pop",
                graph=graph,
            )

    def node_targets(self, graph: Graph, node_id: str, node: NodeBase) -> None:
        for label, target in edge_targets(node):
            if target not in graph.nodes:
                self.error(
                    "graph.edge_target_missing",
                    f"edge {label!r} points at {target!r}, which is not a node in this graph",
                    graph=graph,
                    node=node_id,
                )
        for field_name, graph_id in graph_references(node):
            if graph_id not in self.graphs:
                rule = (
                    "graph.gate_redirect_unknown"
                    if field_name == "redirect"
                    else "graph.subgraph_unknown"
                )
                self.error(
                    rule,
                    f"{field_name} names graph {graph_id!r}, which the pack does not define",
                    graph=graph,
                    node=node_id,
                )
        # DESIGN.md section 6.2 fixes the edge labels of confirm and handoff; a node that omits
        # one has a decision the engine cannot route.
        labels: tuple[str, ...] = ()
        if isinstance(node, ConfirmNode):
            labels = ("yes", "no")
        elif isinstance(node, HandoffNode):
            labels = ("resumed", "closed")
        if labels:
            declared = getattr(node, "edges", {})
            missing = [label for label in labels if label not in declared]
            if missing:
                self.error(
                    "graph.edges_incomplete",
                    f"{node.type} node must declare edges {', '.join(labels)}; missing "
                    f"{', '.join(missing)}",
                    graph=graph,
                    node=node_id,
                )

    def reachability(self, graph: Graph) -> None:
        if graph.start not in graph.nodes:
            return
        seen = {graph.start}
        stack = [graph.start]
        while stack:
            current = stack.pop()
            for _label, target in edge_targets(graph.nodes[current]):
                if target in graph.nodes and target not in seen:
                    seen.add(target)
                    stack.append(target)
        for node_id in graph.nodes:
            if node_id not in seen:
                self.warn(
                    "graph.node_unreachable",
                    "node is not reachable from the start node",
                    graph=graph,
                    node=node_id,
                )

    # -- 5.2: no un-suspended loops ------------------------------------------------------

    def intra_graph_cycles(self, graph: Graph) -> None:
        """A cycle inside one graph that passes through no suspending node.

        Suspending nodes (``ask``, ``confirm``, ``handoff``, and ``tool`` with an async tool)
        are removed from the graph first, because a loop that waits for someone is a
        conversation, not a spin (DESIGN.md sections 5.2 and 7.2).
        """
        alive = {node_id: node for node_id, node in graph.nodes.items() if not self.suspends(node)}
        successors = {
            node_id: [t for _l, t in edge_targets(node) if t in alive]
            for node_id, node in alive.items()
        }
        for cycle in _find_cycles(successors):
            self.error(
                "graph.unsuspended_cycle",
                "these nodes form a loop with no node that waits for the customer, a human or "
                f"an async tool, so a single turn could spin forever: {' -> '.join(cycle)}",
                graph=graph,
                node=cycle[0],
            )

    def call_cycles(self) -> None:
        """Graph A reaching itself through ``subgraph`` or ``gate.redirect``, waiting on nothing."""
        successors = {
            graph_id: [
                target
                for node in graph.nodes.values()
                for _field, target in graph_references(node)
                if target in self.graphs
            ]
            for graph_id, graph in self.graphs.items()
        }
        for cycle in _find_cycles(successors):
            if any(self.graph_can_suspend(self.graphs[graph_id]) for graph_id in cycle):
                continue
            self.error(
                "graph.subgraph_cycle",
                "these graphs invoke each other in a loop and none of them contains a node that "
                f"waits: {' -> '.join(cycle)}",
                graph=self.graphs[cycle[0]],
            )

    def suspends(self, node: NodeBase) -> bool:
        spec = NODE_TYPES[node.type]
        if spec.suspends is not None:
            return True
        if spec.suspends_when_async_tool and isinstance(node, ToolNode):
            tool = self.tools.get(node.tool)
            return tool is not None and tool.declaration.async_
        return False

    def graph_can_suspend(self, graph: Graph) -> bool:
        return any(self.suspends(node) for node in graph.nodes.values())

    # -- per-node bodies -----------------------------------------------------------------

    def state_env(self, graph: Graph) -> dict[str, TypeInfo]:
        return model_type_env(state=graph.state.model, ctx=ConversationContext)

    def node_body(self, graph: Graph, node_id: str, node: NodeBase) -> None:
        match node:
            case RouterNode():
                self.router(graph, node_id, node)
            case SayNode():
                self.template(graph, node_id, node.message, field="message")
            case EndNode():
                pass  # handled by end_outputs, which needs the whole graph
            case SubgraphNode():
                self.subgraph(graph, node_id, node)
            case LlmNode():
                self.llm(graph, node_id, node)
            case AskNode():
                self.ask(graph, node_id, node)
            case ToolNode():
                self.tool_node(graph, node_id, node)
            case GateNode():
                self.gate(graph, node_id, node)
            case ConfirmNode():
                self.confirm(graph, node_id, node)
            case HandoffNode():
                pass

    def router(self, graph: Graph, node_id: str, node: RouterNode) -> None:
        env = self.state_env(graph)
        for predicate in node.edges:
            expression = self.expression(graph, node_id, predicate, field="edges")
            if expression is None:
                continue
            kind = self.typed(graph, node_id, expression, env, field="edges")
            if kind is not None and not kind.unknown and kind.category != "bool":
                self.warn(
                    "graph.router_predicate_not_bool",
                    f"router branch {predicate!r} is {kind.describe()}, not a boolean; it will be "
                    "taken on any truthy value",
                    graph=graph,
                    node=node_id,
                )
        if node.default is None:
            self.warn(
                "graph.router_no_default",
                "router has no 'default' target, so the turn dead-ends if no branch matches",
                graph=graph,
                node=node_id,
            )

    def gate(self, graph: Graph, node_id: str, node: GateNode) -> None:
        env = self.state_env(graph)
        expression = self.expression(graph, node_id, node.predicate, field="predicate")
        if expression is None:
            return
        kind = self.typed(graph, node_id, expression, env, field="predicate")
        if kind is not None and not kind.unknown and kind.category != "bool":
            self.warn(
                "graph.gate_predicate_not_bool",
                f"gate predicate is {kind.describe()}, not a boolean",
                graph=graph,
                node=node_id,
            )

    def ask(self, graph: Graph, node_id: str, node: AskNode) -> None:
        self.template(graph, node_id, node.prompt, field="prompt")
        fields = graph.state.model.model_fields
        for slot in node.slots:
            if slot not in fields:
                self.error(
                    "graph.ask_slot_unknown",
                    f"slot {slot!r} is not a declared state field; declared: "
                    f"{', '.join(sorted(fields)) or 'none'}",
                    graph=graph,
                    node=node_id,
                )

    def llm(self, graph: Graph, node_id: str, node: LlmNode) -> None:
        for tool_name in node.tools:
            spec = self.tools.get(tool_name)
            if spec is None:
                self.error(
                    "graph.llm_tool_unknown",
                    f"llm node lists tool {tool_name!r}, which the pack does not declare",
                    graph=graph,
                    node=node_id,
                )
            elif spec.risk not in MODEL_CALLABLE:
                self.error(
                    "graph.llm_tool_not_read",
                    f"llm node lists {tool_name!r}, which is {spec.risk.value} risk; DESIGN.md "
                    "section 8.2 allows only read-tier tools inside a model tool loop",
                    graph=graph,
                    node=node_id,
                )
        if node.knowledge is not None:
            self.template(graph, node_id, node.knowledge.query, field="knowledge.query")
        built = build_model(f"{node_id.title()}Output", node.output_schema)
        for issue in built.issues:
            if issue.code == "unresolved_type":
                continue  # pack tool models are stubs until phase 4; already warned on state
            self.error(
                "graph.llm_output_schema_invalid",
                f"output_schema.{issue.field}: {issue.message}",
                graph=graph,
                node=node_id,
            )
        fields = graph.state.model.model_fields
        for slot in node.output_schema:
            if slot not in fields:
                self.warn(
                    "graph.llm_output_not_in_state",
                    f"output_schema field {slot!r} is not a declared state field, so the value "
                    "the model produces has nowhere to be stored",
                    graph=graph,
                    node=node_id,
                )

    def confirm(self, graph: Graph, node_id: str, node: ConfirmNode) -> None:
        self.template(graph, node_id, node.prompt, field="prompt")
        spec = self.tools.get(node.action.tool)
        if spec is None:
            self.error(
                "graph.confirm_action_tool_unknown",
                f"confirm proposes tool {node.action.tool!r}, which the pack does not declare",
                graph=graph,
                node=node_id,
            )
            return
        if spec.risk is Risk.READ:
            self.warn(
                "graph.confirm_action_is_read",
                f"confirm proposes {spec.name!r}, a read-tier tool with no side effect; a "
                "confirmation the customer cannot meaningfully refuse trains them to say yes",
                graph=graph,
                node=node_id,
            )
        self.tool_arguments(graph, node_id, spec, node.action.args, field="action.args")

    def tool_node(self, graph: Graph, node_id: str, node: ToolNode) -> None:
        spec = self.tools.get(node.tool)
        if spec is None:
            hint = (
                ""
                if self.tools.present
                else " (the pack has no tools/tools.yaml, so it declares no tools at all)"
            )
            self.error(
                "graph.tool_unknown",
                f"tool node references {node.tool!r}, which the pack does not declare{hint}",
                graph=graph,
                node=node_id,
            )
            return
        self.tool_arguments(graph, node_id, spec, node.args, field="args")
        self.tool_into(graph, node_id, spec, node)
        self.approval_binding(graph, node_id, spec, node)

    def tool_arguments(
        self, graph: Graph, node_id: str, spec: ToolSpec, args: dict[str, Scalar], *, field: str
    ) -> None:
        env = self.state_env(graph)
        declared = spec.input_model.model_fields
        for name, raw in args.items():
            if name not in declared:
                self.error(
                    "graph.tool_arg_unknown",
                    f"{field}.{name} is not an input of tool {spec.name!r}; declared inputs: "
                    f"{', '.join(sorted(declared)) or 'none'}",
                    graph=graph,
                    node=node_id,
                )
                continue
            value = self.value(graph, node_id, raw, field=f"{field}.{name}")
            if value is None:
                continue
            target = from_annotation(declared[name].annotation)
            self.assignable(graph, node_id, value, target, what=f"{field}.{name}", env=env)
        for name, info in declared.items():
            if info.is_required() and name not in args:
                self.error(
                    "graph.tool_arg_missing",
                    f"tool {spec.name!r} requires input {name!r}, which {field} does not supply",
                    graph=graph,
                    node=node_id,
                )

    def tool_into(self, graph: Graph, node_id: str, spec: ToolSpec, node: ToolNode) -> None:
        state_fields = graph.state.model.model_fields
        env = dict(self.state_env(graph))
        env["result"] = TypeInfo(annotation=spec.output_model)
        if node.into is None:
            return
        if isinstance(node.into, str):
            target = node.into
            if not target.startswith("state."):
                self.error(
                    "graph.tool_into_invalid",
                    f"into must name a state field (for example 'state.charge'), got {target!r}",
                    graph=graph,
                    node=node_id,
                )
                return
            field_name = target[len("state.") :]
            if field_name not in state_fields:
                self.error(
                    "graph.tool_into_invalid",
                    f"into targets state field {field_name!r}, which is not declared",
                    graph=graph,
                    node=node_id,
                )
            return
        for field_name, raw in node.into.items():
            if field_name not in state_fields:
                self.error(
                    "graph.tool_into_invalid",
                    f"into targets state field {field_name!r}, which is not declared",
                    graph=graph,
                    node=node_id,
                )
                continue
            value = self.value(graph, node_id, raw, field=f"into.{field_name}")
            if value is None:
                continue
            target_type = from_annotation(state_fields[field_name].annotation)
            self.assignable(graph, node_id, value, target_type, what=f"into.{field_name}", env=env)

    # -- 8.2: approval binding -----------------------------------------------------------

    def approval_binding(self, graph: Graph, node_id: str, spec: ToolSpec, node: ToolNode) -> None:
        if not spec.needs_confirm:
            if node.requires_approval is not None:
                self.info(
                    "graph.approval_not_needed",
                    f"requires_approval is declared but {spec.name!r} is {spec.risk.value} risk "
                    "and needs no approval",
                    graph=graph,
                    node=node_id,
                )
            return
        if node.requires_approval is None:
            self.error(
                "graph.approval_missing",
                f"tool {spec.name!r} is {spec.risk.value} risk, so the node must name the confirm "
                f"node whose ActionApproval binds it (DESIGN.md section 8.2: requires_approval). "
                f"That confirm must be a node in this graph ({graph.id!r}); {SAME_GRAPH_REASON}",
                graph=graph,
                node=node_id,
            )
            return
        confirm = graph.nodes.get(node.requires_approval)
        if not isinstance(confirm, ConfirmNode):
            self.error(
                "graph.approval_unknown",
                f"requires_approval names {node.requires_approval!r}, which is not a confirm node "
                f"in this graph ({graph.id!r}). A confirm in a calling graph cannot be named here: "
                f"{SAME_GRAPH_REASON} Move the confirm into this graph",
                graph=graph,
                node=node_id,
            )
            return
        if confirm.action.tool != node.tool:
            self.error(
                "graph.approval_mismatch",
                f"confirm {node.requires_approval!r} approves tool {confirm.action.tool!r} but "
                f"this node calls {node.tool!r}; the approval hash could never match",
                graph=graph,
                node=node_id,
            )
            return
        approved = _canonical_args(confirm.action.args)
        called = _canonical_args(node.args)
        if approved != called:
            self.error(
                "graph.approval_mismatch",
                f"confirm {node.requires_approval!r} approves arguments {approved} but this node "
                f"calls with {called}; DESIGN.md section 8.2 hashes the arguments, so the call "
                "would be refused at run time",
                graph=graph,
                node=node_id,
            )

    # -- 5.2: confirm on all paths -------------------------------------------------------

    def confirm_coverage(self) -> None:
        """Every write or high-risk tool node has a ``confirm`` on all paths from customer input.

        Two forward *must*-analyses over the interprocedural control-flow graph, computed
        together to a fixpoint:

        ``guarded`` (a boolean)
            "some confirm node lies on every path from the last customer input to here".
            This is DESIGN.md section 5.2's rule.

        ``covered`` (a set of confirm points)
            "*these* confirm nodes lie on every such path". This is what DESIGN.md section 8.2
            needs, because an approval is bound to one action's arguments: a confirm for a
            different action does not authorise this call.

        ``confirming_graphs`` (a set of graph ids)
            "every such path passes a confirm belonging to *each of these graphs*". A WRITE or
            HIGH tool node is discharged only when its own graph is in that set, because
            DESIGN.md section 8.2 requires the approving confirm to be a node in the same graph
            as the tool node (see :data:`SAME_GRAPH_REASON`). A confirm in a calling graph
            therefore no longer discharges a callee's tool node, even though the analysis is
            interprocedural and can see it.

        Transfer function, identical for all three:

        * graph entry contributes nothing (a conversation begins with a customer message),
        * an ``ask`` node clears it (the customer spoke again since any earlier approval),
        * a ``confirm`` node's ``yes`` edge produces exactly itself and its ``no`` edge nothing,
        * every other node passes its incoming value through,
        * a point's incoming value is the *meet* (``and`` / intersection) over its predecessors,
          so a value survives only when it holds on **every** path.

        Two confirms on two different branches therefore leave ``guarded`` true but ``covered``
        empty: there is always a confirmation, but no single approval covers the call. That is
        ``graph.approval_unreachable``, not ``graph.unconfirmed_write``.
        """
        successors, entries = self.control_flow_graph()
        if not successors:
            return
        predecessors: dict[Point, list[tuple[Point, str]]] = {p: [] for p in successors}
        for point, edges in successors.items():
            for edge in edges:
                predecessors.setdefault(edge.target, []).append((point, edge.label))

        universe = frozenset(
            point for point in successors if isinstance(self.node_at(point), ConfirmNode)
        )
        graph_universe = frozenset(point[0] for point in universe)
        covered: dict[Point, frozenset[Point]] = {
            point: (frozenset() if point in entries else universe) for point in successors
        }
        confirming_graphs: dict[Point, frozenset[str]] = {
            point: (frozenset() if point in entries else graph_universe) for point in successors
        }
        guarded: dict[Point, bool] = {point: point not in entries for point in successors}
        reachable = self.reachable_points(successors, entries)

        changed = True
        rounds = 0
        limit = len(successors) * (len(universe) + len(graph_universe) + 3) + 2
        while changed and rounds < limit:
            changed = False
            rounds += 1
            for point in successors:
                at_entry = point in entries
                incoming: frozenset[Point] | None = frozenset() if at_entry else None
                incoming_graphs: frozenset[str] | None = frozenset() if at_entry else None
                incoming_guard: bool | None = False if at_entry else None
                for pred, label in predecessors.get(point, []):
                    if pred not in reachable:
                        continue
                    out = self.covered_out(pred, covered[pred], label)
                    incoming = out if incoming is None else (incoming & out)
                    out_graphs = self.confirming_graphs_out(pred, confirming_graphs[pred], label)
                    incoming_graphs = (
                        out_graphs if incoming_graphs is None else (incoming_graphs & out_graphs)
                    )
                    out_guard = self.guarded_out(pred, guarded[pred], label)
                    incoming_guard = (
                        out_guard if incoming_guard is None else (incoming_guard and out_guard)
                    )
                if incoming is None:
                    incoming = frozenset() if at_entry else universe
                if incoming_graphs is None:
                    incoming_graphs = frozenset() if at_entry else graph_universe
                if incoming_guard is None:
                    incoming_guard = not at_entry
                if incoming != covered[point]:
                    covered[point] = incoming
                    changed = True
                if incoming_graphs != confirming_graphs[point]:
                    confirming_graphs[point] = incoming_graphs
                    changed = True
                if incoming_guard != guarded[point]:
                    guarded[point] = incoming_guard
                    changed = True

        for point in sorted(reachable):
            node = self.node_at(point)
            if not isinstance(node, ToolNode):
                continue
            spec = self.tools.get(node.tool)
            if spec is None or not spec.needs_confirm:
                continue
            graph = self.graphs[point[0]]
            if not guarded[point]:
                self.error(
                    "graph.unconfirmed_write",
                    f"tool {node.tool!r} is {spec.risk.value} risk but there is a path from the "
                    "last customer input to this node with no confirm node on it "
                    "(DESIGN.md section 5.2)",
                    graph=graph,
                    node=point[1],
                )
            elif point[0] not in confirming_graphs[point]:
                elsewhere = ", ".join(sorted(confirming_graphs[point]))
                where = (
                    f"the confirm(s) that do cover it live in graph(s) {elsewhere}"
                    if elsewhere
                    else "the confirms that cover it are in other graphs and differ per path"
                )
                self.error(
                    "graph.unconfirmed_write",
                    f"tool {node.tool!r} is {spec.risk.value} risk and every path to it passes a "
                    f"confirm, but none of those confirms is a node in this graph "
                    f"({point[0]!r}): {where}. {SAME_GRAPH_REASON} Move the confirm into "
                    f"{point[0]!r}, next to the call it authorises",
                    graph=graph,
                    node=point[1],
                )
            elif (
                node.requires_approval is not None
                and (point[0], node.requires_approval) not in covered[point]
            ):
                self.error(
                    "graph.approval_unreachable",
                    f"requires_approval names {node.requires_approval!r}, but that confirm is not "
                    "on every path to this node, so the approval is missing on at least one path",
                    graph=graph,
                    node=point[1],
                )
            self.confirm_reentry(point, node, spec)

    def confirm_reentry(self, point: Point, node: ToolNode, spec: ToolSpec) -> None:
        """A cycle back into a confirmed tool node that passes neither a ``confirm`` nor an ``ask``.

        DESIGN.md section 8.2 binds one ``ActionApproval`` to one proposed action. A loop that
        re-enters the call without a fresh customer decision lets a single approval authorise
        unbounded calls, and the run-time hash check cannot see it because the arguments never
        change (phase-0 deferred finding N1, phase-1 review finding F4).

        Intra-graph only, which is sufficient: by the same-graph rule the approving confirm is a
        node in this graph, and a path that leaves the graph can only come back through this
        graph's ``start`` or through the call site it left from, both of which are on the
        intra-graph edges walked here.
        """
        graph = self.graphs[point[0]]
        seen: set[str] = set()
        stack = [target for _label, target in edge_targets(node) if target in graph.nodes]
        while stack:
            current = stack.pop()
            if current == point[1]:
                self.error(
                    "graph.approval_reused",
                    f"tool {node.tool!r} is {spec.risk.value} risk and this node is reachable "
                    "from itself without passing a confirm or an ask, so one ActionApproval "
                    "would authorise every call the loop makes; the arguments never change, so "
                    "the run-time hash check cannot catch it either. Put the confirm inside the "
                    "loop, or break the cycle",
                    graph=graph,
                    node=point[1],
                )
                return
            if current in seen:
                continue
            seen.add(current)
            here = graph.nodes[current]
            if isinstance(here, ConfirmNode | AskNode):
                continue  # the customer decides again on this path
            stack.extend(t for _label, t in edge_targets(here) if t in graph.nodes)

    def covered_out(self, point: Point, incoming: frozenset[Point], label: str) -> frozenset[Point]:
        node = self.node_at(point)
        if isinstance(node, ConfirmNode):
            # The customer's "yes" is itself the last customer input, and it approves exactly
            # this action: earlier approvals do not survive it.
            return frozenset({point}) if label == "yes" else frozenset()
        if isinstance(node, AskNode):
            return frozenset()
        return incoming

    def confirming_graphs_out(
        self, point: Point, incoming: frozenset[str], label: str
    ) -> frozenset[str]:
        """The graph-id twin of :meth:`covered_out`.

        A confirm contributes only *its own* graph, so intersecting over predecessors leaves a
        graph id in the set exactly when every path passes a confirm defined in that graph.
        """
        node = self.node_at(point)
        if isinstance(node, ConfirmNode):
            return frozenset({point[0]}) if label == "yes" else frozenset()
        if isinstance(node, AskNode):
            return frozenset()
        return incoming

    def guarded_out(self, point: Point, incoming: bool, label: str) -> bool:
        """The boolean twin of :meth:`covered_out`: is anything confirmed on the way out?"""
        node = self.node_at(point)
        if isinstance(node, ConfirmNode):
            return label == "yes"
        if isinstance(node, AskNode):
            return False
        return incoming

    def node_at(self, point: Point) -> NodeBase | None:
        graph = self.graphs.get(point[0])
        return graph.nodes.get(point[1]) if graph else None

    def control_flow_graph(self) -> tuple[dict[Point, list[_Edge]], set[Point]]:
        """Build the interprocedural CFG over ``(graph id, node id)`` points.

        ``subgraph`` and ``gate`` calls are inlined: a call point gets an edge into the callee's
        start, and every ``end`` of the callee gets an edge back to the caller's continuation.
        The return edges are context-insensitive (an ``end`` returns to *every* call site of its
        graph), which over-approximates the paths. For a must-analysis that is the safe
        direction: more predecessors can only shrink the covered set, so the analysis may report
        a violation that a context-sensitive one would not, and can never hide one.
        """
        successors: dict[Point, list[_Edge]] = {}
        returns: dict[str, list[Point]] = {}
        for graph_id, graph in self.graphs.items():
            for node_id, node in graph.nodes.items():
                point = (graph_id, node_id)
                edges = [
                    _Edge(label, (graph_id, target))
                    for label, target in edge_targets(node)
                    if target in graph.nodes
                ]
                if isinstance(node, SubgraphNode) and node.graph in self.graphs:
                    callee = self.graphs[node.graph]
                    if callee.start in callee.nodes:
                        edges.append(_Edge("call", (node.graph, callee.start)))
                    returns.setdefault(node.graph, []).append(point)
                if isinstance(node, GateNode) and node.redirect in self.graphs:
                    callee = self.graphs[node.redirect]
                    if callee.start in callee.nodes:
                        edges.append(_Edge("redirect", (node.redirect, callee.start)))
                    returns.setdefault(node.redirect, []).append(point)
                successors[point] = edges

        for callee_id, call_sites in returns.items():
            callee = self.graphs[callee_id]
            for node_id, node in callee.nodes.items():
                if not isinstance(node, EndNode):
                    continue
                end_point = (callee_id, node_id)
                for call_site in call_sites:
                    caller = self.graphs[call_site[0]]
                    call_node = caller.nodes[call_site[1]]
                    if isinstance(call_node, SubgraphNode):
                        if call_node.next in caller.nodes:
                            successors[end_point].append(
                                _Edge("return", (call_site[0], call_node.next))
                            )
                    else:
                        # A gate re-evaluates its predicate when the redirect frame pops (6.2).
                        successors[end_point].append(_Edge("return", call_site))

        entries: set[Point] = set()
        entry_graph = self.manifest.entry_graph if self.manifest else None
        starts: dict[str, Point] = {
            graph_id: (graph_id, graph.start)
            for graph_id, graph in self.graphs.items()
            if graph.start in graph.nodes
        }
        for graph_id, start in starts.items():
            if graph_id == entry_graph or graph_id not in returns:
                # The pack's entry graph always starts from a customer message; a graph nobody
                # calls is checked on its own terms rather than skipped.
                entries.add(start)

        # A graph whose only call sites are themselves unreachable would otherwise be analysed
        # by nothing at all: it is not an entry (it *is* called) and no reachable path enters
        # it. Promote such a callee to an entry and repeat, because promoting one graph can
        # make another graph's call sites reachable. Entries only grow, so this terminates.
        while True:
            reachable = self.reachable_points(successors, entries)
            promoted = {
                starts[callee_id]
                for callee_id, call_sites in returns.items()
                if callee_id in starts
                and starts[callee_id] not in entries
                and not any(site in reachable for site in call_sites)
            }
            if not promoted:
                return successors, entries
            entries |= promoted

    def reachable_points(
        self, successors: dict[Point, list[_Edge]], entries: set[Point]
    ) -> set[Point]:
        seen = set(entries)
        stack = list(entries)
        while stack:
            point = stack.pop()
            for edge in successors.get(point, []):
                if edge.target not in seen:
                    seen.add(edge.target)
                    stack.append(edge.target)
        return seen

    # -- 5.2: sub-graph mappings ---------------------------------------------------------

    def subgraph(self, graph: Graph, node_id: str, node: SubgraphNode) -> None:
        callee = self.graphs.get(node.graph)
        if callee is None:
            return  # already reported by node_targets
        env = self.state_env(graph)
        declared_inputs = callee.inputs.model.model_fields
        for name, raw in node.inputs.items():
            if name not in declared_inputs:
                self.error(
                    "graph.subgraph_input_unknown",
                    f"{node.graph!r} declares no input {name!r}; declared inputs: "
                    f"{', '.join(sorted(declared_inputs)) or 'none'}",
                    graph=graph,
                    node=node_id,
                )
                continue
            value = self.value(graph, node_id, raw, field=f"inputs.{name}")
            if value is None:
                continue
            target = from_annotation(declared_inputs[name].annotation)
            self.assignable(graph, node_id, value, target, what=f"inputs.{name}", env=env)
        for name, info in declared_inputs.items():
            if info.is_required() and name not in node.inputs:
                self.error(
                    "graph.subgraph_input_missing",
                    f"{node.graph!r} requires input {name!r}, which this node does not map",
                    graph=graph,
                    node=node_id,
                )

        declared_outputs = callee.outputs.model.model_fields
        state_fields = graph.state.model.model_fields
        for state_field, output_name in node.outputs.items():
            if state_field not in state_fields:
                self.error(
                    "graph.subgraph_output_field_unknown",
                    f"outputs maps into state field {state_field!r}, which this graph does not "
                    "declare",
                    graph=graph,
                    node=node_id,
                )
                continue
            if output_name not in declared_outputs:
                self.error(
                    "graph.subgraph_output_unknown",
                    f"{node.graph!r} declares no output {output_name!r}; declared outputs: "
                    f"{', '.join(sorted(declared_outputs)) or 'none'}",
                    graph=graph,
                    node=node_id,
                )
                continue
            source = from_annotation(declared_outputs[output_name].annotation)
            target = from_annotation(state_fields[state_field].annotation)
            message = _incompatible(source, target)
            if message is not None:
                self.error(
                    "graph.subgraph_output_type",
                    f"output {output_name!r} is {source.describe()} but state field "
                    f"{state_field!r} is {target.describe()}: {message}",
                    graph=graph,
                    node=node_id,
                )

    # -- end outputs ---------------------------------------------------------------------

    def end_outputs(self, graph: Graph) -> None:
        declared = graph.outputs.model.model_fields
        env = self.state_env(graph)
        end_nodes = {
            node_id: node for node_id, node in graph.nodes.items() if isinstance(node, EndNode)
        }
        for node_id, node in end_nodes.items():
            for name, raw in node.outputs.items():
                if name not in declared:
                    self.error(
                        "graph.end_output_unknown",
                        f"end node produces {name!r}, which the graph does not declare in "
                        f"outputs; declared: {', '.join(sorted(declared)) or 'none'}",
                        graph=graph,
                        node=node_id,
                    )
                    continue
                value = self.value(graph, node_id, raw, field=f"outputs.{name}")
                if value is None:
                    continue
                target = from_annotation(declared[name].annotation)
                self.assignable(graph, node_id, value, target, what=f"outputs.{name}", env=env)
            missing = [
                name
                for name, info in declared.items()
                if info.is_required() and name not in node.outputs
            ]
            if missing:
                self.error(
                    "graph.end_output_missing",
                    f"graph declares required output(s) {', '.join(sorted(missing))} that this "
                    "end node does not produce",
                    graph=graph,
                    node=node_id,
                )

    # -- expression, template and assignment helpers -------------------------------------

    def expression(self, graph: Graph, node_id: str, raw: str, *, field: str) -> Expr | None:
        try:
            return parse(raw)
        except ParseError as exc:
            self.error("expr.parse_error", f"{field}: {exc}", graph=graph, node=node_id)
            return None

    def value(self, graph: Graph, node_id: str, raw: Scalar, *, field: str) -> "_TypedValue | None":
        try:
            value = parse_value(raw)
        except ParseError as exc:
            self.error("expr.parse_error", f"{field}: {exc}", graph=graph, node=node_id)
            return None
        except ValueLooksLikeExpression as exc:
            self.error("expr.looks_like_expression", f"{field}: {exc}", graph=graph, node=node_id)
            return None
        return _TypedValue(raw=value.raw, expression=value.expression, literal=value.literal)

    def typed(
        self, graph: Graph, node_id: str, expression: Expr, env: TypeEnv, *, field: str
    ) -> TypeInfo | None:
        notes: list[TypeNote] = []
        try:
            kind = infer(expression, env, notes)
        except TypeError_ as exc:
            self.error("expr.type_error", f"{field}: {exc}", graph=graph, node=node_id)
            return None
        self.notes(graph, node_id, notes, field=field)
        return kind

    def notes(self, graph: Graph, node_id: str, notes: Iterable[TypeNote], *, field: str) -> None:
        for note in notes:
            self.warn(f"expr.{note.code}", f"{field}: {note.message}", graph=graph, node=node_id)

    def assignable(
        self,
        graph: Graph,
        node_id: str,
        value: "_TypedValue",
        target: TypeInfo,
        *,
        what: str,
        env: TypeEnv,
    ) -> None:
        if value.expression is not None:
            source = self.typed(graph, node_id, value.expression, env, field=what)
            if source is None:
                return
        else:
            source = _literal_type_info(value.literal)
        message = _incompatible(source, target)
        if message is not None:
            self.error(
                "expr.type_error",
                f"{what}: {source.describe()} cannot be assigned to {target.describe()}: {message}",
                graph=graph,
                node=node_id,
            )
            return
        if source.optional and not target.optional and not target.unknown:
            self.warn(
                "graph.assignment_optional",
                f"{what}: {source.describe()} may be None but the target is "
                f"{target.describe()}, which is not optional",
                graph=graph,
                node=node_id,
            )

    def template(self, graph: Graph, node_id: str, source: str, *, field: str) -> None:
        issues, notes = validate_template(source, self.state_env(graph), jinja_env=self.jinja)
        for issue in issues:
            self.error(
                f"template.{issue.code}",
                f"{field}: {issue.message}",
                graph=graph,
                node=node_id,
            )
        self.notes(graph, node_id, notes, field=field)

    # -- pack-level notices --------------------------------------------------------------

    def tool_declaration_issues(self) -> None:
        for spec in self.tools.tools.values():
            for issue in spec.issues:
                self.warn("tools.declaration_unresolved", issue, location="tools/tools.yaml")

    def exemptions_notice(self) -> None:
        """DESIGN.md section 8.2: "The validator lists every exemption in its report"."""
        for spec in self.tools.exemptions:
            if spec.risk is Risk.HIGH:
                self.error(
                    "tools.high_risk_exempt",
                    f"tool {spec.name!r} is high risk and marked confirm_exempt; DESIGN.md "
                    "section 8.2 offers the exemption for write-tier tools only, and a high-tier "
                    "tool moves money or access. Reclassify the tool or drop the exemption",
                    location="tools/tools.yaml",
                )
                continue
            self.info(
                "graph.confirm_exempt",
                f"tool {spec.name!r} is {spec.risk.value} risk and marked confirm_exempt, so no "
                "confirm node is required before it; review this deliberately",
                location="tools/tools.yaml",
            )

    def manifest_graph_references(self) -> None:
        if self.manifest is None:
            return
        interrupts = self.manifest.interrupts
        for field_name, graph_ids in (
            ("allowed_from", interrupts.allowed_from),
            ("blocked_in", interrupts.blocked_in),
        ):
            for graph_id in graph_ids:
                if graph_id not in self.graphs:
                    self.warn(
                        "manifest.interrupt_graph_unknown",
                        f"interrupts.{field_name} names graph {graph_id!r}, which the pack does "
                        "not define",
                        location="pack.yaml",
                    )

    def not_executable_notice(self) -> None:
        used: dict[str, int] = {}
        for graph in self.graphs.values():
            for node in graph.nodes.values():
                if not NODE_TYPES[node.type].executable:
                    used[node.type] = used.get(node.type, 0) + 1
        for node_type, count in sorted(used.items()):
            phase = NODE_TYPES[node_type].executable_phase
            self.info(
                "graph.node_not_executable",
                f"{count} {node_type!r} node(s): validated but not executable yet; core runs "
                f"them from phase {phase}",
            )


@dataclass(frozen=True, slots=True)
class _TypedValue:
    raw: str
    expression: Expr | None
    literal: object


def _literal_type_info(literal: object) -> TypeInfo:
    """The type of a literal, remembering its value so a ``Literal[...]`` target can check it."""
    if literal is None:
        return TypeInfo(annotation=type(None), optional=True)
    if isinstance(literal, bool):
        return TypeInfo(annotation=bool, literal_values=(literal,))
    if isinstance(literal, int):
        return TypeInfo(annotation=int, literal_values=(literal,))
    if isinstance(literal, float):
        return TypeInfo(annotation=float)
    return TypeInfo(annotation=str, literal_values=(literal,))


def _incompatible(source: TypeInfo, target: TypeInfo) -> str | None:
    """``None`` when a value of type ``source`` may be stored in ``target``."""
    if source.unknown or target.unknown:
        return None
    if source.category == "none":
        return None if target.optional else "the target is not optional"
    if source.category != target.category:
        return f"{source.category} is not {target.category}"
    if target.literal_values is not None and source.literal_values is None:
        return None  # a same-category expression may or may not be in the set; not decidable
    if target.literal_values is not None and source.literal_values is not None:
        extra = [v for v in source.literal_values if v not in target.literal_values]
        if extra:
            allowed = ", ".join(repr(v) for v in target.literal_values)
            return f"{extra!r} is not one of {allowed}"
    return None


def _canonical_args(args: dict[str, Scalar]) -> str:
    """Canonical text for an argument mapping, so whitespace differences are not a mismatch.

    Classification goes through :func:`parse_value`, the *same* decision the engine will make
    when it evaluates the node, so ``{amount: 100}`` (a YAML int) and ``{amount: "100"}`` (a
    string literal) do not canonicalise alike. Comparing them by a different rule than the one
    the run time uses would pass here and then fail DESIGN.md 8.2's hash check at run time,
    which is the failure the static rule exists to prevent (phase-1 review finding F7).
    """
    parts = []
    for name in sorted(args):
        raw = args[name]
        try:
            value = parse_value(raw)
        except (ParseError, ValueLooksLikeExpression):
            # Already reported as expr.parse_error / expr.looks_like_expression by the node
            # rules; fall back to the raw text so the comparison stays deterministic.
            parts.append(f"{name}=<unparsable {raw!r}>")
            continue
        if value.expression is not None:
            parts.append(f"{name}={unparse(value.expression)}")
        else:
            parts.append(f"{name}={literal_source(value.literal)}")
    return "{" + ", ".join(parts) + "}"


def _find_cycles(successors: dict[str, list[str]]) -> list[list[str]]:
    """Every strongly connected component with at least one edge, as a sorted node list.

    Iterative Tarjan, so a very large graph cannot blow the Python stack.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    components: list[list[str]] = []

    for root in successors:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child = work[-1]
            if child == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            recursed = False
            children = successors.get(node, [])
            while child < len(children):
                target = children[child]
                child += 1
                if target not in index:
                    work[-1] = (node, child)
                    work.append((target, 0))
                    recursed = True
                    break
                if target in on_stack:
                    low[node] = min(low[node], index[target])
            if recursed:
                continue
            work[-1] = (node, child)
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                has_edge = len(component) > 1 or node in successors.get(node, [])
                if has_edge:
                    components.append(sorted(component))
            work.pop()
            if work:
                parent, parent_child = work[-1]
                low[parent] = min(low[parent], low[node])
                work[-1] = (parent, parent_child)
    return components
