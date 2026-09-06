"""Node type registry. Implements DESIGN.md section 6.2 (the core vocabulary) and 6.4 (the
YAML shape of each node).

This module is the single place a node type is defined. The schema (:mod:`.schema`), the
validator (:mod:`.rules`), the CLI and the phase-1 test stepper all read :data:`NODE_TYPES`;
none of them carries its own list of node names, edge fields or suspension behaviour.

Each :class:`NodeTypeSpec` records the four facts DESIGN.md section 6.2 tabulates:

* ``model`` - the Pydantic model for that node's YAML block,
* ``chooses_edge`` - whether the node selects among labelled edges,
* ``suspends`` - what it waits for, if anything (DESIGN.md section 7.2),
* ``executable`` - whether core can run it *today*.

``router``, ``say``, ``end`` and ``subgraph`` became executable in phase 1; ``gate`` and
``ask`` in phase 2, because DESIGN.md section 6.6 ("gates fire on every entry to a frame") and
section 7.2 (suspension) are phase 2's subject and neither can be tested without running one.
A gate only evaluates an expression and pushes a graph; an ``ask`` node's slot extraction, the
one part that needs a model, is an injectable hook whose default is deterministic and which
phase 3 replaces. ``tool`` and ``confirm`` became executable in phase 4, which is where the
tool runtime, the approval hash and the idempotency key live. ``handoff`` is still declared but
not executable: the validator type-checks it now and phase 6 supplies the behaviour.
``executable_phase`` records which phase that is, so "not implemented" is never a mystery.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SuspendKind = Literal["waiting_customer", "waiting_human", "waiting_async_tool"]
"""DESIGN.md section 7.2 statuses a node can suspend into."""

Scalar = str | bool | int | float | None
"""A YAML scalar in an ``args``/``into``/``outputs``/``inputs`` mapping. Either an expression
(always a string) or a literal of the declared type, so ``{ verified: false }`` and
``{ outcome: "refunded" }`` both work (DESIGN.md section 6.4 uses both forms)."""


class NodeBase(BaseModel):
    """Fields every node block may carry. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    type: str
    description: str | None = None


class RouterNode(NodeBase):
    """Deterministic branch on a state predicate (DESIGN.md section 6.2)."""

    type: Literal["router"]
    edges: dict[str, str] = Field(min_length=1)
    """Predicate expression to target node id. The first branch that evaluates true wins."""

    default: str | None = None
    """Target when no predicate matches. Absent means the router can dead-end at run time."""


class SayNode(NodeBase):
    """Emit a templated message with no LLM call (DESIGN.md section 6.2)."""

    type: Literal["say"]
    message: str
    """Jinja template rendered in the sandboxed environment (:mod:`.templates`)."""

    next: str


class EndNode(NodeBase):
    """Pop the frame and return outputs (DESIGN.md section 6.2)."""

    type: Literal["end"]
    outputs: dict[str, Scalar] = Field(default_factory=dict)
    """Declared output name to a literal or an expression (see :func:`.schema.parse_value`)."""


class SubgraphNode(NodeBase):
    """Invoke another graph with input and output mapping (DESIGN.md section 6.2)."""

    type: Literal["subgraph"]
    graph: str
    inputs: dict[str, Scalar] = Field(default_factory=dict)
    """Callee input name to a caller-side expression or literal."""

    outputs: dict[str, str] = Field(default_factory=dict)
    """Caller state field to a callee output name."""

    next: str


class KnowledgeQuery(BaseModel):
    """A retrieval request attached to an ``llm`` node (DESIGN.md sections 6.4 and 9)."""

    model_config = ConfigDict(extra="forbid")

    query: str
    """A Jinja template, so it can interpolate state (DESIGN.md section 6.4)."""

    k: int = Field(default=3, ge=1, le=20)


class LlmNode(NodeBase):
    """Prompted step that may fill slots, speak, call read tools and choose an edge."""

    type: Literal["llm"]
    instructions: str
    tools: list[str] = Field(default_factory=list)
    """READ-tier tools the model may call in this node's bounded loop (DESIGN.md section 8.4)."""

    output_schema: dict[str, str] | None = None
    """The state fields this node's answer may write, and their types (DESIGN.md section 11.3).

    ``None`` means *undeclared*, which is not the same as ``{}``. Undeclared is a validation
    error (``graph.llm_output_schema_absent``), because it used to mean "anything, of any type"
    at run time and now means "nothing", and a pack author who has not said which they meant
    should find out at load rather than on a customer's turn (review finding V2). ``{}`` is the
    explicit way to say a node writes no state."""
    knowledge: KnowledgeQuery | None = None
    edges: dict[str, str] = Field(min_length=1)
    model: str | None = None
    """Per-node model override (DESIGN.md section 11.1: "Model choice is per pack with per-node
    override"). Unset means the pack's ``llm.default_model``."""


class AskNode(NodeBase):
    """Ask the customer for specific slots; suspends until the reply."""

    type: Literal["ask"]
    slots: list[str] = Field(min_length=1)
    prompt: str
    next: str


class ToolNode(NodeBase):
    """Deterministic tool call with arguments from state expressions."""

    type: Literal["tool"]
    tool: str
    args: dict[str, Scalar] = Field(default_factory=dict)
    into: str | dict[str, Scalar] | None = None
    """Either a target path (``state.charge``) or ``{state field: literal or expression}``."""

    requires_approval: str | None = None
    """The ``confirm`` node whose ``ActionApproval`` must match (DESIGN.md section 8.2)."""

    on_error: str | None = None
    next: str


class GateNode(NodeBase):
    """Assert a predicate; on false push ``redirect`` as a sub-frame and re-evaluate."""

    type: Literal["gate"]
    predicate: str
    redirect: str
    """A graph id, not a node id."""

    next: str


class ConfirmAction(BaseModel):
    """The action a ``confirm`` node proposes, hashed into the ``ActionApproval`` (8.2)."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Scalar] = Field(default_factory=dict)


class ConfirmNode(NodeBase):
    """Present a proposed action and require an explicit yes (DESIGN.md sections 6.2, 8.2)."""

    type: Literal["confirm"]
    action: ConfirmAction
    prompt: str
    edges: dict[Literal["yes", "no"], str]


class HandoffNode(NodeBase):
    """Build a handoff packet and suspend until a human returns control or closes."""

    type: Literal["handoff"]
    reason: str
    edges: dict[Literal["resumed", "closed"], str]


@dataclass(frozen=True, slots=True)
class NodeTypeSpec:
    name: str
    model: type[NodeBase]
    chooses_edge: bool
    suspends: SuspendKind | None
    executable: bool
    executable_phase: int
    """The BACKLOG.md phase that makes (or made) this node type executable."""

    suspends_when_async_tool: bool = False
    """``tool`` nodes suspend only when the tool they call is declared async (6.2)."""

    template_fields: tuple[str, ...] = ()
    """Fields holding a Jinja template that must validate against the graph state (6.4)."""

    expression_fields: tuple[str, ...] = ()
    """Fields holding a single expression."""


NODE_TYPES: dict[str, NodeTypeSpec] = {
    "router": NodeTypeSpec(
        name="router",
        model=RouterNode,
        chooses_edge=True,
        suspends=None,
        executable=True,
        executable_phase=1,
    ),
    "say": NodeTypeSpec(
        name="say",
        model=SayNode,
        chooses_edge=False,
        suspends=None,
        executable=True,
        executable_phase=1,
        template_fields=("message",),
    ),
    "end": NodeTypeSpec(
        name="end",
        model=EndNode,
        chooses_edge=False,
        suspends=None,
        executable=True,
        executable_phase=1,
    ),
    "subgraph": NodeTypeSpec(
        name="subgraph",
        model=SubgraphNode,
        chooses_edge=False,
        suspends=None,
        executable=True,
        executable_phase=1,
    ),
    "llm": NodeTypeSpec(
        name="llm",
        model=LlmNode,
        chooses_edge=True,
        suspends=None,
        executable=True,
        executable_phase=3,
    ),
    "ask": NodeTypeSpec(
        name="ask",
        model=AskNode,
        chooses_edge=False,
        suspends="waiting_customer",
        executable=True,
        executable_phase=2,
        template_fields=("prompt",),
    ),
    "tool": NodeTypeSpec(
        name="tool",
        model=ToolNode,
        chooses_edge=False,
        suspends=None,
        executable=True,
        executable_phase=4,
        suspends_when_async_tool=True,
    ),
    "gate": NodeTypeSpec(
        name="gate",
        model=GateNode,
        chooses_edge=False,
        suspends=None,
        executable=True,
        executable_phase=2,
        expression_fields=("predicate",),
    ),
    "confirm": NodeTypeSpec(
        name="confirm",
        model=ConfirmNode,
        chooses_edge=True,
        suspends="waiting_customer",
        executable=True,
        executable_phase=4,
        template_fields=("prompt",),
    ),
    "handoff": NodeTypeSpec(
        name="handoff",
        model=HandoffNode,
        chooses_edge=True,
        suspends="waiting_human",
        executable=False,
        executable_phase=6,
    ),
}


def executable_types() -> frozenset[str]:
    """Node types core can run today.

    A function, not a constant: :func:`support_core.engine.runners.register_node_type` can add
    a type at run time (DESIGN.md section 6.2, "custom node types are Python classes registered
    by name in the pack"), and a constant computed at import would go stale.
    """
    return frozenset(name for name, spec in NODE_TYPES.items() if spec.executable)


def customer_input_types() -> frozenset[str]:
    """Node types that receive a customer message. The confirm-on-all-paths analysis in
    :mod:`.rules` treats these as the "last customer input" points of DESIGN.md section 5.2."""
    return frozenset(
        name for name, spec in NODE_TYPES.items() if spec.suspends == "waiting_customer"
    )


AnyNode = (
    RouterNode
    | SayNode
    | EndNode
    | SubgraphNode
    | LlmNode
    | AskNode
    | ToolNode
    | GateNode
    | ConfirmNode
    | HandoffNode
)


def edge_targets(node: NodeBase) -> list[tuple[str, str]]:
    """``(edge label, target node id)`` for every outgoing edge of ``node``.

    ``gate.redirect`` is not here: it names a *graph*, not a node. Use :func:`graph_references`.
    """
    targets: list[tuple[str, str]] = []
    edges = getattr(node, "edges", None)
    if isinstance(edges, dict):
        targets.extend((str(label), target) for label, target in edges.items())
    for field_name in ("next", "on_error", "default"):
        value = getattr(node, field_name, None)
        if isinstance(value, str):
            targets.append((field_name, value))
    return targets


def graph_references(node: NodeBase) -> list[tuple[str, str]]:
    """``(field, graph id)`` for every graph this node invokes."""
    references: list[tuple[str, str]] = []
    if isinstance(node, SubgraphNode):
        references.append(("graph", node.graph))
    if isinstance(node, GateNode):
        references.append(("redirect", node.redirect))
    return references


def is_terminal(node: NodeBase) -> bool:
    return isinstance(node, EndNode)
