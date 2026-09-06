"""The MCP adapter. Implements DESIGN.md section 8.3's second and third bullets.

    ``McpToolAdapter(server_url, risk_map)`` discovers MCP tools and wraps each as a ``Tool``.
    The pack must supply a risk tier per MCP tool; unknown tools default to HIGH so nothing
    sneaks in as READ.

    Tool outputs are treated as untrusted data. They are inserted into prompts inside clearly
    delimited data blocks and never as instructions.

Both properties are structural here rather than advisory:

* every discovered tool becomes an ordinary :class:`~support_core.tools.base.Tool`, so the same
  registry, the same risk policy, the same approval binding and the same idempotency key apply
  to it as to a tool the pack wrote itself. There is no second execution path, and nothing about
  a tool's origin is visible to the runtime that runs it;
* a tool whose name the pack's ``risk_map`` does not mention is HIGH. Not "warn and default to
  READ", not "skip it": HIGH, which means a ``confirm`` node must authorise every call and the
  model-facing loop cannot reach it at all. A server that adds a tool overnight therefore adds
  something the graph has no way to call, which is the failure mode to want;
* an MCP result is text (or JSON) from another system. It comes back inside
  :class:`McpOutput`, whose ``content`` is a plain string, and every path that shows a tool
  result to a model puts it through phase 3's per-render data fence
  (:func:`~support_core.llm.prompt.data_block`). This module deliberately has no rendering of
  its own; inventing a second way to show untrusted text to a model is exactly what phase 3's
  review spent its findings on.

The transport is not this module's business. :class:`McpClient` is the two methods an MCP
session offers - ``list_tools`` and ``call_tool`` - which the ``mcp`` package's ``ClientSession``
satisfies as it stands. That keeps the adapter testable without a server and keeps the choice of
stdio, SSE or streamable HTTP where it belongs, in whatever wires the pack up.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, create_model

from support_core.tools.base import Tool, ToolContext, ToolFailed
from support_core.tools.risk import Risk

UNDECLARED_RISK = Risk.HIGH
"""DESIGN.md section 8.3: "unknown tools default to HIGH so nothing sneaks in as READ"."""


class McpToolInfo(BaseModel):
    """One tool as an MCP server describes it."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str | None = None
    inputSchema: dict[str, Any] = Field(default_factory=dict)


class McpOutput(BaseModel):
    """What an MCP call returns, as untrusted data.

    ``content`` is the server's text, verbatim and unparsed. It reaches a model only inside a
    fenced data block, and it reaches graph state as a string a pack's expressions can read but
    that no node interprets.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = ""
    is_error: bool = False
    structured: dict[str, Any] | None = None
    """The server's ``structuredContent``, when it sent one. Still data."""


@runtime_checkable
class McpClient(Protocol):
    """The two methods this adapter needs from an MCP session."""

    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...


class McpTool(Tool):
    """One MCP server tool, wrapped so the rest of the system cannot tell the difference."""

    client: Any
    remote_name: str

    async def run(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
        arguments = input.model_dump(mode="json", exclude_none=True)
        try:
            result = await self.client.call_tool(self.remote_name, arguments)
        except Exception as exc:
            msg = f"the MCP server failed to run {self.remote_name!r}: {type(exc).__name__}: {exc}"
            raise ToolFailed(msg) from exc
        return _as_output(result)


def _as_output(result: Any) -> McpOutput:
    """Flatten an MCP ``CallToolResult`` into text, without interpreting it.

    Deliberately dumb. The server's content blocks are joined with newlines and anything that is
    not text is rendered as JSON; nothing here decides that a block "means" a value, because a
    tool result is data and the pack's own expressions are where a meaning may be assigned.
    """
    if isinstance(result, McpOutput):
        return result
    is_error = bool(getattr(result, "isError", False))
    structured = getattr(result, "structuredContent", None)
    blocks = getattr(result, "content", None)
    if blocks is None:
        text = result if isinstance(result, str) else json.dumps(_jsonable(result), default=str)
    else:
        parts: list[str] = []
        for block in blocks:
            piece = getattr(block, "text", None)
            parts.append(
                piece if isinstance(piece, str) else json.dumps(_jsonable(block), default=str)
            )
        text = "\n".join(parts)
    return McpOutput(
        content=text,
        is_error=is_error,
        structured=dict(structured) if isinstance(structured, Mapping) else None,
    )


def _jsonable(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped: Any = dump(mode="json")
        return dumped
    return str(value)


class McpToolAdapter:
    """Discover a server's tools and wrap each as a native :class:`Tool` (DESIGN.md 8.3)."""

    def __init__(
        self,
        client: McpClient,
        risk_map: Mapping[str, Risk | str],
        *,
        prefix: str = "",
        timeout_s: float = 15.0,
        idempotent: Mapping[str, bool] | None = None,
        confirm_exempt: Mapping[str, str] | None = None,
    ) -> None:
        self.client = client
        self.risk_map = {name: Risk(risk) for name, risk in risk_map.items()}
        self.prefix = prefix
        self.timeout_s = timeout_s
        self.idempotent = dict(idempotent or {})
        self.confirm_exempt = dict(confirm_exempt or {})
        self.undeclared: list[str] = []
        """Tools the pack's ``risk_map`` did not mention, and which are therefore HIGH."""

    async def discover(self) -> list[Tool]:
        """List the server's tools and wrap each one."""
        listed = await self.client.list_tools()
        infos = getattr(listed, "tools", listed)
        return [self.wrap(McpToolInfo.model_validate(_jsonable_info(info))) for info in infos]

    def wrap(self, info: McpToolInfo) -> Tool:
        risk = self.risk_map.get(info.name)
        if risk is None:
            self.undeclared.append(info.name)
            risk = UNDECLARED_RISK
        local = f"{self.prefix}{info.name}"
        reason = self.confirm_exempt.get(info.name)
        return McpTool(
            name=local,
            description=(info.description or f"MCP tool {info.name!r}").strip() or info.name,
            input_model=input_model_for(local, info.inputSchema),
            output_model=McpOutput,
            risk=risk,
            # An MCP server makes no promise about repeating a call safely, so the safe default
            # is at-most-once unless the pack says otherwise for a named tool.
            idempotent=self.idempotent.get(info.name, risk is Risk.READ),
            confirm_exempt=bool(reason) and risk is Risk.WRITE,
            confirm_exempt_reason=reason if risk is Risk.WRITE else None,
            timeout_s=self.timeout_s,
            client=self.client,
            remote_name=info.name,
        )


def _jsonable_info(info: Any) -> Any:
    if isinstance(info, Mapping):
        return dict(info)
    return {
        "name": getattr(info, "name", ""),
        "description": getattr(info, "description", None),
        "inputSchema": dict(getattr(info, "inputSchema", {}) or {}),
    }


_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def input_model_for(name: str, schema: Mapping[str, Any]) -> type[BaseModel]:
    """Build a Pydantic input model from an MCP tool's JSON schema.

    Only the top level is typed, and anything the mapping above does not name becomes ``Any``.
    That is enough for the two jobs the model has - validating what a graph passes and giving
    the approval hash a canonical form - and pretending to a deeper understanding of an
    arbitrary JSON schema would be pretending.
    """
    properties = schema.get("properties")
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    if isinstance(properties, Mapping):
        for field_name, spec in properties.items():
            declared = spec.get("type") if isinstance(spec, Mapping) else None
            annotation = _JSON_TYPES.get(declared, Any) if isinstance(declared, str) else Any
            if field_name in required:
                fields[str(field_name)] = (annotation, ...)
            else:
                fields[str(field_name)] = (
                    annotation | None if annotation is not Any else Any,
                    None,
                )
    model: type[BaseModel] = create_model(
        f"{_class_name(name)}Input", __config__=ConfigDict(extra="forbid"), **fields
    )
    return model


def _class_name(name: str) -> str:
    return "".join(part.title() for part in name.replace("-", "_").split("_") if part) or "Mcp"


def wrap_tools(
    client: McpClient, infos: Sequence[McpToolInfo], risk_map: Mapping[str, Risk | str]
) -> list[Tool]:
    """Wrap an already-listed set of tools. Used by tests and by an offline manifest."""
    adapter = McpToolAdapter(client, risk_map)
    return [adapter.wrap(info) for info in infos]
