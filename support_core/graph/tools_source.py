"""Where a pack's tools come from, and what the validator does when the two sources disagree.

DESIGN.md section 8.3 has one source: "Packs export ``TOOLS: list[Tool]``". Phase 1 could not
import pack code, so it added a declarative ``tools/tools.yaml`` and warned that the imported
registry must replace it (phase-1 deferred finding I: "declaring ``issue_refund`` as ``read``
removes every check today"). This module is that replacement.

The rule, in one paragraph. **If the pack exports tools, the registry is what everything reads**
- the validator's risk tiers, its argument type-checking, and the runtime's policy - and
``tools/tools.yaml``, if it exists at all, is only a claim that gets compared against the truth:
a different risk tier is an error, because one of the two is a lie and nobody can tell which;
a different shape is a warning, because a stale declaration misleads a reader without
endangering anybody. **If the pack exports none**, the YAML is used as phase 1 used it, with a
warning that says the obvious thing: a pack whose graphs call tools it does not export cannot
run them, so nothing here has been checked against anything that will ever execute.

Importing a pack runs its Python. That is what DESIGN.md 5.2's "validation failures are startup
failures" implies for tools, and it is why an import that raises becomes a finding rather than
an exception out of ``validate_pack``.
"""

from dataclasses import dataclass
from pathlib import Path

from support_core.graph.findings import Finding, Severity
from support_core.graph.tools_manifest import (
    ToolManifest,
    ToolManifestError,
    ToolSpec,
    load_tool_manifest,
    manifest_from_registry,
)
from support_core.tools.loading import PackToolsImportError, registry_for_pack
from support_core.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ResolvedTools:
    """What the pack's tools are, and what was wrong with saying so."""

    manifest: ToolManifest
    registry: ToolRegistry
    findings: tuple[Finding, ...] = ()


def resolve_tools(pack_path: Path) -> ResolvedTools:
    """Import the pack's tools, read its declarations, and reconcile the two."""
    findings: list[Finding] = []
    registry = ToolRegistry()
    try:
        registry = registry_for_pack(pack_path)
    except PackToolsImportError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                rule="tools.import_failed",
                message=str(exc),
                location="tools/__init__.py",
            )
        )
    declared = ToolManifest()
    try:
        declared = load_tool_manifest(pack_path)
    except ToolManifestError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                rule="tools.manifest_invalid",
                message=str(exc),
                location="tools/tools.yaml",
            )
        )

    if not len(registry):
        if declared.present:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    rule="tools.declared_not_exported",
                    message=(
                        "tools/tools.yaml declares "
                        f"{', '.join(sorted(declared.tools)) or 'nothing'} but tools/__init__.py "
                        "exports no TOOLS, so the declared risk tiers are checked against "
                        "nothing that can run and every tool node in this pack would fail at "
                        "run time (DESIGN.md section 8.3)"
                    ),
                    location="tools/tools.yaml",
                )
            )
        return ResolvedTools(manifest=declared, registry=registry, findings=tuple(findings))

    manifest = manifest_from_registry(registry)
    if declared.present:
        findings.extend(_drift(declared, manifest))
    return ResolvedTools(manifest=manifest, registry=registry, findings=tuple(findings))


def _drift(declared: ToolManifest, exported: ToolManifest) -> list[Finding]:
    """Compare what the pack says with what it exports (phase-1 deferred finding I)."""
    findings: list[Finding] = []
    for name in sorted(declared.tools):
        claim = declared.tools[name]
        truth = exported.get(name)
        if truth is None:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="tools.registry_drift",
                    message=(
                        f"tools/tools.yaml declares {name!r} but tools/__init__.py does not "
                        f"export it; a graph that calls it would type-check and then fail at "
                        f"run time"
                    ),
                    location="tools/tools.yaml",
                )
            )
            continue
        if claim.risk is not truth.risk:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="tools.registry_drift",
                    message=(
                        f"tools/tools.yaml declares {name!r} as {claim.risk.value} risk but the "
                        f"exported tool is {truth.risk.value}. The exported tool is what the "
                        f"runtime enforces; a declaration that disagrees is either a stale file "
                        f"or an attempt to validate a graph against a tier nothing will apply"
                    ),
                    location="tools/tools.yaml",
                )
            )
        stale = _stale_fields(claim, truth)
        if stale:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    rule="tools.declaration_stale",
                    message=(
                        f"tools/tools.yaml describes {name!r} differently from the exported "
                        f"tool ({'; '.join(stale)}). The exported tool is what is used"
                    ),
                    location="tools/tools.yaml",
                )
            )
    for name in sorted(set(exported.tools) - set(declared.tools)):
        findings.append(
            Finding(
                severity=Severity.WARNING,
                rule="tools.declaration_stale",
                message=(
                    f"tools/__init__.py exports {name!r}, which tools/tools.yaml does not "
                    f"declare; the file is a reader's summary of the registry and is now "
                    f"incomplete"
                ),
                location="tools/tools.yaml",
            )
        )
    return findings


def _stale_fields(claim: ToolSpec, truth: ToolSpec) -> list[str]:
    left = claim.declaration
    right = truth.declaration
    differences: list[str] = []
    if set(left.input) != set(right.input):
        differences.append(
            f"inputs {sorted(left.input)} declared against {sorted(right.input)} exported"
        )
    if set(left.output) != set(right.output):
        differences.append(
            f"outputs {sorted(left.output)} declared against {sorted(right.output)} exported"
        )
    for flag in ("idempotent", "requires_human_approval", "confirm_exempt", "async_"):
        if getattr(left, flag) != getattr(right, flag):
            differences.append(
                f"{flag.rstrip('_')} declared {getattr(left, flag)!r}, exported "
                f"{getattr(right, flag)!r}"
            )
    return differences
