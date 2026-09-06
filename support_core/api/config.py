"""How a deployment is configured. Implements DESIGN.md section 4.1 with 20 ("secrets via
environment").

DESIGN.md section 4.1 makes a deployment "one domain, one image, one service, one Postgres
database", with the pack in the image and everything else supplied from outside. Three things
have to be supplied from outside and none of them is a secret:

* **which pack** the service serves,
* **which model provider** answers - a live Anthropic account, or the recorded cassettes of
  PLAN.md's fake provider - which must be a matter of configuration and never of a code change,
  because the same image has to run in a demo without an API key and in production with one;
* **what a conversation on this deployment starts out knowing** - the
  :class:`~support_core.graph.context.ConversationContext` a new conversation is created with.
  It is configuration rather than something the client sends, because a browser that could
  describe its own customer record could describe somebody else's.

Secrets stay in the environment: the database URL comes from
:func:`support_core.storage.config.database_url` and the Anthropic key from the SDK's own
``ANTHROPIC_API_KEY``. Nothing here reads or stores either.
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ProviderChoice = Literal["auto", "replay", "anthropic", "none"]
"""What answers an ``llm`` node.

``auto`` is the deployable default: a live provider where there is an API key, the recorded
cassettes where there is not, and neither if the deployment configured no cassettes - a pack
with no ``llm`` nodes needs no provider at all and should not be made to invent one.
"""

RESOLVED: tuple[str, ...] = ("replay", "anthropic", "none")

ENV_CONFIG = "SUPPORT_APP_CONFIG"
ENV_PACK = "SUPPORT_PACK"
ENV_PROVIDER = "SUPPORT_LLM_PROVIDER"
ENV_CASSETTES = "SUPPORT_CASSETTE_DIR"
ENV_API_KEY = "ANTHROPIC_API_KEY"


class ConfigError(ValueError):
    """The application configuration is missing, unreadable or contradictory."""


class AppConfig(BaseModel):
    """Everything :func:`support_core.api.create_app` needs that is not the pack itself."""

    model_config = ConfigDict(extra="forbid")

    pack: Path = Path("packs/acme_billing")
    """The domain pack to serve. Relative paths resolve against the working directory."""

    provider: ProviderChoice = "auto"
    cassette_dir: Path | None = None
    """Recorded responses for the ``replay`` provider: every ``*.json`` cassette in the
    directory, merged. A request the recording has never seen is refused rather than guessed at,
    which is the property that makes a replayed conversation worth watching."""

    lock_wait_seconds: float = Field(default=0.0, ge=0.0)
    """How long an HTTP or WebSocket handler waits for the conversation lock. Zero - queue and
    return - is the default for exactly the reason phase 2's review finding R7 gives: a handler
    that waits holds a connection per waiter, and with turns that take seconds, N+1 messages on
    one conversation is a stall. The drain worker is what makes zero safe."""

    drain_workers: int = Field(default=4, ge=1, le=64)
    drain_budget_seconds: float = Field(default=60.0, gt=0.0)
    """How long the drain worker keeps trying for a conversation whose lock is held elsewhere.
    Giving up is safe: the message is durable and ``pending``, and the holder of the lock drains
    the queue in order. It is not free, though - until phase 7's scheduler calls
    ``recover_stalled``, a message nobody comes back for waits for the next inbound one."""

    new_conversation_context: dict[str, Any] = Field(default_factory=dict)
    """The ``ctx`` a conversation opened by a channel starts with (DESIGN.md section 10). In a
    real deployment this is where the CRM lookup's result goes; in the demo it is the one
    customer the sample pack's fake billing system has charges for. It can never contain
    ``identity_verified``: only the pack's verification workflow sets that."""

    suggestions: list[str] = Field(default_factory=list)
    """Messages the client offers as one-click starters. With the recorded provider these are
    the conversations that were recorded; with a live model they are only a hint."""

    serve_client: bool = True
    """Serve the built-in demo page at ``/``. Off for a deployment whose customers have their
    own front end."""

    title: str = "support-core"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AppConfig":
        """Build from ``SUPPORT_APP_CONFIG`` (a JSON file) with environment overrides.

        A file, because "which cassettes, which pack, what a new conversation knows" is a
        paragraph rather than a variable; environment overrides, because a container is
        configured with variables and DESIGN.md section 20 says so.
        """
        source = dict(env if env is not None else os.environ)
        config = cls.from_file(Path(source[ENV_CONFIG])) if source.get(ENV_CONFIG) else cls()
        values = config.model_dump()
        if source.get(ENV_PACK):
            values["pack"] = Path(source[ENV_PACK])
        if source.get(ENV_PROVIDER):
            values["provider"] = source[ENV_PROVIDER]
        if source.get(ENV_CASSETTES):
            values["cassette_dir"] = Path(source[ENV_CASSETTES])
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            msg = f"invalid configuration from the environment: {exc}"
            raise ConfigError(msg) from exc

    @classmethod
    def from_file(cls, path: Path) -> "AppConfig":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            msg = f"cannot read the application configuration at {path}: {exc}"
            raise ConfigError(msg) from exc
        except json.JSONDecodeError as exc:
            msg = f"{path} is not valid JSON: {exc}"
            raise ConfigError(msg) from exc
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            msg = f"{path} is not a valid application configuration: {exc}"
            raise ConfigError(msg) from exc

    def resolve_provider(self, env: Mapping[str, str] | None = None) -> str:
        """Which provider this configuration actually means, resolving ``auto``.

        ``auto`` prefers a live model when the account is there to pay for it, because that is
        what a deployment with a key wants; a demo without one falls back to the recording, and
        a deployment with neither gets ``none`` - which is a working configuration for a pack
        with no prompted nodes and a clear failure for one with them.
        """
        if self.provider != "auto":
            return self.provider
        source = env if env is not None else os.environ
        if source.get(ENV_API_KEY):
            return "anthropic"
        return "replay" if self.cassette_dir is not None else "none"
