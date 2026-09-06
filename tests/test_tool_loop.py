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
from support_core.tools.base import ToolRefused
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
    """A node with no tool runtime is told so, rather than answering as though it had one.

    The executor always builds a real runner from the pack's registry, so this is what a
    ``NodeRuntime`` constructed outside one does: it refuses at ``specs()``, before the model is
    told a tool exists, and the ``llm`` node turns that into a node error.
    """
    gateway = ReadOnlyToolGateway(runner=UnavailableToolRunner(), declared=("get_charge",))
    with pytest.raises(ToolsUnavailableError, match="no tool runtime is configured"):
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


async def test_a_custom_node_type_cannot_reach_the_tool_runner_through_its_runtime() -> None:
    """Review finding V4, and PLAN.md's every-phase rule.

    A pack-registered custom node type is arbitrary Python whose only handle on the outside is
    its :class:`~support_core.engine.runners.NodeRuntime`. While the runner was a public field on
    that dataclass, such a node could call ``rt.tool_runner.invoke("issue_refund", ...)`` and
    never meet the gateway - inert only because the default runner refuses everything, and a
    tool call with no ``ActionApproval`` the moment phase 4 installs a real one. The runner now
    lives in the closure the gateway factory captured.
    """
    import uuid

    from support_core import load_pack
    from support_core.engine.hooks import EngineHooks
    from support_core.engine.runners import NodeRuntime, tool_gateway_factory
    from support_core.engine.types import Frame
    from tests.engine_support import PACKS

    runner = Willing(HIGH)
    pack = load_pack(PACKS / "llm_pack")
    graph = pack.graphs["root"]
    runtime = NodeRuntime(
        graph=graph,
        frame=Frame(frame_seq=0, graph_id="root", node_id="classify", kind="root", state={}),
        step_id="s",
        run_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        hooks=EngineHooks(),
        environment=pack.environment,
        tool_gateway=tool_gateway_factory(runner, tool_risk={}, max_calls=5, step_id="s"),
    )

    # Nothing a node can read off its runtime is a tool runtime. Phase 4 adds exactly one
    # attribute with an ``invoke``, and it is the per-node capability the executor built: for a
    # node that is not a ``tool`` node it refuses every call, and it carries no reference to the
    # ToolRuntime underneath (there is none to reach: the executor holds it).
    reachable = [
        name
        for name in dir(runtime)
        if not name.startswith("__") and hasattr(getattr(runtime, name, None), "invoke")
    ]
    assert reachable == ["tools"]
    with pytest.raises(ToolRefused, match="may not invoke tools"):
        await runtime.tools.invoke("issue_refund", {"charge_id": "ch_1", "amount": 29.0})
    with pytest.raises(ToolRefused, match="may not invoke tools"):
        await runtime.tools.complete("issue_refund", {})

    # The one route that does exist refuses a HIGH-tier tool, even a declared one.
    gateway = runtime.tool_gateway(["issue_refund"])
    assert await gateway.specs() == []
    outcome = await gateway.call(ToolCall(id="tu", name="issue_refund"))
    assert outcome.is_error
    assert runner.invoked == []
