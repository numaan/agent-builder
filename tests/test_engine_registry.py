"""Node type registration and the manifest additions phase 2 made.

``register_node_type`` is DESIGN.md section 6.2's "custom node types are Python classes
registered by name in the pack", delivered early because two of DESIGN.md section 7.2's four
statuses have no core node type to suspend into. ``timeouts:`` is section 7.2's "timeouts are
per status and configurable per pack", which section 5.1's manifest has no block for.
"""

from dataclasses import replace

import pytest

from support_core.engine import build_runner, register_node_type, unregister_node_type
from support_core.engine.errors import NodeNotExecutableError
from support_core.engine.runners import NODE_RUNNERS, SayRunner
from support_core.graph.manifest import PackManifest, TimeoutRule, TimeoutsConfig
from support_core.graph.nodes import NODE_TYPES, executable_types
from tests.engine_support import BOOM_SPEC, BoomRunner, custom_node_types


def test_a_registered_type_is_visible_to_the_validator_and_the_engine() -> None:
    assert "boom" not in NODE_TYPES
    with custom_node_types():
        assert "boom" in NODE_TYPES
        assert "boom" in executable_types()
        block = NODE_TYPES["boom"].model.model_validate({"type": "boom", "next": "y"})
        assert isinstance(build_runner("x", block), BoomRunner)
    assert "boom" not in NODE_TYPES
    assert "boom" not in NODE_RUNNERS
    assert "boom" not in executable_types()


def test_a_core_node_type_cannot_be_replaced() -> None:
    """A pack must not be able to redefine ``tool`` or ``confirm`` and lose their safety."""
    with pytest.raises(ValueError, match="core type"):
        register_node_type(replace(BOOM_SPEC, name="say"), BoomRunner)
    assert NODE_RUNNERS["say"] is SayRunner


def test_a_second_registration_of_one_name_cannot_replace_the_first() -> None:
    """Review finding R6: a silent overwrite is how two packs corrupt each other.

    DESIGN.md section 6.7 keeps two pack versions loaded side by side, and phase 9 splits core
    from the packs; both packs defining a ``verify_identity`` node type must not be a race for
    who imported last. Registering the identical spec and runner again is a no-op, because
    loading the same pack twice is not an error.
    """

    class OtherRunner(BoomRunner):
        pass

    with custom_node_types():
        register_node_type(BOOM_SPEC, BoomRunner)  # idempotent
        assert NODE_RUNNERS["boom"] is BoomRunner
        with pytest.raises(ValueError, match="already registered"):
            register_node_type(BOOM_SPEC, OtherRunner)
        with pytest.raises(ValueError, match="already registered"):
            register_node_type(replace(BOOM_SPEC, executable_phase=9), BoomRunner)
        assert NODE_RUNNERS["boom"] is BoomRunner


def test_unregistering_something_that_was_never_registered_is_refused() -> None:
    with pytest.raises(ValueError, match="was not registered"):
        unregister_node_type("say")


async def test_a_node_type_core_cannot_run_names_its_phase() -> None:
    block = NODE_TYPES["handoff"].model.model_validate(
        {"type": "handoff", "reason": "x", "edges": {"resumed": "a", "closed": "b"}}
    )
    runner = build_runner("escalate", block)
    with pytest.raises(NodeNotExecutableError, match="not executable until phase 6"):
        await runner.run(None, None, _FakeRuntime())


class _FakeRuntime:
    """Only the attribute the error message reads."""

    class graph:
        id = "refund"


def test_timeouts_resolve_per_status_and_per_channel() -> None:
    config = TimeoutsConfig.model_validate(
        {
            "waiting_customer": {"seconds": 1800, "action": "close"},
            "channels": {"email": {"waiting_customer": {"seconds": 604800, "action": "none"}}},
        }
    )
    assert config.rule("waiting_customer", "web_chat") == TimeoutRule(seconds=1800, action="close")
    assert config.rule("waiting_customer", "email") == TimeoutRule(seconds=604800, action="none")
    assert config.rule("waiting_customer") == TimeoutRule(seconds=1800, action="close")
    # An unset status on an overridden channel falls back to the pack-level rule.
    assert config.rule("waiting_human", "email") == config.waiting_human


def test_the_default_timeouts_never_close_a_conversation_behind_the_authors_back() -> None:
    defaults = TimeoutsConfig()
    assert defaults.waiting_customer.seconds is None
    assert defaults.waiting_customer.action == "none"
    assert defaults.waiting_async_tool.action == "handoff"


def test_a_manifest_without_a_timeouts_block_still_parses(
    sample_manifest: dict[str, object],
) -> None:
    manifest = PackManifest.model_validate(sample_manifest)
    assert manifest.timeouts == TimeoutsConfig()


@pytest.fixture
def sample_manifest() -> dict[str, object]:
    return {
        "id": "acme-billing",
        "version": "1.0.0",
        "core": ">=0.0.1,<1",
        "entry_graph": "root",
        "channels": ["web_chat"],
        "llm": {"default_model": "claude-sonnet-5"},
        "handoff": {"queue": "q"},
    }
