"""Shared helpers for the engine tests. Not a second execution path: everything here either
builds an :class:`~support_core.engine.executor.Executor` or reads the database it wrote.

The phase-1 in-memory stepper (``tests/stepper.py``) is gone: phase 2's executor took over its
job, and keeping two loops that walk a frame stack would guarantee they drift.
"""

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.engine.errors import NodeError
from support_core.engine.hooks import (
    EngineHooks,
    HandoffRequest,
    InterruptDecision,
    InterruptRequest,
    SlotRequest,
)
from support_core.engine.runners import NodeRuntime, register_node_type, unregister_node_type
from support_core.engine.types import (
    NodeResult,
    OutboundMessage,
    ResumeEvent,
    SuspendReason,
)
from support_core.graph.context import ConversationContext
from support_core.graph.nodes import NodeBase, NodeTypeSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS = REPO_ROOT / "tests" / "packs"
DETERMINISTIC_PACK = PACKS / "deterministic_pack"
ENGINE_PACK = PACKS / "engine_pack"


class SimulatedCrash(RuntimeError):
    """Raised by the probe hook to stand in for a process that stopped existing."""


@dataclass(slots=True)
class Recorder:
    """Hooks that record what the engine asked for, and can crash it on cue."""

    handoffs: list[HandoffRequest] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)
    crash_at: str | None = None
    crash_after: int = 0
    """Crash on the ``crash_after``-th visit to ``crash_at`` (0 = the first)."""

    interrupt: InterruptDecision | None = None
    interrupts: list[InterruptRequest] = field(default_factory=list)
    """Every interrupt check the engine ran, so a test can assert it did *not* run one."""

    slots: dict[str, Any] | None = None
    handoff_delivers: bool = True
    """Whether the recorded handoff hook reports that a human was actually told (finding P2).
    Set it to False to stand in for a queue nothing would take the packet from."""

    now: datetime = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    seen: dict[str, int] = field(default_factory=dict)

    def hooks(self) -> EngineHooks:
        return EngineHooks(
            handoff=self._handoff,
            send=self._send,
            probe=self._probe,
            interrupt_check=self._interrupt,
            extract_slots=self._extract,
            clock=self._clock,
        )

    def _clock(self) -> datetime:
        """A clock that always moves forward, so timestamps are distinct and predictable."""
        self.now += timedelta(milliseconds=1)
        return self.now

    async def _handoff(self, request: HandoffRequest) -> bool:
        self.handoffs.append(request)
        return self.handoff_delivers

    async def _send(self, conversation_id: uuid.UUID, messages: Sequence[OutboundMessage]) -> None:
        self.sent.extend(message.text for message in messages)

    async def _probe(self, point: str, detail: dict[str, Any]) -> None:
        self.probes.append(point)
        if point != self.crash_at:
            return
        seen = self.seen.get(point, 0)
        self.seen[point] = seen + 1
        if seen == self.crash_after:
            raise SimulatedCrash(f"{point} at {detail.get('step_id')}")

    async def _interrupt(self, request: InterruptRequest) -> InterruptDecision:
        self.interrupts.append(request)
        return self.interrupt or InterruptDecision(kind="continue")

    async def _extract(self, request: SlotRequest) -> dict[str, Any]:
        if self.slots is not None:
            return dict(self.slots)
        return {request.slots[0]: request.reply} if request.slots else {}


# -- reading what the engine wrote --------------------------------------------------------


async def run_row(engine: AsyncEngine, conversation_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT * FROM run WHERE conversation_id = :c"), {"c": conversation_id}
        )
        return dict(result.mappings().one())


async def trace_rows(engine: AsyncEngine, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every trace step of a run in ``seq`` order, the total order finding F5 added."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT step_id, seq, node_id, edge, state_patch, error, started_at, ended_at "
                "FROM trace_step WHERE run_id = :r ORDER BY seq"
            ),
            {"r": run_id},
        )
        return [dict(row) for row in result.mappings()]


async def messages(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT direction, author, text, status, queue_seq FROM message "
                "WHERE conversation_id = :c ORDER BY created_at, ordinal, id"
            ),
            {"c": conversation_id},
        )
        return [dict(row) for row in result.mappings()]


async def outbound_texts(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[str]:
    rows = await messages(engine, conversation_id)
    return [row["text"] for row in rows if row["direction"] == "outbound"]


async def path(engine: AsyncEngine, run_id: uuid.UUID) -> list[str]:
    return [row["node_id"] for row in await trace_rows(engine, run_id)]


async def tool_calls(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every ``tool_call`` of a conversation, joined through the run (phase-0 finding F6)."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT tc.id, tc.idempotency_key, tc.tool, tc.args, tc.result, tc.risk, "
                "       tc.status, tc.error, tc.attempts, tc.node_id, tc.step_id, tc.approval_id, "
                "       tc.context_patch "
                "FROM tool_call tc JOIN run r ON r.id = tc.run_id "
                "WHERE r.conversation_id = :c ORDER BY tc.created_at, tc.id"
            ),
            {"c": conversation_id},
        )
        return [dict(row) for row in result.mappings()]


async def approvals(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT tool, args, args_hash, approved_by, node_id, frame_seq, step_id, "
                "       consumed_at, consumed_by_tool_call_id "
                "FROM action_approval WHERE conversation_id = :c ORDER BY approved_at, id"
            ),
            {"c": conversation_id},
        )
        return [dict(row) for row in result.mappings()]


async def set_context(
    engine: AsyncEngine, conversation_id: uuid.UUID, context: dict[str, Any]
) -> None:
    """Rewrite ``conversation.context``, standing in for a CRM change between turns."""
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE conversation SET context = CAST(:ctx AS jsonb) WHERE id = :c"),
            {"ctx": json.dumps(context), "c": conversation_id},
        )


# -- node types registered only for the tests ---------------------------------------------


class WaitNode(NodeBase):
    """Suspends into a chosen status. Stands in for phase 4's async tool and the scheduler."""

    type: Literal["wait"]
    status: Literal["waiting_async_tool", "waiting_timer", "waiting_human"] = "waiting_async_tool"
    next: str


class BoomNode(NodeBase):
    """Always fails, for the DESIGN.md section 7.3 routing tests."""

    type: Literal["boom"]
    on_error: str | None = None
    next: str


class WaitRunner:
    """Implements the DESIGN.md section 6.3 node protocol directly, as a pack's own node would."""

    def __init__(self, node_id: str, node: NodeBase) -> None:
        assert isinstance(node, WaitNode)
        self.id = node_id
        self.type = node.type
        self.node = node

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        return NodeResult(suspend=SuspendReason(status=self.node.status, detail={"node": self.id}))

    async def resume(
        self,
        state: BaseModel,
        ctx: ConversationContext,
        rt: NodeRuntime,
        event: ResumeEvent,
    ) -> NodeResult:
        patch = {k: v for k, v in event.payload.items() if k in type(state).model_fields}
        return NodeResult(state_patch=patch)


class CrashNode(NodeBase):
    """Fails with an exception the engine does *not* route (not a ``NodeError``).

    Phase 2 used a pack that reached an ``llm`` node for this, because core refused to run one.
    Phase 3 runs them, so the "conversation core cannot get through" case needs a node that is
    unroutable on purpose rather than one that is merely unimplemented - and this one stays
    unroutable however many phases arrive.
    """

    type: Literal["crash"]
    next: str


class CrashRunner:
    def __init__(self, node_id: str, node: NodeBase) -> None:
        self.id = node_id
        self.type = node.type

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        msg = f"{self.id}: this node fails in a way the engine cannot route"
        raise RuntimeError(msg)

    async def resume(
        self,
        state: BaseModel,
        ctx: ConversationContext,
        rt: NodeRuntime,
        event: ResumeEvent,
    ) -> NodeResult:  # pragma: no cover - a crash node never suspends
        msg = f"{self.id}: this node fails in a way the engine cannot route"
        raise RuntimeError(msg)


class BoomRunner:
    def __init__(self, node_id: str, node: NodeBase) -> None:
        self.id = node_id
        self.type = node.type

    async def run(self, state: BaseModel, ctx: ConversationContext, rt: NodeRuntime) -> NodeResult:
        msg = f"{self.id}: this node always fails"
        raise NodeError(msg)

    async def resume(
        self,
        state: BaseModel,
        ctx: ConversationContext,
        rt: NodeRuntime,
        event: ResumeEvent,
    ) -> NodeResult:  # pragma: no cover - a boom node never suspends
        msg = f"{self.id}: this node always fails"
        raise NodeError(msg)


WAIT_SPEC = NodeTypeSpec(
    name="wait",
    model=WaitNode,
    chooses_edge=False,
    suspends="waiting_async_tool",
    executable=True,
    executable_phase=2,
)
CRASH_SPEC = NodeTypeSpec(
    name="crash",
    model=CrashNode,
    chooses_edge=False,
    suspends=None,
    executable=True,
    executable_phase=2,
)
BOOM_SPEC = NodeTypeSpec(
    name="boom",
    model=BoomNode,
    chooses_edge=False,
    suspends=None,
    executable=True,
    executable_phase=2,
)


@contextmanager
def custom_node_types() -> Iterator[None]:
    """Register ``wait``, ``boom`` and ``crash`` for the duration of the block."""
    register_node_type(WAIT_SPEC, WaitRunner)
    register_node_type(BOOM_SPEC, BoomRunner)
    register_node_type(CRASH_SPEC, CrashRunner)
    try:
        yield None
    finally:
        unregister_node_type("wait")
        unregister_node_type("boom")
        unregister_node_type("crash")
