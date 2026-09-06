"""``load_pack``. Implements DESIGN.md section 5.2.

"``load_pack(path)`` parses everything, resolves references, and runs a validator before the
service accepts traffic. Validation failures are startup failures."

So :func:`load_pack` returns a :class:`~support_core.graph.pack.Pack` or raises
:class:`PackValidationError`, which carries the whole report rather than only the first problem.
:func:`load_pack_report` is the non-raising form the CLI and the tests use.
"""

from pathlib import Path

from support_core import __version__
from support_core.graph.findings import Severity, ValidationReport
from support_core.graph.pack import Pack, build_pin
from support_core.graph.schema import read_graphs
from support_core.graph.tools_source import resolve_tools
from support_core.graph.validator import validate_pack


class PackValidationError(ValueError):
    """The pack did not validate. ``report`` holds every finding, not only the first."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        errors = "\n".join(f"  {finding.render()}" for finding in report.errors)
        super().__init__(f"{report.summary()}\n{errors}")


def load_pack(path: Path | str) -> Pack:
    """Load and validate the pack at ``path``. Raises :class:`PackValidationError` on any error."""
    pack, report = load_pack_report(path)
    if pack is None:
        raise PackValidationError(report)
    return pack


def load_pack_report(path: Path | str) -> tuple[Pack | None, ValidationReport]:
    """Load the pack and return ``(pack or None, report)``.

    The graphs are parsed twice: once inside :func:`~support_core.graph.validator.validate_pack`
    to produce the report, and once here to build the object. Parsing a pack is a handful of
    small YAML files at startup, and the alternative - threading parsed graphs out through
    ``ValidationReport``, which is a serialisable Pydantic model - would put non-serialisable
    objects into the CLI's report type.

    They are *read* only once, though: ``validate_pack`` records the text of every graph file
    on ``report.graph_sources`` and the second parse works from that snapshot. A pack edited on
    disk between the two parses can therefore no longer produce a ``PackPin`` whose hashes
    describe a mix of two versions (phase-1 deferred finding P1).
    """
    pack_path = Path(path)
    report = validate_pack(pack_path)
    if not report.ok or report.manifest is None:
        return None, report

    graphs, findings = read_graphs(pack_path, report.graph_files, sources=report.graph_sources)
    if any(finding.severity is Severity.ERROR for finding in findings):  # pragma: no cover
        # validate_pack already reported these; reaching here would mean the two disagree.
        report.findings.extend(findings)
        return None, report

    # Cheap the second time: the pack's ``tools`` package is already in ``sys.modules`` and
    # ``validate_pack`` has already reported anything wrong with it, so this only rebuilds the
    # registry object from the tools it imported.
    resolved = resolve_tools(pack_path)
    if any(finding.severity is Severity.ERROR for finding in resolved.findings):
        # pragma: no cover - validate_pack reported these and returned above
        report.findings.extend(resolved.findings)
        return None, report

    pack = Pack(
        path=pack_path,
        manifest=report.manifest,
        persona=_read_text(pack_path / "persona.md"),
        policies=_read_text(pack_path / "policies.md"),
        graphs=graphs,
        tools=resolved.manifest,
        pin=build_pin(report.manifest, graphs, __version__),
        registry=resolved.registry,
    )
    return pack, report


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # pragma: no cover - validate_pack reports it first
        return ""
