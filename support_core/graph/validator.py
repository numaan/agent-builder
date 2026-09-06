"""Pack validation. Implements DESIGN.md section 5.2.

``validate_pack`` never raises for a bad pack: it returns a :class:`ValidationReport` whose
findings the CLI prints and whose ``ok`` flag decides the exit code.
:func:`support_core.graph.loader.load_pack` calls the same function and refuses to start on any
error finding.

Phase 0 implemented the layout and manifest half. Phase 1 filled in :func:`validate_graphs`,
which parses every graph file (:mod:`support_core.graph.schema`) and runs every graph rule in
section 5.2 (:mod:`support_core.graph.rules`). :class:`Severity`, :class:`Finding` and
:class:`ValidationReport` moved to :mod:`support_core.graph.findings` to break an import cycle
and are re-exported here, which is where the CLI and the tests import them from.
"""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from support_core import __version__
from support_core.graph.findings import Finding, Severity, ValidationReport
from support_core.graph.manifest import (
    ManifestError,
    ManifestUnreadableError,
    PackManifest,
    load_manifest,
)
from support_core.graph.rules import validate_graph_set
from support_core.graph.schema import read_graphs
from support_core.graph.tools_source import resolve_tools

__all__ = [
    "Finding",
    "Severity",
    "ValidationReport",
    "validate_graphs",
    "validate_pack",
]

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
TOOLS_EXPORT = re.compile(r"^TOOLS\s*(?::|=)")
"""A module-level ``TOOLS = ...`` or ``TOOLS: list[...] = ...`` line (section 8.3)."""


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
    except ManifestUnreadableError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                rule="manifest.unreadable",
                message=str(exc),
                location="pack.yaml",
            )
        )
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
        for entry in sorted(graphs_dir.iterdir()):
            if entry.suffix not in GRAPH_SUFFIXES:
                continue
            rel = entry.relative_to(pack_path).as_posix()
            if entry.is_file():
                report.graph_files.append(rel)
            else:
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        rule="layout.not_a_file",
                        message=f"{rel} has a graph file suffix but is not a file",
                        location=rel,
                    )
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
    findings.extend(
        validate_graphs(
            pack_path, report.manifest, report.graph_files, sources=report.graph_sources
        )
    )
    return report


def validate_graphs(
    pack_path: Path,
    manifest: PackManifest | None,
    graph_files: Iterable[str],
    *,
    sources: dict[str, str] | None = None,
) -> list[Finding]:
    """Every graph rule in DESIGN.md section 5.2.

    Parses each graph file into a :class:`~support_core.graph.schema.Graph` and hands the whole
    set, plus the pack's tools, to :func:`support_core.graph.rules.validate_graph_set`. From
    phase 4 those tools are the pack's *imported* ``TOOLS`` wherever it exports any
    (:func:`~support_core.graph.tools_source.resolve_tools`), so the risk tiers the confirm rule
    reasons about are the ones the runtime will enforce.
    Parsing and rule-checking are separate so one broken file does not hide the problems in the
    others: a file that cannot be parsed contributes its own finding and the remaining graphs are
    still validated. ``sources`` collects the text of every file read, so the loader can parse
    exactly the bytes the validator saw (phase-1 deferred finding P1).
    """
    files = list(graph_files)
    resolved = resolve_tools(pack_path)
    findings: list[Finding] = list(resolved.findings)
    tools = resolved.manifest
    if not files:
        return findings
    graphs, parse_findings = read_graphs(pack_path, files, sources=sources)
    findings.extend(parse_findings)
    findings.extend(validate_graph_set(graphs, tools, manifest))
    return findings


def _read_utf8(pack_path: Path, rel: str, rule: str) -> tuple[str | None, list[Finding]]:
    """Read a pack file as UTF-8; an unreadable file becomes an error finding, not a crash."""
    try:
        return (pack_path / rel).read_text(encoding="utf-8"), []
    except (OSError, UnicodeDecodeError) as exc:
        finding = Finding(
            severity=Severity.ERROR,
            rule=rule,
            message=f"cannot be read as UTF-8 text: {exc}",
            location=rel,
        )
        return None, [finding]


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
    if not (pack_path / "tools" / "__init__.py").is_file():
        return []
    source, findings = _read_utf8(pack_path, "tools/__init__.py", "tools.unreadable")
    if source is None:
        return findings
    if not any(TOOLS_EXPORT.match(line) for line in source.splitlines()):
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
    source, findings = _read_utf8(pack_path, location, "knowledge.sources_unreadable")
    if source is None:
        return findings
    try:
        raw: Any = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        return [
            Finding(
                severity=Severity.ERROR,
                rule="knowledge.sources_invalid",
                message=f"invalid YAML: {exc}",
                location=location,
            )
        ]
    except RecursionError:
        return [
            Finding(
                severity=Severity.ERROR,
                rule="knowledge.sources_invalid",
                message="YAML is nested too deeply to parse",
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
    if not (pack_path / "policies.md").is_file():
        return []
    source, findings = _read_utf8(pack_path, "policies.md", "policies.unreadable")
    if source is None:
        return findings
    lines = [ln for ln in source.splitlines() if ln.strip()]
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
