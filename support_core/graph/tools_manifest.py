"""Declarative tool manifest for validation. Implements DESIGN.md sections 8.1 to 8.3 as far as
phase 1 needs them, and unblocks the confirm-on-all-paths rule of section 5.2.

DESIGN.md section 8.3 says packs export ``TOOLS: list[Tool]`` from Python, and phase 4 builds
the registry by importing that module. The validator cannot wait for phase 4: the most valuable
rule in the system ("every write or high-risk tool node has a ``confirm`` node on all paths
between the last customer input and the call") is meaningless without a risk tier per tool.

So a pack also declares its tools as data, in ``tools/tools.yaml``::

    tools:
      - name: issue_refund
        description: Refund a charge to the original payment method.
        risk: high
        input: { charge_id: str, amount: float }
        output: { refund_id: str, status: str }
        idempotent: true
        requires_human_approval: false
        confirm_exempt: false
        async: false
        timeout_s: 15

**Phase 4 replaces this file as the source of truth.** When the real registry exists, the
manifest becomes a cross-check: the validator should compare the declared tiers and models
against the imported ``TOOLS`` and report any drift, or the manifest should be generated from
them. Until then, a missing ``tools/tools.yaml`` means the pack declares no tools, and every
``tool`` node in it is an unknown-tool error - which is the safe direction.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from support_core.graph.types import BuiltModel, build_model
from support_core.tools.base import Tool
from support_core.tools.registry import ToolRegistry, field_types
from support_core.tools.risk import Risk, needs_confirm

MANIFEST_NAME = "tools/tools.yaml"


class ToolManifestError(ValueError):
    """``tools/tools.yaml`` exists but is unreadable or invalid."""


class ToolDeclaration(BaseModel):
    """One tool, as a pack declares it for validation purposes (DESIGN.md section 8.1)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    risk: Risk
    input: dict[str, str] = Field(default_factory=dict)
    output: dict[str, str] = Field(default_factory=dict)
    idempotent: bool = True
    requires_human_approval: bool = False
    confirm_exempt: bool = False
    """DESIGN.md section 8.2: a WRITE tool the customer cannot reasonably be asked about, such
    as sending a one-time passcode. Every exemption is listed in the validator report."""

    confirm_exempt_reason: str | None = None
    """Why the exemption is defensible. Required whenever ``confirm_exempt`` is set (phase-1
    deferred finding J): DESIGN.md section 8.2 asks for exemptions to be "reviewed deliberately",
    and a flag nobody has to justify is not reviewed, it is noticed."""

    async_: bool = Field(default=False, alias="async")
    timeout_s: float = Field(default=15.0, gt=0)

    @model_validator(mode="after")
    def _exemption_is_justified(self) -> "ToolDeclaration":
        if self.confirm_exempt and not (self.confirm_exempt_reason or "").strip():
            msg = f"tool {self.name!r} is confirm_exempt and must give a confirm_exempt_reason"
            raise ValueError(msg)
        if self.confirm_exempt_reason and not self.confirm_exempt:
            msg = f"tool {self.name!r} gives a confirm_exempt_reason but is not confirm_exempt"
            raise ValueError(msg)
        return self


class ToolSpec(BaseModel):
    """A declaration plus the Pydantic models built from its ``input`` and ``output``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    declaration: ToolDeclaration
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    issues: tuple[str, ...] = ()
    """Problems with the declared field types, reported by the validator."""

    @property
    def name(self) -> str:
        return self.declaration.name

    @property
    def risk(self) -> Risk:
        return self.declaration.risk

    @property
    def needs_confirm(self) -> bool:
        """WRITE and HIGH need a confirm; only WRITE may be exempted.

        DESIGN.md section 8.2 offers ``confirm_exempt`` for "a WRITE tool ... such as sending a
        one-time passcode". It is not offered for HIGH, which is "money, access, irreversible",
        so a HIGH tool still needs a confirm however it is declared. The validator reports the
        declaration itself as ``tools.high_risk_exempt``.

        The decision itself lives in :func:`support_core.tools.risk.needs_confirm`, which the
        run-time :class:`~support_core.tools.base.Tool` also calls, so a tool cannot be exempt to
        the validator and not to the runtime.
        """
        return needs_confirm(self.risk, confirm_exempt=self.declaration.confirm_exempt)


class ToolManifest(BaseModel):
    """Every tool a pack declares, indexed by name."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    present: bool = False
    """False when the pack declares no tools at all."""

    from_registry: bool = False
    """True when these came from the pack's imported ``TOOLS`` rather than from YAML. Only then
    are the risk tiers the ones the runtime will actually enforce."""

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    @property
    def exemptions(self) -> list[ToolSpec]:
        return [
            spec
            for spec in self.tools.values()
            if spec.declaration.confirm_exempt and spec.risk is not Risk.READ
        ]


def manifest_from_registry(registry: ToolRegistry) -> ToolManifest:
    """The validator's view of an *imported* registry (DESIGN.md section 8.3).

    The rules type-check argument expressions against ``input_model`` and read ``risk``; taking
    both from the imported :class:`~support_core.tools.base.Tool` is what makes the registry
    authoritative (phase-1 deferred finding I). The declaration beside them is only for the
    report and for the drift comparison against ``tools/tools.yaml``.
    """
    specs = {tool.name: spec_from_tool(tool) for tool in registry}
    return ToolManifest(tools=specs, present=True, from_registry=True)


def spec_from_tool(tool: Tool) -> ToolSpec:
    declaration = ToolDeclaration(
        name=tool.name,
        description=tool.description,
        risk=tool.risk,
        input=field_types(tool.input_model),
        output=field_types(tool.output_model),
        idempotent=tool.idempotent,
        requires_human_approval=tool.requires_human_approval,
        confirm_exempt=tool.confirm_exempt,
        confirm_exempt_reason=tool.confirm_exempt_reason,
        timeout_s=tool.timeout_s,
        **{"async": tool.async_},
    )
    return ToolSpec(
        declaration=declaration, input_model=tool.input_model, output_model=tool.output_model
    )


def load_tool_manifest(pack_path: Path) -> ToolManifest:
    """Read ``tools/tools.yaml``. A missing file yields an empty, ``present=False`` manifest."""
    path = pack_path / "tools" / "tools.yaml"
    if not path.is_file():
        return ToolManifest()
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"{MANIFEST_NAME}: cannot be read as UTF-8 text: {exc}"
        raise ToolManifestError(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"{MANIFEST_NAME}: invalid YAML: {exc}"
        raise ToolManifestError(msg) from exc
    except RecursionError as exc:
        msg = f"{MANIFEST_NAME}: YAML is nested too deeply to parse"
        raise ToolManifestError(msg) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        msg = f"{MANIFEST_NAME}: top level must be a mapping with a 'tools' list"
        raise ToolManifestError(msg)
    entries = raw.get("tools") or []
    unknown = set(raw) - {"tools"}
    if unknown:
        msg = f"{MANIFEST_NAME}: unknown key(s) {', '.join(sorted(unknown))}"
        raise ToolManifestError(msg)
    if not isinstance(entries, list):
        msg = f"{MANIFEST_NAME}: 'tools' must be a list"
        raise ToolManifestError(msg)

    specs: dict[str, ToolSpec] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            msg = f"{MANIFEST_NAME}: tools[{index}] must be a mapping"
            raise ToolManifestError(msg)
        try:
            declaration = ToolDeclaration.model_validate(entry)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            msg = f"{MANIFEST_NAME}: tools[{index}]: {problems}"
            raise ToolManifestError(msg) from exc
        if declaration.name in specs:
            msg = f"{MANIFEST_NAME}: duplicate tool name {declaration.name!r}"
            raise ToolManifestError(msg)
        specs[declaration.name] = _build_spec(declaration)
    return ToolManifest(tools=specs, present=True)


def _build_spec(declaration: ToolDeclaration) -> ToolSpec:
    prefix = "".join(part.title() for part in declaration.name.split("_"))
    built_input: BuiltModel = build_model(f"{prefix}Input", declaration.input)
    built_output: BuiltModel = build_model(f"{prefix}Output", declaration.output)
    issues = tuple(
        f"{declaration.name}.{kind}.{issue.field}: {issue.message}"
        for kind, built in (("input", built_input), ("output", built_output))
        for issue in built.issues
    )
    return ToolSpec(
        declaration=declaration,
        input_model=built_input.model,
        output_model=built_output.model,
        issues=issues,
    )
