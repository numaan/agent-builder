"""Executor, frame stack, interrupt check, checkpointing, limits. Implements DESIGN.md section 7
and 6.6. Populated in phases 2 and 6.

Phase 2 delivers the turn loop (7.1), suspension and resumption (7.2), the failure paths of 7.3
that need no tool runtime, the single-writer advisory lock (17) and the frame stack (6.1, 6.6).
Phase 4 adds the ``tool`` and ``confirm`` nodes and the per-node tool capability they run on
(:class:`~support_core.engine.runners.NodeToolAccess`). The pieces later phases own are hooks on
:class:`~support_core.engine.hooks.EngineHooks`, not holes in the loop.
"""

from support_core.engine.errors import (
    EngineError,
    IncompatiblePackError,
    NodeError,
    NodeNotExecutableError,
    StatePatchError,
)
from support_core.engine.executor import Executor
from support_core.engine.hooks import (
    ConfirmDecision,
    ConfirmRequest,
    EngineHooks,
    HandoffRequest,
    InterruptDecision,
)
from support_core.engine.locks import conversation_lock, lock_key
from support_core.engine.runners import (
    NodeRuntime,
    NodeToolAccess,
    build_runner,
    register_node_type,
    unregister_node_type,
)
from support_core.engine.types import (
    ApprovalProposal,
    Frame,
    GraphInvocation,
    Node,
    NodeResult,
    OutboundMessage,
    ResumeEvent,
    SuspendReason,
    TurnOutcome,
    step_id,
)

__all__ = [
    "ApprovalProposal",
    "ConfirmDecision",
    "ConfirmRequest",
    "EngineError",
    "EngineHooks",
    "Executor",
    "Frame",
    "GraphInvocation",
    "HandoffRequest",
    "IncompatiblePackError",
    "InterruptDecision",
    "Node",
    "NodeError",
    "NodeNotExecutableError",
    "NodeResult",
    "NodeRuntime",
    "NodeToolAccess",
    "OutboundMessage",
    "ResumeEvent",
    "StatePatchError",
    "SuspendReason",
    "TurnOutcome",
    "build_runner",
    "conversation_lock",
    "lock_key",
    "register_node_type",
    "step_id",
    "unregister_node_type",
]
