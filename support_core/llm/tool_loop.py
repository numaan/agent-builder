"""The bounded, read-only tool loop inside an ``llm`` node. Implements DESIGN.md section 8.4 and
the first row of section 8.2's risk table.

    An ``llm`` node with ``tools:`` runs a bounded loop (default 5 iterations): model requests
    tool, runtime validates against the node's allowed list and READ tier, executes, feeds
    result back. ... This is where the model gathers facts; it is never where it acts.

Phase 4 owns tool execution, and what phase 3 left is a seam shaped so that phase 4 could not
widen it - :class:`~support_core.tools.runtime.RegistryToolRunner` is what fills it now.
:class:`ModelToolRunner` is the injectable half and it is never spoken to
directly: :class:`ReadOnlyToolGateway` wraps every runner, and an ``llm`` node only ever holds a
gateway. The gateway refuses, before the runner is called at all:

* a tool the node did not declare in its ``tools:`` list,
* a tool whose risk tier is not READ, checked against *both* the runner's own declaration and
  the pack's manifest, so a disagreement between them is a refusal rather than a coin toss,
* a call beyond the iteration or per-turn budget.

A refusal is fed back to the model as an error tool result rather than raised, because a model
asking for a tool it may not have is a thing that happens, not a system failure - and the model
needs to be told no in a form it can react to. Refusals count against the budget, so "ask for the
refund tool a thousand times" is not a way to burn the turn either.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from support_core.llm.types import LLMError, ModelTool, ToolCall
from support_core.tools.risk import MODEL_CALLABLE, Risk


class ToolsUnavailableError(LLMError):
    """A node declares tools but nothing can execute them: no tool runtime is configured."""


@dataclass(frozen=True, slots=True)
class ModelToolSpec:
    """A tool as the runtime knows it: what the model is told, plus the tier it is governed by."""

    name: str
    description: str
    input_schema: dict[str, Any]
    risk: Risk

    def as_model_tool(self) -> ModelTool:
        return ModelTool(
            name=self.name, description=self.description, input_schema=self.input_schema
        )


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What came back from a tool call, or why it did not happen."""

    content: str
    is_error: bool = False


class ModelToolRunner(Protocol):
    """The tool-execution seam. Deliberately two methods and no policy: the policy is the
    gateway's, above, and the runtime's, below (:mod:`support_core.tools.runtime`)."""

    async def describe(self, names: Sequence[str]) -> Sequence[ModelToolSpec]: ...

    async def invoke(
        self, name: str, arguments: Mapping[str, Any], *, call_id: str, step_id: str
    ) -> ToolOutcome: ...


class UnavailableToolRunner:
    """A runner for a runtime that has none: it refuses, loudly, rather than pretending.

    Used by a :class:`~support_core.engine.runners.NodeRuntime` built outside an executor. The
    executor always builds a real one from the pack's registry, so a node that reaches this has
    no tool runtime at all, and a node that gathers no facts must not answer as though it had.
    """

    async def describe(self, names: Sequence[str]) -> Sequence[ModelToolSpec]:
        msg = (
            f"this node offers the model {sorted(names)}, but no tool runtime is configured, so "
            f"no tool can be described to it"
        )
        raise ToolsUnavailableError(msg)

    async def invoke(
        self, name: str, arguments: Mapping[str, Any], *, call_id: str, step_id: str
    ) -> ToolOutcome:  # pragma: no cover - describe already refused
        msg = f"no tool runtime is configured; {name!r} cannot be called"
        raise ToolsUnavailableError(msg)


@dataclass(slots=True)
class ReadOnlyToolGateway:
    """Everything an ``llm`` node is allowed to do with tools, and nothing else."""

    runner: ModelToolRunner
    declared: tuple[str, ...]
    """The node's own ``tools:`` list (DESIGN.md section 6.4)."""

    manifest_risk: Mapping[str, Risk] = field(default_factory=dict)
    """Risk tiers the executor read from the pack's registry, as a cross-check against what the
    runner reports. Both now come from the imported ``TOOLS`` (phase-1 deferred finding I), so a
    disagreement means something is wrong with the wiring rather than with the pack - and it is
    still a refusal, because a tier nobody agrees on is not a tier."""

    max_calls: int = 5
    step_id: str = ""
    calls: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    _specs: dict[str, ModelToolSpec] | None = None

    async def specs(self) -> list[ModelTool]:
        """The tools the model may be told about: the node's declared list, READ tier only."""
        if not self.declared:
            return []
        described = await self.runner.describe(self.declared)
        resolved: dict[str, ModelToolSpec] = {}
        for spec in described:
            if spec.name not in self.declared:
                continue
            if self._refuse_reason(spec.name, spec.risk) is None:
                resolved[spec.name] = spec
        self._specs = resolved
        return [resolved[name].as_model_tool() for name in self.declared if name in resolved]

    async def call(self, call: ToolCall) -> ToolOutcome:
        """Run one model-requested tool call, or refuse it."""
        if len(self.calls) >= self.max_calls:
            # Not counted: the budget is already spent, and counting a refusal against a spent
            # budget would make ``calls`` grow without bound on a model that keeps asking.
            self.refusals.append(call.name)
            return ToolOutcome(
                content=f"refused: the tool budget for this node ({self.max_calls}) is spent",
                is_error=True,
            )
        spec = (self._specs or {}).get(call.name)
        if spec is None:
            return self._refuse(
                call.name,
                f"{call.name!r} is not one of the tools this step may use "
                f"({', '.join(self.declared) or 'none'})",
            )
        reason = self._refuse_reason(call.name, spec.risk)
        if reason is not None:
            return self._refuse(call.name, reason)
        self.calls.append(call.name)
        return await self.runner.invoke(
            call.name, call.arguments, call_id=call.id, step_id=self.step_id
        )

    def label_for(self, call: ToolCall) -> str:
        """The name to put in a fence label for this call's result.

        The *resolved* spec's name, or ``unknown tool`` when nothing resolved - which is every
        refusal. ``call.name`` comes from the model, and a fence label is core-written text
        (review finding V10).
        """
        spec = (self._specs or {}).get(call.name)
        return spec.name if spec is not None else "unknown tool"

    def _refuse(self, name: str, reason: str) -> ToolOutcome:
        self.refusals.append(name)
        self.calls.append(name)
        return ToolOutcome(content=f"refused: {reason}", is_error=True)

    def _refuse_reason(self, name: str, risk: Risk) -> str | None:
        """DESIGN.md section 8.2, transcribed into one decision."""
        if name not in self.declared:
            return f"{name!r} is not declared on this node"
        if risk not in MODEL_CALLABLE:
            return (
                f"{name!r} is {risk.value} risk; only read-tier tools may be called from a model "
                f"loop, and a write or high-risk tool needs a confirm node and a tool node"
            )
        declared_risk = self.manifest_risk.get(name)
        if declared_risk is not None and declared_risk != risk:
            return (
                f"{name!r} is declared {declared_risk.value} by the pack but reported "
                f"{risk.value} by the tool runtime; the two must agree before it can be called"
            )
        return None
