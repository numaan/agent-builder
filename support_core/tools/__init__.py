"""Tool base, registry, risk policy, idempotency, MCP adapter. Implements DESIGN.md section 8.

What a pack imports from here is small on purpose (DESIGN.md section 18: "Public API surface a
pack touches (kept deliberately small)"): :class:`Tool`, :class:`FunctionTool`, :class:`Risk`
and :class:`ToolContext` are enough to write a tool, and :class:`McpToolAdapter` is enough to
adopt somebody else's.

:mod:`support_core.tools.runtime` - the thing that actually executes a call - is deliberately
*not* re-exported. It is the engine's to hold, not a pack's: a pack that could reach a
:class:`~support_core.tools.runtime.ToolRuntime` could invoke a HIGH tool without going through
the node that binds it to an approval. It is imported by path where it is needed.
"""

from support_core.tools.base import (
    ApprovalRecord,
    FunctionTool,
    Tool,
    ToolContext,
    ToolError,
    ToolFailed,
    ToolRefused,
)
from support_core.tools.mcp import McpClient, McpOutput, McpToolAdapter, McpToolInfo
from support_core.tools.registry import RegistryError, ToolRegistry
from support_core.tools.risk import MODEL_CALLABLE, REQUIRES_CONFIRM, Risk, needs_confirm

__all__ = [
    "MODEL_CALLABLE",
    "REQUIRES_CONFIRM",
    "ApprovalRecord",
    "FunctionTool",
    "McpClient",
    "McpOutput",
    "McpToolAdapter",
    "McpToolInfo",
    "RegistryError",
    "Risk",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolFailed",
    "ToolRefused",
    "ToolRegistry",
    "needs_confirm",
]
