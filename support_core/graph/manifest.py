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

Channel = Literal["web_chat", "email"]
"""Customer channels core knows how to serve (section 12). The desk is not a customer channel."""


class ManifestError(ValueError):
    """``pack.yaml`` is missing, unreadable, or fails validation."""


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_model: str = Field(min_length=1)
    escalation_model: str | None = None
    """Used for handoff summaries and hard reasoning nodes; defaults to ``default_model``."""


class InterruptConfig(BaseModel):
    """Section 6.6: which graphs may be interrupted by a topic change, and which may not."""

    model_config = ConfigDict(extra="forbid")

    allowed_from: list[str] = Field(default_factory=list)
    blocked_in: list[str] = Field(default_factory=list)


class HandoffConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue: str = Field(min_length=1)
    sla_minutes: int | None = Field(default=None, ge=0)


class LimitsConfig(BaseModel):
    """Hard per-turn and per-conversation limits enforced by the engine (sections 5.1, 14)."""

    model_config = ConfigDict(extra="forbid")

    max_nodes_per_turn: int = Field(default=25, ge=1)
    max_tool_calls_per_turn: int = Field(default=10, ge=0)
    max_llm_cost_per_conversation_usd: float = Field(default=2.0, ge=0)


class PackManifest(BaseModel):
    """Parsed ``pack.yaml``. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    version: str
    core: str
    """PEP 440 specifier the installed ``support_core`` version must satisfy."""
    entry_graph: str = Field(min_length=1)
    language: str = "en"
    channels: list[Channel] = Field(min_length=1)
    llm: LlmConfig
    interrupts: InterruptConfig = Field(default_factory=InterruptConfig)
    handoff: HandoffConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

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
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            msg = f"core {value!r} is not a valid version specifier (example: '>=1.4,<2')"
            raise ValueError(msg) from exc
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
    except yaml.YAMLError as exc:
        msg = f"{manifest_path}: invalid YAML: {exc}"
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
