"""GLM as DESIGN.md section 11.1's second provider.

The point of these tests is not that GLM works - no key runs in CI - but that choosing it is a
configuration change and nothing else, and that the two fields its endpoint does not implement
are dropped without weakening anything the rest of the system relies on.
"""

from __future__ import annotations

import pytest

from support_core.api.config import AppConfig, ConfigError
from support_core.api.runtime import build_provider
from support_core.llm.anthropic_provider import (
    ANTHROPIC_CAPABILITIES,
    COMPATIBLE_ENDPOINT_CAPABILITIES,
    build_payload,
)
from support_core.llm.glm_provider import DEFAULT_BASE_URL, GlmProvider, base_url
from tests.test_llm_provider import a_request


def test_glm_is_reachable_by_configuration_alone() -> None:
    config = AppConfig.model_validate(
        {"pack": "packs/acme_billing", "provider": "glm", "models": {"default": "glm-4.6"}}
    )
    assert config.resolve_provider({}) == "glm"
    assert config.models.default == "glm-4.6"


def test_auto_picks_glm_when_only_a_glm_key_is_present() -> None:
    config = AppConfig(pack="packs/acme_billing")
    assert config.resolve_provider({"GLM_API_KEY": "k"}) == "glm"
    assert config.resolve_provider({"ZAI_API_KEY": "k"}) == "glm"
    assert config.resolve_provider({"ANTHROPIC_API_KEY": "k"}) == "anthropic"
    assert config.resolve_provider({}) == "none"


def test_glm_without_a_key_fails_at_startup_not_mid_conversation() -> None:
    config = AppConfig(pack="packs/acme_billing", provider="glm")
    with pytest.raises(ConfigError, match="GLM_API_KEY"):
        build_provider(config, "glm")


def test_the_endpoint_defaults_to_zai_and_is_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GLM_BASE_URL", raising=False)
    assert base_url() == DEFAULT_BASE_URL
    monkeypatch.setenv("GLM_BASE_URL", "https://example.invalid/anthropic")
    assert base_url() == "https://example.invalid/anthropic"


def test_a_compatible_endpoint_is_sent_neither_cache_control_nor_strict() -> None:
    """Both are optional extras. Dropping them costs money and a retry, not a guarantee."""
    request = a_request()
    anthropic = build_payload(request, ANTHROPIC_CAPABILITIES)
    compatible = build_payload(request, COMPATIBLE_ENDPOINT_CAPABILITIES)

    def strict_flags(payload: dict[str, object]) -> list[object]:
        tools = payload.get("tools") or []
        assert isinstance(tools, list)
        return [tool.get("strict") for tool in tools if isinstance(tool, dict)]

    assert any(flag is True for flag in strict_flags(anthropic))
    assert all(flag is None for flag in strict_flags(compatible))
    assert compatible["messages"] == anthropic["messages"]
    assert compatible.get("tool_choice") == anthropic.get("tool_choice")


def test_the_answer_schema_still_travels_without_strict() -> None:
    """The schema is what makes the answer checkable; only the up-front refusal is lost."""
    payload = build_payload(a_request(), COMPATIBLE_ENDPOINT_CAPABILITIES)
    tools = payload["tools"]
    assert isinstance(tools, list)
    assert any(isinstance(tool, dict) and tool.get("input_schema") for tool in tools)


def test_the_provider_reports_itself_as_glm() -> None:
    provider = GlmProvider(key="k", url="https://example.invalid/anthropic")
    assert provider.name == "glm"
    assert provider.capabilities.supports_cache_control is False
    assert provider.capabilities.supports_strict_tools is False
