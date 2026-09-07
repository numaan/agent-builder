"""Pack manifest (``pack.yaml``) schema and loader. Implements DESIGN.md section 5.1.

The manifest is the one file every pack must have. It is validated with Pydantic so that a
typo in a key is a startup failure (section 5.2) rather than a silently ignored setting.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from support_core import __version__
from support_core.guardrails.outbound import CitationPolicy

Channel = Literal["web_chat", "email"]
"""Customer channels core knows how to serve (section 12). The desk is not a customer channel."""


class ManifestError(ValueError):
    """``pack.yaml`` is missing, unreadable, or fails validation."""


class ManifestUnreadableError(ManifestError):
    """``pack.yaml`` exists but cannot be read as UTF-8 text (or at all)."""


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_model: str = Field(min_length=1)
    escalation_model: str | None = None
    """Used for handoff summaries and hard reasoning nodes; defaults to ``default_model``.

    Also the last rung of DESIGN.md section 7.3's ladder: "retry with backoff, then fall back to
    ``escalation_model``, then handoff with reason ``llm_unavailable``."""

    confidence_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    """DESIGN.md section 11.3: "Low confidence on a decision below a pack threshold routes to
    ``unclear`` handling instead of a guess." Below this, the engine takes the node's ``unclear``
    edge if it declares one and hands off if it does not - it never takes the model's guess."""

    max_tool_iterations: int = Field(default=5, ge=0, le=20)
    """The bound on DESIGN.md section 8.4's model-facing tool loop ("default 5 iterations")."""

    retries: int = Field(default=2, ge=0, le=5)
    """Attempts per model before the escalation rung (DESIGN.md section 7.3)."""

    max_output_tokens: int = Field(default=4096, ge=256)
    prompt_budget: dict[str, int] = Field(default_factory=dict)
    """Per-layer token budgets (DESIGN.md section 10), keyed by the layer names of section 11.2.
    Anything unset keeps the core default."""

    @field_validator("prompt_budget")
    @classmethod
    def _known_layers(cls, value: dict[str, int]) -> dict[str, int]:
        from support_core.llm.prompt import PromptBudget

        known = set(PromptBudget.model_fields)
        unknown = sorted(set(value) - known)
        if unknown:
            msg = f"unknown prompt layer(s) {unknown}; known layers are {sorted(known)}"
            raise ValueError(msg)
        if any(budget < 1 for budget in value.values()):
            msg = "a layer budget must be at least one token"
            raise ValueError(msg)
        return value


class MemoryConfig(BaseModel):
    """The conversation memory of DESIGN.md section 10.

    Section 10 says the conversation summary is "updated every K turns" and that prompt assembly
    uses explicit token budgets, but section 5.1's example manifest has nowhere to put either.
    Added here for the same reason phase 2 added ``timeouts:``, and with defaults that do nothing
    surprising: summarising is off unless a pack asks for it, because it costs a model call per
    K turns and a pack that never reads ``ctx.summary`` should not pay for it.
    """

    model_config = ConfigDict(extra="forbid")

    summarize_every_turns: int = Field(default=0, ge=0)
    """``K``. Zero disables the rolling summary."""

    window_messages: int = Field(default=12, ge=1)
    """How many recent messages go in the prompt's turn window (section 10, "Turn window")."""

    max_summary_chars: int = Field(default=1200, ge=100)


class InterruptConfig(BaseModel):
    """Section 6.6: which graphs may be interrupted by a topic change, and which may not."""

    model_config = ConfigDict(extra="forbid")

    allowed_from: list[str] = Field(default_factory=list)
    blocked_in: list[str] = Field(default_factory=list)

    @field_validator("allowed_from", "blocked_in")
    @classmethod
    def _graph_ids_unique(cls, value: list[str]) -> list[str]:
        if any(not graph for graph in value):
            msg = "graph ids must not be empty"
            raise ValueError(msg)
        if len(set(value)) != len(value):
            msg = "contains duplicates"
            raise ValueError(msg)
        return value


class HandoffConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue: str = Field(min_length=1)
    sla_minutes: int | None = Field(default=None, ge=0)


class GuardrailsConfig(BaseModel):
    """DESIGN.md section 14's "pack-supplied configuration", which section 5.1 has nowhere to put.

    Same precedent and the same argument as phase 2's ``timeouts:``, phase 3's ``memory:`` and
    phase 6's ``limits.max_node_errors``: the design requires the setting and its example manifest
    has no key for it, so the key is added and the addition is written down.

    Only the *non-structural* guardrails are configurable here, which is section 14's own line -
    "packs cannot disable structural guardrails". Risk tiers, approval hashes, gates and the
    per-turn limits are enforced by the tool runtime and the engine and appear nowhere in this
    block, by construction rather than by a rule somebody has to remember.
    """

    model_config = ConfigDict(extra="forbid")

    citations: CitationPolicy = Field(default_factory=CitationPolicy)
    """The outbound citation check of sections 9.2 and 14. On by default: a guardrail a pack has
    to remember to enable is not a guardrail."""


class LimitsConfig(BaseModel):
    """Hard per-turn and per-conversation limits enforced by the engine (sections 5.1, 14)."""

    model_config = ConfigDict(extra="forbid")

    max_nodes_per_turn: int = Field(default=25, ge=1)
    max_tool_calls_per_turn: int = Field(default=10, ge=0)
    max_llm_cost_per_conversation_usd: float = Field(default=2.0, ge=0)
    max_node_errors: int = Field(default=3, ge=1)
    """Consecutive failures of one node in one frame before the conversation goes to a human.

    The bound DESIGN.md section 7.3's retry story needs and section 5.1 has nowhere to put. A
    node whose ``on_error`` edge leads back to itself - which is what a ``tool`` node retrying
    its own call looks like - otherwise repeats for as long as it keeps failing, and for a WRITE
    tool that needs no approval that is one side effect per failure (phase 4's resolution left
    this shape open and named phase 6 as its owner). Three, because a transient failure deserves
    more than one try and a permanent one deserves a person; counted in the frame, so it survives
    a crash, and reset whenever the node succeeds, so an ordinary loop is untouched."""


SuspendStatus = Literal["waiting_customer", "waiting_human", "waiting_async_tool", "waiting_timer"]
"""The four statuses a run can suspend into (DESIGN.md section 7.2)."""

TimeoutAction = Literal["none", "close", "handoff"]
"""What the engine does when a suspension times out.

``close`` closes the conversation, ``handoff`` calls the handoff hook and leaves the run
waiting for a human, ``none`` leaves it waiting. DESIGN.md section 7.2: "A ``waiting_customer``
timeout in web chat closes the conversation; in email it does nothing for days."
"""


class TimeoutRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int | None = Field(default=None, ge=1)
    """``None`` means the run waits indefinitely."""

    action: TimeoutAction = "none"


class ChannelTimeouts(BaseModel):
    """Per-channel overrides; an unset status falls back to the pack-level rule."""

    model_config = ConfigDict(extra="forbid")

    waiting_customer: TimeoutRule | None = None
    waiting_human: TimeoutRule | None = None
    waiting_async_tool: TimeoutRule | None = None
    waiting_timer: TimeoutRule | None = None


class TimeoutsConfig(BaseModel):
    """Per-status suspension timeouts (DESIGN.md section 7.2).

    Section 5.1's example manifest has no block for these and section 7.2 requires them to be
    "per status and configurable per pack", so this is an addition to the manifest schema
    (recorded in reviews/phase-2.md). The defaults never time anything out except a long-running
    async tool: closing a customer's conversation is a product decision a pack must make
    explicitly, not something a default should do behind the author's back.
    """

    model_config = ConfigDict(extra="forbid")

    waiting_customer: TimeoutRule = Field(default_factory=TimeoutRule)
    waiting_human: TimeoutRule = Field(default_factory=TimeoutRule)
    waiting_async_tool: TimeoutRule = Field(
        default_factory=lambda: TimeoutRule(seconds=900, action="handoff")
    )
    waiting_timer: TimeoutRule = Field(default_factory=TimeoutRule)
    channels: dict[Channel, ChannelTimeouts] = Field(default_factory=dict)

    def rule(self, status: SuspendStatus, channel: Channel | None = None) -> TimeoutRule:
        """The rule for ``status`` on ``channel``: the channel override, else the pack rule."""
        override = self.channels.get(channel) if channel is not None else None
        if override is not None:
            specific = getattr(override, status)
            if specific is not None:
                assert isinstance(specific, TimeoutRule)
                return specific
        rule = getattr(self, status)
        assert isinstance(rule, TimeoutRule)
        return rule


class PackManifest(BaseModel):
    """Parsed ``pack.yaml``. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    version: str
    core: str
    """PEP 440 specifier the installed ``support_core`` version must satisfy."""
    entry_graph: str = Field(min_length=1)
    language: str = Field(default="en", min_length=2)
    """BCP 47 tag such as ``en`` or ``pt-BR``; must not be blank."""
    channels: list[Channel] = Field(min_length=1)
    llm: LlmConfig
    interrupts: InterruptConfig = Field(default_factory=InterruptConfig)
    handoff: HandoffConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)

    @field_validator("version")
    @classmethod
    def _version_is_pep440(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            msg = f"version {value!r} is not a valid version string"
            raise ValueError(msg) from exc
        return value

    @field_validator("core")
    @classmethod
    def _core_is_specifier(cls, value: str) -> str:
        try:
            specifiers = SpecifierSet(value)
        except InvalidSpecifier as exc:
            msg = f"core {value!r} is not a valid version specifier (example: '>=1.4,<2')"
            raise ValueError(msg) from exc
        if not specifiers:
            # SpecifierSet('') is valid and matches every version, which would let a pack
            # opt out of the compatibility check the manifest exists to enforce.
            msg = "core must name at least one version constraint (example: '>=1.4,<2')"
            raise ValueError(msg)
        return value

    @field_validator("channels")
    @classmethod
    def _channels_unique(cls, value: list[Channel]) -> list[Channel]:
        if len(set(value)) != len(value):
            msg = "channels contains duplicates"
            raise ValueError(msg)
        return value

    def core_compatible(self, core_version: str = __version__) -> bool:
        """True if ``core_version`` satisfies the manifest's ``core`` specifier.

        Pre-releases are allowed so a pack can pin a development build of core.
        """
        return SpecifierSet(self.core).contains(Version(core_version), prereleases=True)


def load_manifest(path: Path) -> PackManifest:
    """Read and validate ``pack.yaml`` at ``path`` (a file or a pack directory)."""
    manifest_path = path / "pack.yaml" if path.is_dir() else path
    try:
        raw: Any = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"{manifest_path}: not found"
        raise ManifestError(msg) from exc
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"{manifest_path}: cannot be read as UTF-8 text: {exc}"
        raise ManifestUnreadableError(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"{manifest_path}: invalid YAML: {exc}"
        raise ManifestError(msg) from exc
    except RecursionError as exc:
        # PyYAML recurses per nesting level; a pathological file must be a validation failure,
        # not an exception escaping the loader.
        msg = f"{manifest_path}: YAML is nested too deeply to parse"
        raise ManifestError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"{manifest_path}: top level must be a mapping"
        raise ManifestError(msg)
    try:
        return PackManifest.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        msg = f"{manifest_path}: {problems}"
        raise ManifestError(msg) from exc
