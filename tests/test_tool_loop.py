"""The model-facing tool gateway. DESIGN.md sections 8.2 (the risk table) and 8.4 (the loop).

Phase 4 owns tool execution. What phase 3 owns is that when it arrives it cannot widen what a
model is allowed to do: the node holds a :class:`ReadOnlyToolGateway`, the gateway holds the
runner, and every refusal below happens before the runner is spoken to.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from support_core.llm.tool_loop import (
    ModelToolSpec,
    ReadOnlyToolGateway,
    ToolOutcome,
    ToolsUnavailableError,
    UnavailableToolRunner,
)
from support_core.llm.types import ToolCall
from support_core.tools.risk import Risk


class Willing:
    """A runner with no scruples: it describes and runs whatever it is asked for."""

    def __init__(self, *specs: ModelToolSpec) -> None:
        self.specs = list(specs)
        self.invoked: list[str] = []

    async def describe(self, names: Sequence[str]) -> Sequence[ModelToolSpec]:
        return self.specs

    async def invoke(
        self, name: str, arguments: Mapping[str, Any], *, call_id: str, step_id: str
    ) -> ToolOutcome:
        self.invoked.append(name)
        return ToolOutcome(content="done")


def spec(name: str, risk: Risk) -> ModelToolSpec:
    return ModelToolSpec(name=name, description=name, input_schema={"type": "object"}, risk=risk)


READ = spec("get_charge", Risk.READ)
WRITE = spec("send_otp", Risk.WRITE)
HIGH = spec("issue_refund", Risk.HIGH)


async def test_only_read_tier_tools_are_offered_to_the_model() -> None:
    runner = Willing(READ, WRITE, HIGH)
    gateway = ReadOnlyToolGateway(
        runner=runner, declared=("get_charge", "send_otp", "issue_refund")
    )
    assert [tool.name for tool in await gateway.specs()] == ["get_charge"]


@pytest.mark.parametrize("tool", [WRITE, HIGH])
async def test_a_write_or_high_tool_is_refused_even_when_the_node_declares_it(
    tool: ModelToolSpec,
) -> None:
    """DESIGN.md section 8.2: "From ``llm`` node loop: never" for WRITE and HIGH.

    The pack declaring it on the node is not enough, and neither is the runtime being willing.
    """
    runner = Willing(READ, tool)
    gateway = ReadOnlyToolGateway(runner=runner, declared=("get_charge", tool.name))
    await gateway.specs()
    outcome = await gateway.call(ToolCall(id="tu", name=tool.name))
    assert outcome.is_error
    assert "not one of the tools this step may use" in outcome.content
    assert runner.invoked == []


async def test_a_tool_the_node_did_not_declare_is_refused() -> None:
    runner = Willing(READ, spec("other_read", Risk.READ))
    gateway = ReadOnlyToolGateway(runner=runner, declared=("get_charge",))
    await gateway.specs()
    outcome = await gateway.call(ToolCall(id="tu", name="other_read"))
    assert outcome.is_error
    assert runner.invoked == []


async def test_a_disagreement_about_the_risk_tier_is_a_refusal() -> None:
    """Phase-1 deferred finding I: risk tiers are self-declared until phase 4's registry.

    Two sources say what ``get_charge`` is. If they disagree, the safe reading is that neither
    can be trusted, so the call does not happen.
    """
    runner = Willing(READ)
    gateway = ReadOnlyToolGateway(
        runner=runner, declared=("get_charge",), manifest_risk={"get_charge": Risk.HIGH}
    )
    assert await gateway.specs() == []
    outcome = await gateway.call(ToolCall(id="tu", name="get_charge"))
    assert outcome.is_error
    assert runner.invoked == []


async def test_a_matching_risk_tier_is_allowed_through() -> None:
    runner = Willing(READ)
    gateway = ReadOnlyToolGateway(
        runner=runner, declared=("get_charge",), manifest_risk={"get_charge": Risk.READ}
    )
    await gateway.specs()
    outcome = await gateway.call(ToolCall(id="tu", name="get_charge", arguments={"id": "c1"}))
    assert not outcome.is_error
    assert runner.invoked == ["get_charge"]


async def test_refusals_count_against_the_budget() -> None:
    """Asking for a forbidden tool for ever is not a way to spend the turn."""
    runner = Willing(READ, HIGH)
    gateway = ReadOnlyToolGateway(
        runner=runner, declared=("get_charge", "issue_refund"), max_calls=3
    )
    await gateway.specs()
    for _ in range(5):
        await gateway.call(ToolCall(id="tu", name="issue_refund"))
    assert len(gateway.calls) == 3
    assert "budget" in (await gateway.call(ToolCall(id="tu", name="get_charge"))).content


async def test_the_default_runner_refuses_to_describe_anything() -> None:
    """Nothing executes a tool in phase 3, and the model is not told otherwise."""
    gateway = ReadOnlyToolGateway(runner=UnavailableToolRunner(), declared=("get_charge",))
    with pytest.raises(ToolsUnavailableError, match="phase 4"):
        await gateway.specs()


async def test_a_node_with_no_tools_never_reaches_the_runner() -> None:
    gateway = ReadOnlyToolGateway(runner=UnavailableToolRunner(), declared=())
    assert await gateway.specs() == []


async def test_a_call_before_specs_were_resolved_is_refused() -> None:
    """Defence in depth: the allow-list is built by :meth:`specs`, so a call that skipped it has
    nothing to check against and is refused rather than trusted."""
    runner = Willing(READ)
    gateway = ReadOnlyToolGateway(runner=runner, declared=("get_charge",))
    outcome = await gateway.call(ToolCall(id="tu", name="get_charge"))
    assert outcome.is_error
    assert runner.invoked == []
