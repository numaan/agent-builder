"""Validation findings. Implements DESIGN.md section 5.2 (the report a pack load produces).

Split out of :mod:`support_core.graph.validator` in phase 1 so the loader, the graph rules and
the validator can all produce findings without importing each other in a cycle. The names are
re-exported from :mod:`support_core.graph.validator`, which is where phase 0 put them and where
the CLI and the existing tests still import them from.

Rule ids are stable dotted identifiers (``layout.missing_file``, ``graph.unconfirmed_write``).
Tests assert on them, so renaming one is a breaking change.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from support_core.graph.manifest import PackManifest


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
    """File the finding points at, relative to the pack directory."""

    node: str | None = None
    """Node id inside that file, when the finding is about one node (phase 1)."""

    def render(self) -> str:
        where = self.location or ""
        if self.node:
            where = f"{where}:{self.node}" if where else self.node
        suffix = f" [{where}]" if where else ""
        return f"{self.severity.value.upper():7} {self.rule}{suffix}: {self.message}"


class ValidationReport(BaseModel):
    pack_path: Path
    manifest: PackManifest | None = None
    graph_files: list[str] = Field(default_factory=list)
    graph_sources: dict[str, str] = Field(default_factory=dict, repr=False)
    """The text of each graph file as it was read, keyed by the path in ``graph_files``.

    :func:`support_core.graph.loader.load_pack` parses this snapshot instead of reading the
    files a second time, so the ``PackPin`` it builds hashes the bytes that were actually
    parsed even if the directory changes underneath (phase-1 deferred finding P1)."""

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
