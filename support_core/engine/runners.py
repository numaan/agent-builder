"""Node behaviour. Implements the :class:`~support_core.engine.types.Node` protocol of
DESIGN.md section 6.3 for the node types core can run today.

One runner instance per node in a graph. Runners are the *behaviour* half of a node type; the
Pydantic models in :mod:`support_core.graph.nodes` are the *configuration* half that the
validator reads. Keeping them apart is what lets the validator type-check an ``llm`` node in
phase 1 while the engine refuses to run it until phase 3, from one declaration.

A runner never touches storage (DESIGN.md section 6.3: "Nodes never touch storage directly").
It reads the frame's state and the conversation context, and returns a
:class:`~support_core.engine.types.NodeResult`. The executor decides what that means for the
frame stack and writes the checkpoint.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel

from support_core.engine.errors import NodeError, NodeNotExecutableError
from support_core.engine.hooks import EngineHooks
from support_core.engine.types import (
    Frame,
    GraphInvocation,
    NodeResult,
    OutboundMessage,
    ResumeEvent,
    SuspendReason,
)
from support_core.graph.context import ConversationContext
from support_core.graph.expr import EvaluationError, evaluate, parse
from support_core.graph.expr.syntax import Expr
from support_core.graph.nodes import (
    NODE_TYPES,
    AskNode,
    EndNode,
    GateNode,
    NodeBase,
    NodeTypeSpec,
    RouterNode,
    SayNode,
    Scalar,
    SubgraphNode,
    edge_targets,
)
from support_core.graph.schema import Graph, parse_value
from support_core.graph.templates import TemplateError, render

DEFAULT_EDGE = "default"
"""The edge label a ``router`` returns when no predicate matched. Not a possible predicate:
a predicate must start with a root, so ``default`` never parses as one."""


@dataclass(slots=True)
class NodeRuntime:
    """What a node can reach outside itself (DESIGN.md section 6.3).

    Phase 2 gives nodes the pack, their frame, their step id and the engine hooks. The LLM
    layer (phase 3), retrieval (phase 5) and tool invocation (phase 4) arrive as further
    attributes here, which is why nodes take ``rt`` rather than the individual pieces.
    """

    graph: Graph
    frame: Frame
    step_id: str
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    hooks: EngineHooks
    environment: SandboxedEnvironment
    """The *pack's* Jinja environment, never a shared one (phase-1 deferred finding P2)."""

    def render(self, source: str, scope: Mapping[str, Any]) -> str:
        return render(source, dict(scope), env=self.environment)


def scope_of(state: BaseModel, ctx: ConversationContext) -> dict[str, Any]:
    """The roots an expression or template may read (DESIGN.md section 6.4).

    ``result`` is absent: it only exists inside a ``tool`` node's ``into`` mapping (phase 4).
    """
    return {"state": state, "ctx": ctx}


def value_of(raw: Scalar, scope: Mapping[str, Any]) -> Any:
    """Evaluate one ``args``/``inputs``/``outputs`` entry, expression or literal."""
    value = parse_value(raw)
    if value.expression is None:
        return value.literal
    return evaluate(value.expression, scope)


class _Runner:
    """Common shape: an id, a type, and the failure behaviour for a node that cannot resume."""

    __slots__ = ("id", "type")

    def __init__(self, node_id: str, node: NodeBase) -> None:
        self.id = node_id
        self.type = node.type

    async def run(
        self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime
    ) -> NodeResult:  # pragma: no cover - every concrete runner overrides this
        raise NotImplementedError

    async def resume(
        self,
        state: BaseModel,
        ctx: ConversationContext,
        rt: NodeRuntime,
        event: ResumeEvent,
    ) -> NodeResult:
        """A node that never suspends cannot be resumed.

        Reaching this means the run was suspended at a node that did not ask to be, which is a
        corrupted frame stack rather than a run-time failure, so it is not routed to
        ``on_error``.
        """
        msg = f"{self.id}: a {self.type!r} node does not suspend, so it cannot be resumed"
        raise NodeError(msg)


class SayRunner(_Runner):
    """Emit a templated message with no LLM call (DESIGN.md section 6.2)."""

    __slots__ = ("node",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, SayNode)
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        try:
            text = rt.render(self.node.message, scope_of(state, ctx)).strip()
        except TemplateError as exc:
            raise NodeError(f"{self.id}: message template failed: {exc}") from exc
        return NodeResult(outbound=[OutboundMessage(text=text)])


class RouterRunner(_Runner):
    """First branch whose predicate is true wins; declaration order is the tie-break."""

    __slots__ = ("node", "predicates")

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, RouterNode)
        self.node = node
        self.predicates: list[tuple[str, Expr]] = [(source, parse(source)) for source in node.edges]

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        scope = scope_of(state, ctx)
        for source, expression in self.predicates:
            try:
                matched = bool(evaluate(expression, scope))
            except EvaluationError as exc:
                raise NodeError(f"{self.id}: predicate {source!r} failed: {exc}") from exc
            if matched:
                return NodeResult(next_edge=source)
        if self.node.default is None:
            msg = (
                f"{self.id}: no router branch matched and there is no default "
                f"(graph.router_no_default warned about this at validation time)"
            )
            raise NodeError(msg)
        return NodeResult(next_edge=DEFAULT_EDGE)


class GateRunner(_Runner):
    """Assert a predicate; on false push the redirect graph and re-evaluate.

    DESIGN.md section 6.2: "Assert a predicate. If false, push the ``redirect`` graph as a
    sub-frame, then re-evaluate." The re-evaluation is what the ``return_node`` does: the
    pushed frame returns to *this* node, not to the node after it, so the predicate is checked
    again with whatever the redirect changed.
    """

    __slots__ = ("node", "predicate")

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, GateNode)
        self.node = node
        self.predicate = parse(node.predicate)

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        return self.check(state, ctx)

    def check(self, state: BaseModel, ctx: ConversationContext) -> NodeResult:
        """Evaluate the predicate. Used by the executor for the re-check on frame entry too."""
        try:
            satisfied = bool(evaluate(self.predicate, scope_of(state, ctx)))
        except EvaluationError as exc:
            raise NodeError(
                f"{self.id}: gate predicate {self.node.predicate!r} failed: {exc}"
            ) from exc
        if satisfied:
            return NodeResult()
        return NodeResult(
            push_graph=GraphInvocation(
                graph=self.node.redirect, kind="gate_redirect", return_node=self.id
            )
        )


class SubgraphRunner(_Runner):
    """Invoke another graph with input and output mapping (DESIGN.md section 6.2)."""

    __slots__ = ("node",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, SubgraphNode)
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        scope = scope_of(state, ctx)
        try:
            inputs = {name: value_of(raw, scope) for name, raw in self.node.inputs.items()}
        except EvaluationError as exc:
            raise NodeError(f"{self.id}: sub-graph input failed: {exc}") from exc
        return NodeResult(
            push_graph=GraphInvocation(
                graph=self.node.graph,
                inputs=inputs,
                outputs_into=dict(self.node.outputs),
                kind="subgraph",
                return_node=self.node.next,
            )
        )


class EndRunner(_Runner):
    """Pop the frame and return outputs (DESIGN.md section 6.2)."""

    __slots__ = ("node",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, EndNode)
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        scope = scope_of(state, ctx)
        try:
            outputs = {name: value_of(raw, scope) for name, raw in self.node.outputs.items()}
        except EvaluationError as exc:
            raise NodeError(f"{self.id}: end output failed: {exc}") from exc
        return NodeResult(pop=True, outputs=outputs)


class AskRunner(_Runner):
    """Ask the customer for specific slots; suspend until the reply (DESIGN.md section 6.2).

    The suspension and the resumption are phase 2 (section 7.2). The extraction is not: section
    6.2 says "on resume, extracts slots via structured output", so it goes through the
    ``extract_slots`` hook, whose default puts the whole reply in the first slot and whose real
    implementation is phase 3's structured output call.
    """

    __slots__ = ("node",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, AskNode)
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        try:
            prompt = rt.render(self.node.prompt, scope_of(state, ctx)).strip()
        except TemplateError as exc:
            raise NodeError(f"{self.id}: prompt template failed: {exc}") from exc
        return NodeResult(
            outbound=[OutboundMessage(text=prompt)],
            suspend=SuspendReason(
                status="waiting_customer", detail={"node": self.id, "slots": list(self.node.slots)}
            ),
        )

    async def resume(
        self,
        state: BaseModel,
        ctx: ConversationContext,
        rt: NodeRuntime,
        event: ResumeEvent,
    ) -> NodeResult:
        if event.kind != "customer_message":
            msg = f"{self.id}: an ask node waits for a customer message, not {event.kind!r}"
            raise NodeError(msg)
        patch = await rt.hooks.extract_slots(self.node.slots, event.text or "", ctx)
        unknown = set(patch) - set(type(state).model_fields)
        if unknown:
            msg = f"{self.id}: slot extraction produced unknown state fields {sorted(unknown)}"
            raise NodeError(msg)
        return NodeResult(state_patch=dict(patch))


class NotExecutableRunner(_Runner):
    """A node type core validates but cannot run yet, naming the phase that adds it."""

    __slots__ = ("phase",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        spec = NODE_TYPES.get(node.type)
        self.phase = spec.executable_phase if spec else 0

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        msg = (
            f"{rt.graph.id}.{self.id}: node type {self.type!r} is not executable until "
            f"phase {self.phase}"
        )
        raise NodeNotExecutableError(msg)


RunnerFactory = Callable[[str, NodeBase], Any]
"""Builds the runner for one node. Returns a :class:`~support_core.engine.types.Node`."""

NODE_RUNNERS: dict[str, RunnerFactory] = {
    "say": SayRunner,
    "router": RouterRunner,
    "subgraph": SubgraphRunner,
    "end": EndRunner,
    "gate": GateRunner,
    "ask": AskRunner,
}
"""Runner per node type. A type in :data:`~support_core.graph.nodes.NODE_TYPES` but not here
is validated and refused at run time by :class:`NotExecutableRunner`."""


def build_runner(node_id: str, node: NodeBase) -> Any:
    """The runner for one node of a graph."""
    factory = NODE_RUNNERS.get(node.type)
    if factory is None:
        return NotExecutableRunner(node_id, node)
    return factory(node_id, node)


def register_node_type(spec: NodeTypeSpec, factory: RunnerFactory) -> None:
    """Register a node type and its behaviour by name (DESIGN.md section 6.2).

    "Custom node types are Python classes registered by name in the pack. They must implement
    the same ``Node`` protocol." Phase 2 needs the mechanism for its own tests: two of the four
    statuses in DESIGN.md section 7.2 (``waiting_async_tool``, ``waiting_timer``) have no core
    node type at all, because they belong to phase 4's async tools and to a scheduler, and the
    honest way to exercise suspension into them is to register a node that suspends that way
    rather than to hand-write database rows. Phase 4 uses the same call to load a pack's
    ``nodes/`` directory.

    Registration is process-wide, like the built-in table it extends. Use
    :func:`unregister_node_type` to undo it.
    """
    if spec.name in NODE_TYPES and spec.name not in _REGISTERED:
        msg = f"node type {spec.name!r} is a core type and cannot be replaced"
        raise ValueError(msg)
    NODE_TYPES[spec.name] = spec
    NODE_RUNNERS[spec.name] = factory
    _REGISTERED.add(spec.name)


def unregister_node_type(name: str) -> None:
    """Remove a type added by :func:`register_node_type`. Core types are never removed."""
    if name not in _REGISTERED:
        msg = f"node type {name!r} was not registered by register_node_type"
        raise ValueError(msg)
    NODE_TYPES.pop(name, None)
    NODE_RUNNERS.pop(name, None)
    _REGISTERED.discard(name)


_REGISTERED: set[str] = set()


def resolve_edge(node: NodeBase, label: str | None) -> str:
    """The node id an edge label leads to.

    ``None`` means the node's single ``next`` (DESIGN.md section 6.3: "None means the single
    next"). Every other label is one of the node's declared edges, or the ``next``/``on_error``/
    ``default`` fields, all of which :func:`~support_core.graph.nodes.edge_targets` reports.
    """
    targets = dict(edge_targets(node))
    if label is None:
        target = targets.get("next")
        if target is None:
            msg = f"node type {node.type!r} has no 'next' edge to follow"
            raise NodeError(msg)
        return target
    target = targets.get(label)
    if target is None:
        msg = f"edge {label!r} is not declared on this {node.type!r} node"
        raise NodeError(msg)
    return target
