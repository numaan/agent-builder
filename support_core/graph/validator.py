"""Pack validation. Implements DESIGN.md section 5.2 (the layout half; graph rules are phase 1).

``validate_pack`` never raises for a bad pack: it returns a :class:`ValidationReport` whose
findings the CLI prints and whose ``ok`` flag decides the exit code. ``load_pack`` (phase 1)
will call the same function and refuse to start on any error finding.

Phase 1 hook: :func:`validate_graphs`. It receives the manifest and the graph files and must
implement every rule listed in section 5.2. Until then it only reports that graphs were seen.
"""

from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from support_core import __version__
from support_core.graph.manifest import ManifestError, PackManifest, load_manifest

REQUIRED_FILES: tuple[str, ...] = (
    "pack.yaml",
    "persona.md",
    "policies.md",
    "tools/__init__.py",
    "knowledge/sources.yaml",
)
REQUIRED_DIRS: tuple[str, ...] = (
    "graphs",
    "tools",
    "knowledge",
    "evals",
    "evals/golden",
    "evals/nodes",
)
OPTIONAL_DIRS: tuple[str, ...] = ("nodes", "knowledge/docs", "knowledge/kg")
GRAPH_SUFFIXES: tuple[str, ...] = (".yaml", ".yml")
KNOWLEDGE_SECTIONS: tuple[str, ...] = ("documents", "knowledge_graph", "live_lookups")
POLICIES_MAX_LINES = 40
"""Section 5: policies.md is 'short hard rules injected into every prompt (max ~40 lines)'."""


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Finding(BaseModel):
    severity: Severity
    rule: str
    """Stable dotted identifier, for example ``layout.missing_file``; tests assert on these."""
    message: str
    location: str | None = None
    """File (and later node) the finding points at, relative to the pack directory."""

    def render(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.severity.value.upper():7} {self.rule}{where}: {self.message}"


class ValidationReport(BaseModel):
    pack_path: Path
    manifest: PackManifest | None = None
    graph_files: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def empty(self) -> bool:
        """A pack with no graphs has nothing to run yet, but may still be well-formed."""
        return not self.graph_files

    def summary(self) -> str:
        name = self.manifest.id if self.manifest else self.pack_path.name
        if not self.ok:
            n = len(self.errors)
            return f"{name}: {n} error{'s' if n != 1 else ''}; pack is not valid"
        shape = "empty but well-formed" if self.empty else "well-formed"
        extra = f" ({len(self.warnings)} warning(s))" if self.warnings else ""
        return f"{name}: {shape}{extra}"


def validate_pack(pack_path: Path) -> ValidationReport:
    """Check the pack directory layout, the manifest, and (phase 1) the graphs."""
    pack_path = Path(pack_path)
    report = ValidationReport(pack_path=pack_path)
    findings = report.findings

    if not pack_path.is_dir():
        findings.append(
            Finding(
                severity=Severity.ERROR,
                rule="layout.not_a_directory",
                message=f"{pack_path} is not a directory",
            )
        )
        return report

    findings.extend(_check_layout(pack_path))

    try:
        report.manifest = load_manifest(pack_path)
    except ManifestError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                rule="manifest.invalid",
                message=str(exc),
                location="pack.yaml",
            )
        )

    if report.manifest is not None:
        findings.extend(_check_manifest(report.manifest))

    graphs_dir = pack_path / "graphs"
    if graphs_dir.is_dir():
        report.graph_files = sorted(
            p.relative_to(pack_path).as_posix()
            for p in graphs_dir.iterdir()
            if p.is_file() and p.suffix in GRAPH_SUFFIXES
        )

    if report.empty:
        findings.append(
            Finding(
                severity=Severity.INFO,
                rule="pack.empty",
                message="graphs/ contains no graph files; the pack has nothing to run yet",
                location="graphs/",
            )
        )
    elif report.manifest is not None:
        expected = {f"graphs/{report.manifest.entry_graph}{s}" for s in GRAPH_SUFFIXES}
        if expected.isdisjoint(report.graph_files):
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="graph.entry_missing",
                    message=f"entry_graph {report.manifest.entry_graph!r} has no file in graphs/",
                    location="pack.yaml",
                )
            )

    findings.extend(_check_tools_module(pack_path))
    findings.extend(_check_knowledge_sources(pack_path))
    findings.extend(_check_policies(pack_path))
    findings.extend(validate_graphs(pack_path, report.manifest, report.graph_files))
    return report


def validate_graphs(
    pack_path: Path, manifest: PackManifest | None, graph_files: Iterable[str]
) -> list[Finding]:
    """Phase 1 hook for the graph rules in DESIGN.md section 5.2.

    Phase 1 replaces this body with the real loader and validator (edge targets exist, one
    ``start`` and at least one ``end``, tool references, gate redirects, confirm-on-all-paths,
    sub-graph mapping types, no un-suspended cycles). Phase 0 only makes graphs visible in the
    report so nobody mistakes "no findings" for "validated".
    """
    files = list(graph_files)
    if not files:
        return []
    return [
        Finding(
            severity=Severity.WARNING,
            rule="graph.not_validated",
            message=f"{len(files)} graph file(s) found but graph validation is not implemented yet "
            "(phase 1)",
            location=", ".join(files),
        )
    ]


def _check_layout(pack_path: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel in REQUIRED_DIRS:
        if not (pack_path / rel).is_dir():
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="layout.missing_dir",
                    message=f"required directory {rel}/ is missing",
                    location=rel,
                )
            )
    for rel in REQUIRED_FILES:
        if not (pack_path / rel).is_file():
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="layout.missing_file",
                    message=f"required file {rel} is missing",
                    location=rel,
                )
            )
    for rel in OPTIONAL_DIRS:
        path = pack_path / rel
        if path.exists() and not path.is_dir():
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="layout.not_a_dir",
                    message=f"{rel} exists but is not a directory",
                    location=rel,
                )
            )
    return findings


def _check_manifest(manifest: PackManifest) -> list[Finding]:
    findings: list[Finding] = []
    if not manifest.core_compatible():
        findings.append(
            Finding(
                severity=Severity.ERROR,
                rule="manifest.core_incompatible",
                message=(
                    f"pack requires support-core {manifest.core!r} but {__version__} is installed"
                ),
                location="pack.yaml",
            )
        )
    for graph in manifest.interrupts.allowed_from:
        if graph in manifest.interrupts.blocked_in:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="manifest.interrupts_conflict",
                    message=f"graph {graph!r} is in both interrupts.allowed_from and blocked_in",
                    location="pack.yaml",
                )
            )
    return findings


def _check_tools_module(pack_path: Path) -> list[Finding]:
    """Static check only: importing pack code belongs to the tool registry (phase 4)."""
    init = pack_path / "tools" / "__init__.py"
    if not init.is_file():
        return []
    source = init.read_text(encoding="utf-8")
    if not any(line.startswith("TOOLS") for line in source.splitlines()):
        return [
            Finding(
                severity=Severity.ERROR,
                rule="tools.no_export",
                message="tools/__init__.py must define TOOLS (DESIGN.md section 8.3)",
                location="tools/__init__.py",
            )
        ]
    return []


def _check_knowledge_sources(pack_path: Path) -> list[Finding]:
    sources_path = pack_path / "knowledge" / "sources.yaml"
    if not sources_path.is_file():
        return []
    location = "knowledge/sources.yaml"
    try:
        raw: Any = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [
            Finding(
                severity=Severity.ERROR,
                rule="knowledge.sources_invalid",
                message=f"invalid YAML: {exc}",
                location=location,
            )
        ]
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return [
            Finding(
                severity=Severity.ERROR,
                rule="knowledge.sources_invalid",
                message="top level must be a mapping",
                location=location,
            )
        ]
    findings: list[Finding] = []
    for key, value in raw.items():
        if key not in KNOWLEDGE_SECTIONS:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="knowledge.sources_invalid",
                    message=f"unknown section {key!r}; expected one of {list(KNOWLEDGE_SECTIONS)}",
                    location=location,
                )
            )
        elif value is not None and not isinstance(value, list):
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    rule="knowledge.sources_invalid",
                    message=f"section {key!r} must be a list",
                    location=location,
                )
            )
    return findings


def _check_policies(pack_path: Path) -> list[Finding]:
    policies = pack_path / "policies.md"
    if not policies.is_file():
        return []
    lines = [ln for ln in policies.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) > POLICIES_MAX_LINES:
        return [
            Finding(
                severity=Severity.WARNING,
                rule="policies.too_long",
                message=(
                    f"policies.md has {len(lines)} non-empty lines; DESIGN.md section 5 asks for "
                    f"at most about {POLICIES_MAX_LINES} because it is injected into every prompt"
                ),
                location="policies.md",
            )
        ]
    return []
