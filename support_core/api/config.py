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

from support_core.storage.config import DEFAULT_MAX_CONCURRENT_TURNS

ProviderChoice = Literal["auto", "replay", "anthropic", "glm", "none"]
"""What answers an ``llm`` node.

``auto`` is the deployable default: a live provider where there is an API key, the recorded
cassettes where there is not, and neither if the deployment configured no cassettes - a pack
with no ``llm`` nodes needs no provider at all and should not be made to invent one.
"""

ENV_CONFIG = "SUPPORT_APP_CONFIG"
ENV_PACK = "SUPPORT_PACK"
ENV_PROVIDER = "SUPPORT_LLM_PROVIDER"
ENV_CASSETTES = "SUPPORT_CASSETTE_DIR"
ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_MODEL = "SUPPORT_MODEL"
ENV_ESCALATION_MODEL = "SUPPORT_ESCALATION_MODEL"
ENV_DESK_TOKEN = "SUPPORT_DESK_TOKEN"
ENV_MAX_CONCURRENT_TURNS = "SUPPORT_MAX_CONCURRENT_TURNS"
"""How many turns this replica runs at once. A variable as well as a config-file field because
it is the one setting an operator changes in response to load, and DESIGN.md section 20 says a
container is configured with variables."""

ENV_QDRANT_URL = "SUPPORT_QDRANT_URL"
"""The same variable :mod:`support_core.knowledge.config` reads, so the CLI's sync and the
service's retriever cannot end up pointed at two different instances."""

MIN_DESK_TOKEN = 16
"""Shortest desk credential accepted.

A number rather than a judgement, because "is this a real token" has to be decidable at startup.
Sixteen characters is what ``secrets.token_hex(8)`` gives and is comfortably past the length at
which a credential is a placeholder somebody meant to replace."""


class ConfigError(ValueError):
    """The application configuration is missing, unreadable or contradictory."""


class ModelOverride(BaseModel):
    """Model ids for this deployment, overriding the ones ``pack.yaml`` names.

    DESIGN.md section 11.1 puts model choice in the pack, which is right: a pack author knows
    which model their prompts were written for. It is wrong the moment the same pack is served
    against a different vendor, because ``claude-sonnet-5`` is not a name GLM answers to. Making
    that a deployment setting keeps the vendor out of the pack, which is what having a provider
    abstraction was for.
    """

    model_config = ConfigDict(extra="forbid")

    default: str | None = None
    escalation: str | None = None


class AppConfig(BaseModel):
    """Everything :func:`support_core.api.create_app` needs that is not the pack itself."""

    model_config = ConfigDict(extra="forbid")

    pack: Path = Path("packs/acme_billing")
    """The domain pack to serve. Relative paths resolve against the working directory."""

    provider: ProviderChoice = "auto"

    models: ModelOverride = Field(default_factory=lambda: ModelOverride())
    """Model ids for this deployment. Empty means the pack's own choice stands."""

    cassette_dir: Path | None = None
    """Recorded responses for the ``replay`` provider: every ``*.json`` cassette in the
    directory, merged. A request the recording has never seen is refused rather than guessed at,
    which is the property that makes a replayed conversation worth watching."""

    lock_wait_seconds: float = Field(default=0.0, ge=0.0)
    """How long an HTTP or WebSocket handler waits for the conversation lock. Zero - queue and
    return - is the default for exactly the reason phase 2's review finding R7 gives: a handler
    that waits holds a connection per waiter, and with turns that take seconds, N+1 messages on
    one conversation is a stall. The drain worker is what makes zero safe."""

    max_concurrent_turns: int = Field(default=DEFAULT_MAX_CONCURRENT_TURNS, ge=1, le=256)
    """Turns this replica runs at once, and therefore how big its connection pool is.

    Security review finding S2: a turn holds up to three database connections, one of them for
    the turn's whole length, and nothing bounded how many turns ran at once or sized the pool
    against them - so ten concurrent customers exhausted SQLAlchemy's default pool and every
    later request, ``/healthz`` included, blocked for thirty seconds and then raised.

    Raising this raises the pool with it (:func:`~support_core.storage.config.pool_settings`), so
    the two cannot drift apart. A message that arrives over the bound is not refused and not made
    to wait: it is durable and ``pending`` before the bound is consulted, the caller is told
    ``queued``, and the drain worker runs it - the same answer, and the same machinery, as a
    message that arrived while another turn held its conversation's lock."""

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

    serve_desk: bool = False
    """Serve the human desk API of DESIGN.md section 13 under ``/desk``.

    **Off unless a deployment says otherwise, and unserveable without a credential** (phase W
    review finding W1). It was on by default, on the same listener as the customer chat and with
    no authentication, so any browser that could open the demo page could list every conversation
    in the deployment, read another customer's transcript and handoff packet, and write into it -
    including the human half of a ``requires_human_approval`` pair, which is a customer
    countersigning their own action.

    The argument for the old default was that a deployment which queues handoff packets and
    cannot read them tells customers a person will reply and has no person. That argument is
    sound and it is not an argument for serving them to everybody: the ``handoff`` table is the
    contract, and turning the desk on is one line plus a token.

    Turning it on without :attr:`desk_token` is a startup failure, not an open desk."""

    desk_token: str | None = None
    """The bearer token the desk API requires, at least :data:`MIN_DESK_TOKEN` characters.

    A secret, so a real deployment supplies it in ``SUPPORT_DESK_TOKEN`` (DESIGN.md section 20);
    the field exists so a test, or a developer's local file, can set one without exporting a
    variable. It is never echoed: nothing in a response, a log line or ``/healthz`` contains it.

    One shared token rather than per-operator accounts, deliberately. This is the smallest thing
    that makes the failure mode a refusal, which is what finding W1 asks for; real operator
    identity, rotation and an audit of who did what belong with phase 7's work on this surface,
    and the desk already takes a ``human_id`` on every action for the audit half."""

    qdrant_url: str | None = None
    """Where the vector side of the knowledge layer is (DESIGN.md section 9.1).

    ``None`` means ``SUPPORT_QDRANT_URL``, and that means the compose default. A URL is not a
    secret, so unlike the database password it may live in a config file."""

    use_qdrant: bool = True
    """Whether this deployment has a Qdrant at all.

    Off is a *configuration*: retrieval runs on the Postgres lexical and dense halves only, said
    once at startup. It is not the same as a Qdrant that is down, which is a *fault*: that
    degrades per call, is logged per call, and is what
    :class:`~support_core.knowledge.composite.CompositeRetriever` exists to survive. Keeping the
    two apart is what stops a restart during an outage turning into a deployment that quietly
    never uses the vector side again."""

    handoff_webhook_url: str | None = None
    """A second sink for handoff packets (DESIGN.md section 13's webhook sink).

    The Postgres queue is always written; this is added beside it when a deployment has somewhere
    else to page. A webhook that is down is recorded, not retried, and never fails a turn."""

    transcript_url_template: str = "/desk/conversations/{conversation_id}/transcript"
    """What a packet's ``transcript_url`` points at. Deployment configuration because the URL a
    person can open depends on where this service is reachable from, which core cannot know. The
    default is this service's own desk endpoint, so the link works rather than merely looking
    like one."""

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
        if source.get(ENV_MODEL):
            values["models"]["default"] = source[ENV_MODEL]
        if source.get(ENV_ESCALATION_MODEL):
            values["models"]["escalation"] = source[ENV_ESCALATION_MODEL]
        if source.get(ENV_DESK_TOKEN):
            values["desk_token"] = source[ENV_DESK_TOKEN]
        if source.get(ENV_QDRANT_URL):
            values["qdrant_url"] = source[ENV_QDRANT_URL]
        if source.get(ENV_MAX_CONCURRENT_TURNS):
            values["max_concurrent_turns"] = source[ENV_MAX_CONCURRENT_TURNS]
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

    def build_desk_credential(self) -> str:
        """The desk's bearer token, or a refusal to start (phase W review finding W1).

        Called by :func:`~support_core.api.app.create_app` *before* the router is mounted, so a
        deployment that turns the desk on and forgets the token gets an application that will not
        start rather than a desk anybody can read. That is the whole point of the finding: the
        failure mode of a missing credential has to be refusal, not access.
        """
        token = (self.desk_token or "").strip()
        if not token:
            msg = (
                "serve_desk is on but no desk_token is set: the desk lists every conversation "
                f"in this deployment and writes into any of them. Set {ENV_DESK_TOKEN} in the "
                "environment (DESIGN.md section 20), or turn serve_desk off"
            )
            raise ConfigError(msg)
        if len(token) < MIN_DESK_TOKEN:
            msg = (
                f"desk_token must be at least {MIN_DESK_TOKEN} characters; this one is {len(token)}"
            )
            raise ConfigError(msg)
        return token

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
        if source.get("GLM_API_KEY") or source.get("ZAI_API_KEY"):
            return "glm"
        return "replay" if self.cassette_dir is not None else "none"
