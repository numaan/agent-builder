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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel

from support_core.engine.errors import NodeError, NodeNotExecutableError
from support_core.engine.hooks import ConfirmRequest, EngineHooks, SlotRequest
from support_core.engine.types import (
    ApprovalProposal,
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
    ConfirmNode,
    EndNode,
    GateNode,
    HandoffNode,
    LlmNode,
    NodeBase,
    NodeTypeSpec,
    RouterNode,
    SayNode,
    Scalar,
    SubgraphNode,
    ToolNode,
    edge_targets,
)
from support_core.graph.schema import Graph, parse_value
from support_core.graph.templates import TemplateError, render
from support_core.llm.prompt import Decision, DeferredIntent, TranscriptMessage
from support_core.llm.service import LlmService, NodeRequest
from support_core.llm.tool_loop import (
    ModelToolRunner,
    ReadOnlyToolGateway,
    ToolsUnavailableError,
    UnavailableToolRunner,
)
from support_core.llm.types import LLMError, LLMUnavailableError, StructuredOutputError
from support_core.tools.base import ToolError, ToolFailed, ToolRefused
from support_core.tools.risk import Risk
from support_core.tools.runtime import ToolCallResult

DEFAULT_EDGE = "default"
"""The edge label a ``router`` returns when no predicate matched. Not a possible predicate:
a predicate must start with a root, so ``default`` never parses as one."""


ToolInvoker = Callable[[str, Mapping[str, Any]], Awaitable[ToolCallResult]]
ToolCompleter = Callable[[str, Mapping[str, Any], str], Awaitable[ToolCallResult]]
ActionHasher = Callable[[str, Mapping[str, Any]], tuple[str, dict[str, Any]]]

ASYNC_KEY = "idempotency_key"
"""Where a suspended ``tool`` node records the key its dispatch claimed (finding R1)."""


@dataclass(frozen=True, slots=True)
class NodeToolAccess:
    """Everything one node may do with the tool runtime, and nothing wider.

    Built by the executor, per node, from what the *validated graph* says that node is. Three
    closures, each already carrying the node's identity, its frame, its step id, the single tool
    it declared and the ``confirm`` node it named - so a node cannot widen its own permission,
    cannot invoke a tool some other node declared, and cannot reach the
    :class:`~support_core.tools.runtime.ToolRuntime` underneath, which the executor alone holds.
    This is the shape phase 3's review finding V4 forced on the model-loop gateway, applied to
    the deterministic path.

    A node the graph does not declare as ``type: tool`` gets an :attr:`invoke` that refuses
    everything. A pack-registered custom node type is arbitrary Python and ``rt`` is its only
    route to the outside (reviews/phase-2.md), so what it may do with tools is exactly what an
    ``llm`` node may do: the READ-only gateway, and nothing else.
    """

    invoke: ToolInvoker
    """Run this node's declared tool. Raises :class:`~support_core.tools.base.ToolRefused`."""

    complete: ToolCompleter
    """Finish an async tool from its callback payload (DESIGN.md section 7.2).

    Takes the idempotency key the dispatch claimed, which the node carried out on its
    suspension: the step id computed on the *resuming* pass names a different attempt and so a
    call nobody made (review finding R1).
    """

    hash_action: ActionHasher
    """``sha256(tool + canonical_json(args))`` for a proposal, without running anything.

    A ``confirm`` node needs the hash and nothing else; giving it the registry would give it the
    tools too.
    """

    declared: tuple[str, ...] = ()


async def _no_tool_runtime_call(name: str, args: Mapping[str, Any]) -> ToolCallResult:
    msg = (
        f"this node may not invoke tools; {name!r} would have to be called from a 'tool' node "
        f"declared in the graph (DESIGN.md section 8.2)"
    )
    raise ToolRefused(msg)


async def _no_tool_runtime_complete(
    name: str, payload: Mapping[str, Any], key: str
) -> ToolCallResult:
    return await _no_tool_runtime_call(name, payload)


def _no_tool_runtime_hash(name: str, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    msg = f"no tool runtime is configured, so {name!r} cannot be described or hashed"
    raise ToolRefused(msg)


NO_TOOL_ACCESS = NodeToolAccess(
    invoke=_no_tool_runtime_call,
    complete=_no_tool_runtime_complete,
    hash_action=_no_tool_runtime_hash,
)
"""The default: a node with no tool capability at all."""


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
    """The model-facing, READ-only loop of DESIGN.md section 8.4. Any node may build one."""

    tools: NodeToolAccess = NO_TOOL_ACCESS
    """The deterministic path of DESIGN.md section 8.2, narrowed to this node (see
    :class:`NodeToolAccess`). A node that is not a ``tool`` node cannot invoke anything with it."""

    pending_intents: tuple[DeferredIntent, ...] = ()
    """Requests the customer made that a workflow refused to stop for (DESIGN.md section 6.6
    step 5), surfaced to the root graph's classifier (section 19 step 15). Non-empty only for a
    node in the root frame; the executor decides that, not the node."""

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
"""The default: a gateway over the runner that refuses everything, for an executor built
without a tool runtime. The real one is built per node by the executor."""


def scope_of(state: BaseModel, ctx: ConversationContext) -> dict[str, Any]:
    """The roots an expression or template may read (DESIGN.md section 6.4).

    ``result`` is absent: it only exists inside a ``tool`` node's ``into`` mapping, which
    adds it for that one evaluation.
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
            hint=event.hint,
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
            pending_intents=rt.pending_intents,
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


@dataclass(frozen=True, slots=True)
class _Proposal:
    """What a ``confirm`` node is about to show the customer, and its hash."""

    prompt: str
    tool: str
    args: dict[str, Any]
    args_hash: str


class ConfirmRunner(_Runner):
    """Present a proposed action and require an explicit yes (DESIGN.md sections 6.2, 8.2).

    The node's whole job is to turn a customer's word into an
    :class:`~support_core.storage.models.ActionApproval` bound to
    ``sha256(tool_name + canonical_json(args))`` - and to refuse to do so in every case where
    what the customer agreed to is not what would happen:

    * the arguments are evaluated and hashed **when the proposal is shown**, and the hash travels
      with the suspension. On resume the node hashes again and compares. A frame can be
      re-entered between the two - a gate whose predicate lapsed pushes its redirect before the
      reply is delivered (section 6.6) - so "the state now" is not automatically "the state the
      customer was answering about". A difference re-presents the proposal instead of approving
      the new one.
    * an answer that is not an explicit yes or no is asked again, not resolved. Guessing "no"
      discards what the customer wanted; guessing "yes" moves their money.
    * the node records nothing at all on ``no``. An unused approval is a spendable one.

    The approval is *proposed* here and written by the executor, in the checkpoint transaction,
    with the run, frame and node id taken from the frame - so what a node can influence is the
    tool and the arguments, both of which the hash covers, and not where the approval appears to
    have come from.
    """

    __slots__ = ("node",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, ConfirmNode)
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        return self._present(self._propose(state, ctx, rt))

    async def resume(
        self,
        state: BaseModel,
        ctx: ConversationContext,
        rt: NodeRuntime,
        event: ResumeEvent,
    ) -> NodeResult:
        if event.kind != "customer_message":
            msg = f"{self.id}: a confirm node waits for a customer message, not {event.kind!r}"
            raise NodeError(msg)
        current = self._propose(state, ctx, rt)
        presented = str(event.detail.get("args_hash") or "")
        if presented and presented != current.args_hash:
            # DESIGN.md 8.2's gap, caught at the earliest point rather than only at the call:
            # what the customer is answering is not what would now happen.
            changed = (
                "The details of that action have changed since I asked, so I do not want to act "
                "on the old answer."
            )
            return self._present(current, prefix=changed)
        decision = await rt.hooks.confirm_decision(
            ConfirmRequest(
                node_id=self.id,
                graph_id=rt.graph.id,
                prompt=str(event.detail.get("prompt") or current.prompt),
                reply=event.text or "",
                tool=current.tool,
                args=dict(current.args),
                window=[(message.author, message.text) for message in rt.history],
                ctx=ctx,
            )
        )
        if decision.answer == "yes":
            return NodeResult(
                next_edge="yes",
                approval=ApprovalProposal(
                    tool=current.tool, args=dict(current.args), args_hash=current.args_hash
                ),
            )
        if decision.answer == "no":
            return NodeResult(next_edge="no")
        return self._present(
            current,
            prefix="Sorry, I need a clear yes or no before I do anything.",
        )

    def _present(self, proposal: _Proposal, *, prefix: str | None = None) -> NodeResult:
        text = f"{prefix}\n\n{proposal.prompt}" if prefix else proposal.prompt
        return NodeResult(
            outbound=[OutboundMessage(text=text)],
            suspend=SuspendReason(
                status="waiting_customer",
                detail={
                    "node": self.id,
                    "kind": "confirm",
                    "tool": proposal.tool,
                    "args_hash": proposal.args_hash,
                    "prompt": proposal.prompt,
                },
            ),
        )

    def _propose(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> _Proposal:
        scope = scope_of(state, ctx)
        try:
            args = {name: value_of(raw, scope) for name, raw in self.node.action.args.items()}
        except EvaluationError as exc:
            raise NodeError(f"{self.id}: confirm action argument failed: {exc}") from exc
        try:
            args_hash, canonical = rt.tools.hash_action(self.node.action.tool, args)
        except ToolError as exc:
            raise NodeError(f"{self.id}: {exc}", reason="tool_refused") from exc
        try:
            prompt = rt.render(self.node.prompt, scope).strip()
        except TemplateError as exc:
            raise NodeError(f"{self.id}: confirm prompt template failed: {exc}") from exc
        return _Proposal(
            prompt=prompt, tool=self.node.action.tool, args=canonical, args_hash=args_hash
        )


class ToolRunner(_Runner):
    """A deterministic tool call with arguments from state expressions (DESIGN.md section 6.2).

    The node evaluates its ``args`` and hands them to the capability the executor built for it.
    It does not decide whether the call is allowed: the risk policy, the approval check and the
    idempotency key all live in :class:`~support_core.tools.runtime.ToolRuntime`, one layer
    down, where a pack-registered node type cannot get at them either.

    A refusal or a failure is a :class:`~support_core.engine.errors.NodeError`, which the
    executor routes to the node's ``on_error`` edge if it declares one and to a handoff if it
    does not (DESIGN.md section 7.3). The two are distinguished by ``reason`` - ``tool_refused``
    means the runtime would not run it, ``tool_failed`` means it ran and something went wrong -
    because a handoff packet that cannot tell those apart is not much of a packet.
    """

    __slots__ = ("node",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, ToolNode)
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        scope = scope_of(state, ctx)
        try:
            args = {name: value_of(raw, scope) for name, raw in self.node.args.items()}
        except EvaluationError as exc:
            raise NodeError(f"{self.id}: tool argument failed: {exc}") from exc
        result = await self._call(rt.tools.invoke, args)
        if result.pending:
            # The dispatch's idempotency key travels with the suspension, because the step id
            # computed when the callback arrives names a later attempt and therefore a call
            # nobody claimed (review finding R1). This is the same device the ``confirm`` node
            # uses for its argument hash: what the resuming pass needs is what the suspending
            # pass knew, and durable state is the only place the two can meet.
            return NodeResult(
                suspend=SuspendReason(
                    status="waiting_async_tool",
                    detail={
                        "node": self.id,
                        "kind": "async_tool",
                        "tool": self.node.tool,
                        ASYNC_KEY: result.idempotency_key,
                    },
                )
            )
        return self._apply(result, state, ctx)

    async def resume(
        self,
        state: BaseModel,
        ctx: ConversationContext,
        rt: NodeRuntime,
        event: ResumeEvent,
    ) -> NodeResult:
        if event.kind != "async_tool":
            msg = f"{self.id}: a tool node waits for its async tool's callback, not {event.kind!r}"
            raise NodeError(msg)
        key = str(event.detail.get(ASYNC_KEY) or "")
        if not key:
            # Nothing to complete, and guessing a key is how the call that *is* waiting gets
            # stranded. A run suspended by an older core has no key on its suspension, and a
            # refusal it can route is a better answer than a callback applied to the wrong call.
            msg = (
                f"{self.id}: this suspension records no dispatched call to complete, so the "
                f"callback cannot be matched to one"
            )
            raise NodeError(msg, reason="tool_refused")
        result = await self._complete(rt.tools.complete, event.payload, key)
        return self._apply(result, state, ctx)

    async def _complete(
        self, call: ToolCompleter, payload: Mapping[str, Any], key: str
    ) -> ToolCallResult:
        async def invoke(name: str, args: Mapping[str, Any]) -> ToolCallResult:
            return await call(name, args, key)

        return await self._call(invoke, payload)

    async def _call(self, call: ToolInvoker, args: Mapping[str, Any]) -> ToolCallResult:
        try:
            return await call(self.node.tool, args)
        except ToolRefused as exc:
            raise NodeError(f"{self.id}: {exc}", reason="tool_refused") from exc
        except ToolFailed as exc:
            raise NodeError(f"{self.id}: {exc}", reason="tool_failed") from exc
        except ToolError as exc:  # pragma: no cover - the two subclasses cover it
            raise NodeError(f"{self.id}: {exc}", reason="tool_failed") from exc

    def _apply(
        self, result: ToolCallResult, state: BaseModel, ctx: ConversationContext
    ) -> NodeResult:
        """Map the tool's output into state through ``into`` (DESIGN.md section 6.4)."""
        patch: dict[str, Any] = {}
        into = self.node.into
        if isinstance(into, str):
            # ``into: state.charge`` - the whole result object into one field.
            patch[into.removeprefix("state.")] = result.output_json
        elif into:
            scope = dict(scope_of(state, ctx))
            scope["result"] = result.output
            try:
                patch = {name: value_of(raw, scope) for name, raw in into.items()}
            except EvaluationError as exc:
                raise NodeError(f"{self.id}: into mapping failed: {exc}") from exc
        return NodeResult(state_patch=patch, customer_patch=result.customer_patch)


DESK_ACTION = "__desk_action__"
"""Key in a human resume event's payload saying which desk action this was.

Underscored and reserved, like :data:`~support_core.engine.interrupts.INTERRUPT_RETURN_NODE`,
because the rest of that payload is a *state patch* a desk supplies (DESIGN.md section 13:
"``resume`` with optional state patch") and a graph could legitimately declare a field called
``action``. The executor puts it there; a desk cannot, because the desk API takes the action as
its own argument and never as part of the patch.
"""


DEFAULT_HANDOFF_MESSAGE = (
    "I am passing this conversation to one of our people, with what you have told me so far. "
    "They will reply here."
)
"""What a ``handoff`` node says when the pack does not write its own message.

Two sentences, and both are true of what happens next: the packet really does carry the state,
the actions and the transcript link (DESIGN.md section 13), and the desk API's ``reply`` really
does put a human's answer into this conversation. It deliberately does not say how long it will
take, because the pack's ``handoff.sla_minutes`` is a target for the queue, not a promise to a
customer, and DESIGN.md section 14 makes an unbacked promise a defect.
"""


class HandoffRunner(_Runner):
    """Hand the conversation to a person (DESIGN.md sections 6.2, 13).

    The node's own job is small, on purpose. It says one sentence and suspends
    ``waiting_human``; the *packet* is built and delivered by the executor, because DESIGN.md
    section 6.3 says nodes never touch storage and a packet is six queries and a model call over
    durable state. That division also gets the ordering right: the executor delivers before the
    checkpoint that parks the run, so there is no window in which a run says a human is needed
    and no human has been told.

    On the way back it does no more than read what the desk did:

    * ``resume`` - with an optional state patch, which the executor has already applied - takes
      the ``resumed`` edge, which is DESIGN.md section 13's "On ``resume``, the graph continues
      from the handoff node's ``resumed`` edge";
    * ``close`` takes the ``closed`` edge, so a pack decides what taking over fully means for
      its own graph rather than having the engine end the conversation underneath it.

    A human's typed reply is *not* an edge. The desk's ``reply`` writes a message into the
    conversation and leaves the run parked, because answering a customer is not the same act as
    giving the workflow back and conflating them would resume a graph every time somebody typed.
    """

    __slots__ = ("node",)

    def __init__(self, node_id: str, node: NodeBase) -> None:
        super().__init__(node_id, node)
        assert isinstance(node, HandoffNode)
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        message = DEFAULT_HANDOFF_MESSAGE
        if self.node.message:
            try:
                message = rt.render(self.node.message, scope_of(state, ctx)).strip()
            except TemplateError as exc:
                raise NodeError(f"{self.id}: handoff message template failed: {exc}") from exc
        return NodeResult(
            outbound=[OutboundMessage(text=message)] if message else [],
            suspend=SuspendReason(
                status="waiting_human",
                detail={
                    "node": self.id,
                    "kind": "handoff",
                    "reason": self.node.reason,
                    "next_steps": list(self.node.next_steps),
                },
            ),
        )

    async def resume(
        self,
        state: BaseModel,
        ctx: ConversationContext,
        rt: NodeRuntime,
        event: ResumeEvent,
    ) -> NodeResult:
        if event.kind != "human":
            msg = f"{self.id}: a handoff node waits for a human, not {event.kind!r}"
            raise NodeError(msg)
        action = str(event.payload.get(DESK_ACTION) or "resume")
        return NodeResult(next_edge="closed" if action == "close" else "resumed")


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
    "tool": ToolRunner,
    "confirm": ConfirmRunner,
    "handoff": HandoffRunner,
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
