"""GLM (Z.ai / Zhipu) behind DESIGN.md section 11.1's provider interface.

DESIGN.md section 11.1 says the provider interface "is small enough that another provider is a
day of work", and section 2 fixes Claude as the default *behind an abstraction* rather than as a
dependency. This is the second provider, and it cost rather less than a day, because Z.ai serves
GLM through an endpoint that speaks the Anthropic message API. So this module is not a second
client: it is the same :class:`~support_core.llm.anthropic_provider.AnthropicProvider` pointed at
a different base URL, with the two optional fields that endpoint does not implement turned off.

What that means for the guarantees the rest of the system relies on:

* **Structured output still holds.** The answer is a forced tool call whose ``input_schema`` is
  the node's own schema, and :class:`~support_core.llm.provider.StructuredByCompletion` validates
  the result against the Pydantic model on the way back regardless. Dropping ``strict`` moves the
  refusal from the provider to that validation, which costs a retry rather than a guarantee.
* **The decision constraint still holds**, because it is enforced by the runner against the
  node's declared edges (DESIGN.md section 3, principle 2), not by the provider.
* **Prompt caching is off**, because the endpoint does not implement ``cache_control``. Nothing
  breaks; requests cost what they cost.

The model ids are GLM's, not Claude's, so a pack that names ``claude-sonnet-5`` needs its models
overridden. That is what ``models`` in the app configuration is for: it is a deployment choice,
not a pack edit, so one pack can run against either vendor.
"""

from __future__ import annotations

import os

from support_core.llm.anthropic_provider import (
    COMPATIBLE_ENDPOINT_CAPABILITIES,
    AnthropicProvider,
)

API_KEY_ENV = "GLM_API_KEY"
"""Preferred key. ``ZAI_API_KEY`` is accepted as an alias because Z.ai's own docs use it."""

ALT_API_KEY_ENV = "ZAI_API_KEY"

BASE_URL_ENV = "GLM_BASE_URL"
"""Override for a self-hosted or regional endpoint."""

DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
"""Z.ai's Anthropic-compatible endpoint for the GLM family."""

DEFAULT_MODEL = "glm-4.6"
DEFAULT_ESCALATION_MODEL = "glm-4.6"


def api_key() -> str | None:
    """The GLM key, under either accepted name."""
    return os.environ.get(API_KEY_ENV) or os.environ.get(ALT_API_KEY_ENV)


def api_key_present() -> bool:
    """Whether a live GLM call could be made at all."""
    return bool(api_key())


def base_url() -> str:
    """The endpoint to call, overridable for a regional or self-hosted deployment."""
    return os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL


def GlmProvider(
    *,
    key: str | None = None,
    url: str | None = None,
    max_retries: int = 2,
    timeout: float = 60.0,
) -> AnthropicProvider:
    """GLM through the Anthropic message API. See the module docstring for what is traded."""
    return AnthropicProvider(
        api_key=key or api_key(),
        base_url=url or base_url(),
        max_retries=max_retries,
        timeout=timeout,
        name="glm",
        capabilities=COMPATIBLE_ENDPOINT_CAPABILITIES,
    )
