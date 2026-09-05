"""The engine's data types. Implements DESIGN.md sections 6.1, 6.3, 7.1 and 7.2.

DESIGN.md section 6.3 gives :class:`NodeResult` and the :class:`Node` protocol almost verbatim;
this module adds the types that section names but does not spell (``OutboundMessage``,
``SuspendReason``, ``ResumeEvent``, ``GraphInvocation``) and the :class:`Frame` of section 6.1
("Frames live on a stack").

Everything here is a Pydantic model because the frame stack is stored as ``run.frames`` JSONB
(section 17) and read back by a different process, possibly days later and possibly after a
crash. A frame is therefore never allowed to hold a live object: it holds ``state`` as JSON and
the executor rebuilds the graph's state model from it.

**The frame stack is not only for sub-graph calls.** DESIGN.md section 6.6 pushes a frame when a
customer interrupts a suspended workflow, and a ``gate`` pushes its redirect graph.
:attr:`Frame.kind` names which of those a frame is, from the first commit, so phase 6 adds
interrupt *behaviour* without changing the frame shape or migrating stored stacks.
"""

import uuid
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from support_core.graph.context import ConversationContext
from support_core.graph.manifest import SuspendStatus

RunStatus = Literal[
    "idle",
    "running",
    "waiting_customer",
    "waiting_human",
    "waiting_async_tool",
    "waiting_timer",
    "done",
]
"""``run.status``. The four ``waiting_*`` values are DESIGN.md section 7.2; ``running`` marks a
turn in progress, so a run found ``running`` with a free advisory lock is one whose process died
and must be re-entered from its last checkpoint (section 7.3)."""

SUSPEND_STATUSES: frozenset[str] = frozenset(
    ["waiting_customer", "waiting_human", "waiting_async_tool", "waiting_timer"]
)

FrameKind = Literal["root", "subgraph", "gate_redirect", "interrupt"]
"""Why a frame is on the stack. ``interrupt`` is DESIGN.md section 6.6 and is unused until
phase 6; it is declared now so the stored shape does not change then."""

MessageStatus = Literal["pending", "received", "pending_send", "sent"]
"""``message.status``. Inbound arrives ``pending`` (section 17 "Concurrency") and becomes
``received`` when a turn claims it; outbound is written ``pending_send`` inside the checkpoint
transaction and becomes ``sent`` once a channel adapter (phase 7) has delivered it."""


class OutboundMessage(BaseModel):
    """A message for the customer produced by a node (DESIGN.md section 6.3)."""

    model_config = ConfigDict(extra="forbid")

    text: str
    author: Literal["agent"] = "agent"


class SuspendReason(BaseModel):
    """Why a node stopped and what will wake it (DESIGN.md section 7.2)."""

    model_config = ConfigDict(extra="forbid")

    status: SuspendStatus
    detail: dict[str, Any] = Field(default_factory=dict)
    """Free-form record of what is awaited, stored in ``run.awaiting``."""


class ResumeEvent(BaseModel):
    """What woke a suspended run (DESIGN.md section 7.2, "Resumed by")."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["customer_message", "human", "async_tool", "timer"]
    text: str | None = None
    """The customer's or human's message, when there is one."""

    payload: dict[str, Any] = Field(default_factory=dict)
    """Structured result, for an async tool callback or a desk state patch."""

    message_id: uuid.UUID | None = None


class GraphInvocation(BaseModel):
    """A frame a node asks the engine to push (DESIGN.md section 6.3 ``push_graph``)."""

    model_config = ConfigDict(extra="forbid")

    graph: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs_into: dict[str, str] = Field(default_factory=dict)
    """Caller state field to callee output name, the direction DESIGN.md section 6.4 uses."""

    kind: FrameKind = "subgraph"
    return_node: str | None = None
    """Node in the caller to continue at when this frame pops. A ``gate`` names *itself*, so the
    predicate is re-evaluated after its redirect returns (section 6.2)."""


class NodeResult(BaseModel):
    """What a node returns (DESIGN.md section 6.3).

    ``pop`` and ``outputs`` are additions: section 6.3 lists the fields an ``llm`` or ``tool``
    node needs and section 6.2 says an ``end`` node "pops the frame, returns outputs" without
    saying how it says so. Keeping that in the node rather than special-casing the node type in
    the executor is what lets a pack-registered node type end a frame too.
    """

    model_config = ConfigDict(extra="forbid")

    state_patch: dict[str, Any] = Field(default_factory=dict)
    next_edge: str | None = None
    """The label of the edge to follow. ``None`` means the node's single ``next``."""

    outbound: list[OutboundMessage] = Field(default_factory=list)
    suspend: SuspendReason | None = None
    push_graph: GraphInvocation | None = None
    pop: bool = False
    outputs: dict[str, Any] = Field(default_factory=dict)
    llm_response: dict[str, Any] | None = None
    """Recorded on the trace step, so a replay of a completed step does not call a model
    again (DESIGN.md section 7.3). Phase 3 fills it."""


class Frame(BaseModel):
    """One active graph invocation (DESIGN.md sections 6.1 and 6.6)."""

    model_config = ConfigDict(extra="forbid")

    frame_seq: int
    """Unique and monotonic within the run, and never reused after a pop: it is the second
    field of the ``run_id:frame_seq:node_id:attempt`` step id (DESIGN.md section 7.1), so two
    invocations of the same graph must not share it."""

    graph_id: str
    node_id: str
    state: dict[str, Any] = Field(default_factory=dict)
    """The frame's state, as JSON. Rebuilt into the graph's state model on every entry."""

    kind: FrameKind = "root"
    return_node: str | None = None
    outputs_into: dict[str, str] = Field(default_factory=dict)
    attempts: dict[str, int] = Field(default_factory=dict)
    """Completed executions per node id in this frame; the fourth field of the step id. A node
    re-executed after a crash reuses its number, so the step id - and therefore phase 4's tool
    idempotency key - is the same on the retry."""

    passed_gates: list[str] = Field(default_factory=list)
    """Gate nodes this frame has satisfied, in the order they were passed. Re-evaluated on
    every entry to the frame (DESIGN.md section 6.6: "Gates fire on every entry to a frame, so
    an interrupt cannot be used to reach an unverified action")."""


class TurnOutcome(BaseModel):
    """What one call into the engine did. Returned by ``on_inbound`` and the resume methods."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    run_id: uuid.UUID | None = None
    status: RunStatus | None = None
    queued: bool = False
    """True when the caller could not take the conversation lock: the message is durable with
    ``status = pending`` and whichever process holds the lock will process it in order."""

    messages_processed: int = 0
    steps: list[str] = Field(default_factory=list)
    """Step ids executed by this call, in order."""

    outbound: list[str] = Field(default_factory=list)
    handoff_reason: str | None = None


@runtime_checkable
class Node(Protocol):
    """The node protocol of DESIGN.md section 6.3.

    Implemented by the runners in :mod:`support_core.engine.runners`, one instance per node in
    a graph. It is deliberately separate from the *configuration* models in
    :mod:`support_core.graph.nodes`: those describe the YAML a pack author writes and are read
    by the validator, this describes behaviour and is read by the executor.
    """

    id: str
    type: str

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: Any) -> NodeResult: ...

    async def resume(
        self, state: BaseModel, ctx: ConversationContext, rt: Any, event: ResumeEvent
    ) -> NodeResult: ...


def step_id(run_id: uuid.UUID, frame_seq: int, node_id: str, attempt: int) -> str:
    """The deterministic step id of DESIGN.md section 7.1.

    "The step id is deterministic (``run_id:frame_seq:node_id:attempt``) and is used as the
    idempotency key for tool calls and as the cache key for LLM calls during replay." Every
    field is taken from durable state, so a step re-executed after a crash computes the same id
    and phase 4's tool call is deduplicated by it.
    """
    return f"{run_id}:{frame_seq}:{node_id}:{attempt}"
