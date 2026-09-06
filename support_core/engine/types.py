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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    """A message for the customer (DESIGN.md section 6.3)."""

    model_config = ConfigDict(extra="forbid")

    text: str
    author: Literal["agent", "human"] = "agent"
    """Who wrote it. ``human`` is the desk of DESIGN.md section 13 replying directly, which
    section 12 routes through "the same API surface" as everything else. A transcript that could
    not tell a person's sentences from a model's would not be much of an audit trail, and a
    customer is entitled to know which they are reading."""


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

    hint: str | None = None
    """What the engine learned about this message that the node could not (section 6.6 step 5).

    Today there is one: the customer changed the subject and the current graph refused to be
    interrupted, so the reply is *also* a request for something else. An ``ask`` node's slot
    extractor is given this, which is what stops "sure - and change my address" being read as
    the answer to "what is the six-digit code?"."""

    detail: dict[str, Any] = Field(default_factory=dict)
    """What the node recorded when it suspended (``run.awaiting.detail``), handed back to it.

    A node that suspends knows something the node resuming needs and has nowhere else to put:
    a ``confirm`` node's whole point is that the customer is answering *the proposal that was
    shown to them*, so the approval hash computed at suspend time travels with the event rather
    than being recomputed from a state the frame may have re-entered through a gate redirect in
    between (DESIGN.md sections 6.6, 8.2)."""

    target_frame_seq: int | None = None
    target_node_id: str | None = None
    """The node that suspended, and therefore the only node this event may be delivered to.

    Read from ``run.awaiting`` when the event is created and stored *with* the event, so it is
    durable state rather than a property of the stack as it happens to be when the loop runs.
    The stack moves inside a turn - a gate re-check pushes its redirect, and from phase 6 an
    interrupt pushes a workflow - so a process that resumed a crashed turn and looked at the
    stack top would deliver the customer's reply to whatever was pushed since (independent
    review finding R2). An event with no target is delivered to nobody and put back on the
    queue."""


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


class ApprovalProposal(BaseModel):
    """A ``confirm`` node's record that the customer said yes (DESIGN.md section 8.2).

    The node computes it; the *executor* writes it, in the checkpoint transaction, with the run,
    frame and node taken from the frame it is running - never from the node. A node cannot
    therefore claim an approval was given somewhere it was not, and the executor honours the
    field only from a node the graph declares as ``type: confirm``.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any]
    """The canonical arguments the hash was taken over."""

    args_hash: str
    approved_by: Literal["customer", "human"] = "customer"


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

    approval: ApprovalProposal | None = None
    """An ``ActionApproval`` to record with this checkpoint (DESIGN.md section 8.2)."""

    customer_patch: dict[str, Any] | None = None
    """A change to ``ctx.customer`` a tool asked for (DESIGN.md section 19 step 9).

    The whole new customer object, already validated by the tool runtime. Honoured only from a
    ``tool`` node: ``ctx`` is read-only to nodes (section 6.1), and this is a tool's effect
    travelling out through the node that called it, not a node writing context."""

    @model_validator(mode="after")
    def _one_way_out(self) -> "NodeResult":
        """At most one of ``suspend``, ``push_graph`` and ``pop``.

        The executor has to pick an order when a node asks for two of them, and whichever it
        picks silently discards the other. A node that returns richer results than phase 2's -
        an ``llm`` node that both pushes and suspends, say - would then lose half its intent
        with nothing to show for it (independent review finding R11).
        """
        chosen = [
            name
            for name, asked in (
                ("suspend", self.suspend is not None),
                ("push_graph", self.push_graph is not None),
                ("pop", self.pop),
            )
            if asked
        ]
        if len(chosen) > 1:
            msg = f"a node result may ask for only one of suspend, push_graph or pop, not {chosen}"
            raise ValueError(msg)
        return self


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

    errors: dict[str, int] = Field(default_factory=dict)
    """Consecutive failures per node id in this frame, cleared when the node succeeds.

    The bound on DESIGN.md section 7.3's retry story. A node whose ``on_error`` edge leads back
    to the node - a ``tool`` node retrying its own call is the shape phase 4's resolution left
    open - would otherwise repeat for as long as it keeps failing, and for a WRITE tool that
    needs no approval that is one side effect per failure. Counted here, in the frame, so it
    survives a crash like everything else a turn depends on; *consecutive*, so an ordinary loop
    that passes through a node many times is untouched."""

    offer_return: bool = False
    """This frame was interrupted and is parked (DESIGN.md section 6.6 step 4).

    Set on the frame that was on top when an interrupt pushed a workflow over it, and read when
    it is on top again: "When it ends, the engine asks the customer whether to return to the
    interrupted workflow, then resumes or abandons it." A field on the frame rather than a
    marker on the run, because a conversation can park more than one workflow and each one is
    offered back in the order the stack unwinds."""


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
