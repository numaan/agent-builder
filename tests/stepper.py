"""TEST UTILITY - a minimal in-memory graph stepper. **Not** the execution engine.

The phase-1 exit criterion in BACKLOG.md is "a deterministic graph using only ``router``,
``say``, ``subgraph``, ``end`` executes in a unit test through a minimal in-memory stepper".
This is that stepper, and it deliberately lives in ``tests/`` so that phase 2, which owns the
real executor (DESIGN.md sections 6.3 and 7.1), does not inherit a second one.

What it deliberately does **not** do, and what phase 2 must:

* no checkpointing, no database, no trace steps, no advisory lock, no idempotency keys;
* no ``NodeResult``/``NodeRuntime`` protocol - it matches on node classes directly;
* no suspension, no resume, no interrupt check, no gate re-entry on frame entry;
* no limits beyond a step counter;
* no ``ActionApproval`` - it refuses to run any node type that is not executable in phase 1,
  which is exactly the set that cannot cause a side effect (PLAN.md's standing rule).

Everything reusable it needs (expression evaluation, template rendering, the node registry) it
imports from ``support_core``; the frame-stack loop below is the only logic that is duplicated
work for phase 2, and it is about eighty lines.
"""

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from support_core.graph.context import ConversationContext
from support_core.graph.expr import evaluate, parse
from support_core.graph.nodes import (
    NODE_TYPES,
    EndNode,
    RouterNode,
    SayNode,
    SubgraphNode,
)
from support_core.graph.pack import Pack
from support_core.graph.schema import Graph, parse_value
from support_core.graph.templates import render


class StepperError(RuntimeError):
    """The stepper cannot continue: a dead-ended router, a step limit, or a missing graph."""


class NotExecutableError(StepperError):
    """The graph reached a node type core cannot run yet, naming the phase that adds it."""


@dataclass(slots=True)
class Frame:
    """One active graph invocation (DESIGN.md section 6.1)."""

    graph_id: str
    node_id: str
    state: BaseModel
    return_node: str | None = None
    """Node in the caller to continue at once this frame pops."""

    outputs_into: dict[str, str] = field(default_factory=dict)
    """Caller state field to callee output name."""


@dataclass(slots=True)
class Transcript:
    """What one run produced. Deterministic: the same inputs give the same transcript."""

    path: list[tuple[str, str]] = field(default_factory=list)
    """``(graph id, node id)`` in visit order."""

    messages: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    final_state: dict[str, Any] = field(default_factory=dict)


def run(
    pack: Pack,
    graph_id: str | None = None,
    *,
    ctx: ConversationContext | None = None,
    inputs: dict[str, Any] | None = None,
    max_steps: int = 100,
) -> Transcript:
    """Run ``graph_id`` (default: the pack's entry graph) to its first ``end`` of the root frame."""
    context = ctx or ConversationContext()
    graph = pack.graphs[graph_id or pack.manifest.entry_graph]
    transcript = Transcript()
    stack = [Frame(graph_id=graph.id, node_id=graph.start, state=_new_state(graph, inputs or {}))]

    for _ in range(max_steps):
        frame = stack[-1]
        graph = pack.graphs[frame.graph_id]
        node = graph.nodes[frame.node_id]
        transcript.path.append((frame.graph_id, frame.node_id))
        spec = NODE_TYPES[node.type]
        if not spec.executable:
            raise NotExecutableError(
                f"{frame.graph_id}.{frame.node_id}: node type {node.type!r} is not executable "
                f"until phase {spec.executable_phase}"
            )
        scope: dict[str, Any] = {"state": frame.state, "ctx": context}

        if isinstance(node, SayNode):
            transcript.messages.append(render(node.message, scope).strip())
            frame.node_id = node.next
        elif isinstance(node, RouterNode):
            frame.node_id = _route(node, scope, frame)
        elif isinstance(node, SubgraphNode):
            callee = pack.graphs.get(node.graph)
            if callee is None:
                raise StepperError(f"{frame.graph_id}.{frame.node_id}: no graph {node.graph!r}")
            stack.append(
                Frame(
                    graph_id=callee.id,
                    node_id=callee.start,
                    state=_new_state(callee, {k: _value(v, scope) for k, v in node.inputs.items()}),
                    return_node=node.next,
                    outputs_into=dict(node.outputs),
                )
            )
        elif isinstance(node, EndNode):
            outputs = {name: _value(raw, scope) for name, raw in node.outputs.items()}
            stack.pop()
            if not stack:
                transcript.outputs = outputs
                transcript.final_state = frame.state.model_dump()
                return transcript
            caller = stack[-1]
            for state_field, output_name in frame.outputs_into.items():
                setattr(caller.state, state_field, outputs.get(output_name))
            if frame.return_node is None:  # pragma: no cover - a pushed frame always has one
                raise StepperError("a pushed frame has no return node")
            caller.node_id = frame.return_node
        else:  # pragma: no cover - the executable set is exactly the four above
            raise NotExecutableError(f"no stepper support for node type {node.type!r}")

    raise StepperError(f"more than {max_steps} steps; the graph does not terminate")


def _new_state(graph: Graph, inputs: dict[str, Any]) -> BaseModel:
    """A fresh state for ``graph``, seeded from the inputs whose names are state fields.

    DESIGN.md section 6.4 declares ``inputs`` and ``state`` separately but never says how a node
    reads an input. The stepper takes the reading the validator enforces
    (``graph.input_not_in_state``): an input lands in the state field of the same name. Phase 2
    owns the real answer.
    """
    fields = graph.state.model.model_fields
    seed = {name: value for name, value in inputs.items() if name in fields}
    return graph.state.model(**seed)


def _route(node: RouterNode, scope: dict[str, Any], frame: Frame) -> str:
    """First branch whose predicate is true wins; declaration order is the tie-break."""
    for predicate, target in node.edges.items():
        if evaluate(parse(predicate), scope):
            return target
    if node.default is None:
        raise StepperError(
            f"{frame.graph_id}.{frame.node_id}: no router branch matched and there is no default"
        )
    return node.default


def _value(raw: Any, scope: dict[str, Any]) -> Any:
    value = parse_value(raw)
    return evaluate(value.expression, scope) if value.expression is not None else value.literal
