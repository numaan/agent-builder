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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel

from support_core.engine.errors import NodeError, NodeNotExecutableError
from support_core.engine.hooks import EngineHooks, SlotRequest
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
    LlmNode,
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
from support_core.llm.prompt import Decision, TranscriptMessage
from support_core.llm.service import LlmService, NodeRequest
from support_core.llm.tool_loop import (
    ModelToolRunner,
    ReadOnlyToolGateway,
    ToolsUnavailableError,
    UnavailableToolRunner,
)
from support_core.llm.types import LLMError, LLMUnavailableError, StructuredOutputError
from support_core.tools.risk import Risk

DEFAULT_EDGE = "default"
"""The edge label a ``router`` returns when no predicate matched. Not a possible predicate:
a predicate must start with a root, so ``default`` never parses as one."""


@dataclass(slots=True)
class NodeRuntime:
    """What a node can reach outside itself (DESIGN.md section 6.3).

    Phase 2 gives nodes the pack, their frame, their step id and the engine hooks; phase 3 adds
    the LLM layer and the *seam* through which a model may call a tool. Retrieval (phase 5) and
    the tool runtime itself (phase 4) arrive as further attributes here, which is why nodes take
    ``rt`` rather than the individual pieces.

    The tool seam is deliberately not a callable a node can use directly. A pack-registered
    custom node type is arbitrary Python and ``rt`` is its only route to the outside
    (reviews/phase-2.md, "the every-phase rule"), so what a node gets is :attr:`tool_gateway` -
    a factory that builds a gateway around the node's own declared tool list, which refuses
    anything that is not a READ-tier tool that node declared.

    **The runtime does not hold the runner** (review finding V4). It used to, as a public
    ``tool_runner`` field, which meant a custom node type could call
    ``rt.tool_runner.invoke("issue_refund", ...)`` and never meet the gateway at all - inert
    while the default runner refuses everything, and a tool call without an ``ActionApproval``
    the moment phase 4 installs a real one. The runner is now captured in the closure
    :func:`tool_gateway_factory` returns, and the executor is the only thing that holds it.
    """

    graph: Graph
    frame: Frame
    step_id: str
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    hooks: EngineHooks
    environment: SandboxedEnvironment
    """The *pack's* Jinja environment, never a shared one (phase-1 deferred finding P2)."""

    llm: LlmService | None = None
    """The LLM layer (DESIGN.md section 11). ``None`` where no provider is configured, which is
    every phase-2 test: an ``llm`` node then fails as a node error and hands off, rather than
    silently doing nothing."""

    history: tuple[TranscriptMessage, ...] = ()
    """The recent window of DESIGN.md section 10, read once per turn by the executor."""

    tool_gateway: "ToolGatewayFactory" = field(default_factory=lambda: _no_tool_runtime)
    """The only way to reach a tool from inside a node (DESIGN.md sections 8.2, 8.4)."""

    def render(self, source: str, scope: Mapping[str, Any]) -> str:
        return render(source, dict(scope), env=self.environment)


ToolGatewayFactory = Callable[[Sequence[str]], ReadOnlyToolGateway]
"""Builds the gateway for one node from that node's own ``tools:`` list, and nothing wider."""


def tool_gateway_factory(
    runner: ModelToolRunner,
    *,
    tool_risk: Mapping[str, Risk],
    max_calls: int,
    step_id: str,
    record: Callable[[ReadOnlyToolGateway], None] | None = None,
) -> ToolGatewayFactory:
    """Capture the tool runtime where a node cannot reach it (review finding V4).

    ``runner`` lives in this closure and in the executor that built it. A node holds the
    returned callable, whose only effect is to construct a
    :class:`~support_core.llm.tool_loop.ReadOnlyToolGateway` around the tool list the node
    itself declared - so a node cannot widen its own allow-list, and cannot get past the gateway
    to the runner underneath.

    ``record`` is how the executor learns what a node spent: every gateway built is reported to
    it, so the per-*turn* budget of DESIGN.md section 5.1 can be counted on the run row rather
    than re-granted to each node (review finding V6).
    """

    def build(declared: Sequence[str]) -> ReadOnlyToolGateway:
        gateway = ReadOnlyToolGateway(
            runner=runner,
            declared=tuple(declared),
            manifest_risk=dict(tool_risk),
            max_calls=max_calls,
            step_id=step_id,
        )
        if record is not None:
            record(gateway)
        return gateway

    return build


_no_tool_runtime: ToolGatewayFactory = tool_gateway_factory(
    UnavailableToolRunner(), tool_risk={}, max_calls=10, step_id=""
)
"""The default: a gateway over the runner that refuses everything (phase 4 has not arrived)."""


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
        try:
            prompt = rt.render(self.node.prompt, scope_of(state, ctx)).strip()
        except TemplateError:  # pragma: no cover - run() rendered the same template already
            prompt = self.node.prompt
        request = SlotRequest(
            node_id=self.id,
            graph_id=rt.graph.id,
            slots=list(self.node.slots),
            prompt=prompt,
            reply=event.text or "",
            state_model=type(state),
            state=state.model_dump(mode="json"),
            window=[(message.author, message.text) for message in rt.history],
            ctx=ctx,
        )
        try:
            patch = await rt.hooks.extract_slots(request)
        except LLMUnavailableError as exc:
            raise NodeError(f"{self.id}: {exc}", reason="llm_unavailable") from exc
        except LLMError as exc:
            # A model that could not extract the slots must not fall back to a guess: the value
            # would be treated as the customer's own words by every node after this one.
            raise NodeError(
                f"{self.id}: slot extraction failed: {exc}", reason="llm_invalid_output"
            ) from exc
        # Restricted to the node's *declared slots*, not merely to the state's fields: an ask
        # node asks for specific values (DESIGN.md section 6.2), and an extractor that writes
        # some other field is writing something the customer was never asked about. Phase 2
        # checked the wider set because its extractor could only ever fill the first slot.
        unknown = set(patch) - set(self.node.slots)
        if unknown:
            msg = (
                f"{self.id}: slot extraction produced state fields this node did not ask for: "
                f"{sorted(unknown)}; its slots are {sorted(self.node.slots)}"
            )
            raise NodeError(msg)
        return NodeResult(state_patch=dict(patch))


class LlmRunner(_Runner):
    """A prompted step (DESIGN.md sections 6.2, 11.2, 11.3).

    The node's job in one sentence: hand the LLM layer the *graph's* constraints and turn what
    comes back into a :class:`~support_core.engine.types.NodeResult`, refusing anything the
    graph did not allow. Three things it refuses, all of them DESIGN.md section 11.3 or
    principle 2:

    * an answer that is not valid against the node's own model - an undeclared edge label, a
      state update outside ``output_schema``, no structured answer at all. The service has
      already retried once with the reason stated back to the model; a second failure is a node
      error, which routes to the handoff hook. It is never guessed at.
    * a confidence below the pack's threshold: the node's ``unclear`` edge if it declares one,
      and a handoff if it does not - because "no ``unclear`` edge" means the graph gave the
      model no way to be unsure, not that the guess is now safe.
    * ``needs_handoff``: the model may ask, and the engine decides. It decides yes, but the
      decision is the engine's and is recorded as such.

    The node's ``instructions`` are placed in layer 4 verbatim and are deliberately *not*
    rendered as a template: interpolating state into a trusted layer would be a way for customer
    text that reached a state field to become an instruction, which is the exact escape the
    layering exists to prevent. State reaches the model through layer 6, inside a data block.
    """

    __slots__ = ("node",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, LlmNode)
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        if rt.llm is None:
            msg = (
                f"{self.id}: this llm node needs an LLM provider and none is configured; "
                f"build the Executor with an LlmService"
            )
            raise NodeError(msg, reason="llm_unavailable")

        request = NodeRequest(
            node_id=self.id,
            instructions=self.node.instructions,
            decisions=self._decisions(rt.graph),
            output_schema=dict(self.node.output_schema or {}),
            state=state.model_dump(mode="json"),
            summary=ctx.summary,
            window=list(rt.history),
            gateway=rt.tool_gateway(self.node.tools) if self.node.tools else None,
            model=self.node.model,
            max_tool_iterations=rt.llm.max_tool_iterations,
        )
        try:
            decision = await rt.llm.decide(request)
        except StructuredOutputError as exc:
            msg = (
                f"{self.id}: the model did not produce a usable decision after a retry: {exc}. "
                f"The allowed decisions were {sorted(self.node.edges)}"
            )
            raise NodeError(msg, reason="llm_invalid_output") from exc
        except (LLMUnavailableError, ToolsUnavailableError) as exc:
            raise NodeError(f"{self.id}: {exc}", reason="llm_unavailable") from exc
        except LLMError as exc:  # pragma: no cover - every subclass is handled above
            raise NodeError(f"{self.id}: {exc}", reason="llm_unavailable") from exc

        output = decision.output
        trace = decision.as_trace()
        if output.needs_handoff:
            msg = (
                f"{self.id}: the model asked for a human "
                f"(decision {output.decision!r}, confidence {output.confidence})"
            )
            raise NodeError(msg, reason="model_requested_handoff")

        edge = output.decision
        if output.confidence < rt.llm.confidence_threshold:
            if "unclear" not in self.node.edges:
                msg = (
                    f"{self.id}: confidence {output.confidence} is below the pack threshold "
                    f"{rt.llm.confidence_threshold} and this node declares no 'unclear' edge, so "
                    f"there is nothing to route to but a human"
                )
                raise NodeError(msg, reason="low_confidence")
            trace["routed_to_unclear"] = True
            edge = "unclear"

        outbound = (
            [OutboundMessage(text=output.message_to_customer.strip())]
            if output.message_to_customer and output.message_to_customer.strip()
            else []
        )
        return NodeResult(
            state_patch=self._patch(output.state_updates, state),
            next_edge=edge,
            outbound=outbound,
            llm_response=trace,
        )

    def _decisions(self, graph: Graph) -> list[Decision]:
        """The node's edge labels, described by the node each one leads to.

        DESIGN.md section 11.2 layer 5 is "the node's edge labels with descriptions"; the only
        description a graph carries is the target node's own ``description``, which is what a
        pack author writes when they want to say what a branch means.
        """
        described: list[Decision] = []
        for label, target in self.node.edges.items():
            node = graph.nodes.get(target)
            description = getattr(node, "description", None) if node is not None else None
            described.append(Decision(label=label, description=description))
        return described

    def _patch(self, updates: Any, state: BaseModel) -> dict[str, Any]:
        """State updates, restricted to what the node declared and the state can hold.

        ``declared`` is the node's ``output_schema`` and nothing else (review finding V2). It
        used to fall back to "whatever the model wrote" when the node declared no schema, which
        made the absence of a schema mean *any field, any type* rather than *none*: on a node
        with no schema a model could write the graph's own ``outcome`` field, which a later
        router or gate would then read as if the workflow had established it. The schema is now
        also enforced one layer up, in
        :func:`~support_core.llm.schemas.build_node_output_model`, so this check is the second
        of two rather than the only one.
        """
        if isinstance(updates, BaseModel):
            values = updates.model_dump(mode="json", exclude_unset=True)
        else:
            values = dict(updates or {})
        declared = set(self.node.output_schema or {})
        fields = set(type(state).model_fields)
        unknown = sorted((set(values) - declared) | (set(values) - fields))
        if unknown:
            declares = sorted(self.node.output_schema or {}) or (
                "nothing, so this node may write no state"
            )
            msg = (
                f"{self.id}: the model tried to write state fields it was not offered: "
                f"{unknown}; output_schema declares {declares}"
            )
            raise NodeError(msg, reason="llm_invalid_output")
        return values


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
    "llm": LlmRunner,
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

    Registration is process-wide, like the built-in table it extends, and a name may be
    registered only once: re-registering the *same* spec and factory is a no-op, and
    registering a different behaviour under a name somebody already took is an error rather
    than a silent replacement (review finding R6). Two packs that both define a
    ``verify_identity`` node type would otherwise overwrite each other, which is the same
    failure mode as the process-wide Jinja environment this phase removed, and DESIGN.md
    section 6.7 keeps two pack versions loaded side by side. Making the registry per-pack -
    so both packs can have their own - needs the loader and the validator to carry it too and
    is recorded as a deferred finding against phase 9. Use :func:`unregister_node_type` to undo
    a registration.
    """
    if spec.name in NODE_TYPES and spec.name not in _REGISTERED:
        msg = f"node type {spec.name!r} is a core type and cannot be replaced"
        raise ValueError(msg)
    if spec.name in _REGISTERED and (
        NODE_TYPES[spec.name] != spec or NODE_RUNNERS[spec.name] is not factory
    ):
        msg = (
            f"node type {spec.name!r} is already registered with a different spec or runner; "
            f"unregister it first"
        )
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
