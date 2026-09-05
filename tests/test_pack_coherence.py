"""Two phase-1 deferred findings this phase owns: P1 and P2 (BACKLOG.md "Deferred findings").

P1: ``load_pack`` read and parsed every graph file twice, so a pack edited on disk between the
two reads could produce a ``PackPin`` whose hashes describe a mix of two versions - and
DESIGN.md section 6.7 pins a running conversation to exactly one version.

P2: ``templates.ENVIRONMENT`` was a process-wide Jinja environment with a shared template cache,
which is harmless while nothing varies per pack and a concurrency bug the moment a pack supplies
a filter or two pack versions are loaded side by side (section 6.7 keeps the last two loaded).
"""

import shutil
from pathlib import Path

import pytest

from support_core import load_pack
from support_core.graph import templates
from support_core.graph.loader import load_pack_report
from support_core.graph.pack import build_pin
from support_core.graph.schema import read_graphs
from support_core.graph.validator import validate_pack
from tests.engine_support import DETERMINISTIC_PACK, ENGINE_PACK


@pytest.fixture
def pack_copy(tmp_path: Path) -> Path:
    target = tmp_path / "pack"
    shutil.copytree(DETERMINISTIC_PACK, target)
    return target


def test_each_graph_file_is_read_once(pack_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P1, directly: the pin can only describe one version if only one version was read."""
    reads: list[str] = []
    original = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.suffix in {".yaml", ".yml"} and self.parent.name == "graphs":
            reads.append(self.name)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting_read_text)
    load_pack(pack_copy)

    assert sorted(reads) == ["root.yaml", "tier.yaml"]


def test_the_pin_hashes_the_bytes_that_were_parsed(pack_copy: Path) -> None:
    """P1, behaviourally: an edit landing between the two parses cannot be half-applied."""
    report = validate_pack(pack_copy)
    snapshot = dict(report.graph_sources)
    assert set(snapshot) == set(report.graph_files)

    # The directory changes underneath us, exactly as a deploy would.
    root = pack_copy / "graphs" / "root.yaml"
    root.write_text(
        root.read_text(encoding="utf-8").replace("Hello", "Good morning"), encoding="utf-8"
    )

    graphs, findings = read_graphs(pack_copy, report.graph_files, sources=report.graph_sources)
    assert not findings
    assert "Hello" in graphs["root"].nodes["greet"].message  # type: ignore[attr-defined]
    pin = build_pin(report.manifest, graphs, "0.0.1")  # type: ignore[arg-type]
    assert pin.graphs["root"].source_hash != load_pack(pack_copy).pin.graphs["root"].source_hash
    assert pin.graphs["tier"].source_hash == load_pack(pack_copy).pin.graphs["tier"].source_hash


def test_every_pack_gets_its_own_template_environment() -> None:
    """P2: no Jinja environment - and so no template cache - is shared between packs."""
    first = load_pack(DETERMINISTIC_PACK)
    second = load_pack(DETERMINISTIC_PACK)
    third = load_pack(ENGINE_PACK)

    assert first.environment is not second.environment
    assert first.environment is not third.environment
    assert first.environment is not templates.ENVIRONMENT

    # A filter added to one pack's environment - which is what a pack-supplied filter would be -
    # is invisible to the others and to the module-level fallback.
    first.environment.filters["shout"] = str.upper
    assert "shout" not in second.environment.filters
    assert "shout" not in third.environment.filters
    assert "shout" not in templates.ENVIRONMENT.filters


def test_the_validator_uses_an_environment_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation must not warm or poison the process-wide cache either."""
    made: list[object] = []
    original = templates.make_environment

    def counting_make_environment() -> object:
        environment = original()
        made.append(environment)
        return environment

    monkeypatch.setattr(templates, "make_environment", counting_make_environment)
    monkeypatch.setattr("support_core.graph.rules.make_environment", counting_make_environment)
    load_pack_report(DETERMINISTIC_PACK)

    assert made, "the validator built no environment of its own"
    assert all(environment is not templates.ENVIRONMENT for environment in made)


def test_rendering_goes_through_the_pack_environment() -> None:
    """The engine renders with ``Pack.environment``; ``render`` must honour the argument."""
    pack = load_pack(DETERMINISTIC_PACK)
    pack.environment.filters["shout"] = str.upper
    assert templates.render("{{ 'hi' | shout }}", {}, env=pack.environment) == "HI"
    with pytest.raises(templates.TemplateError, match="shout"):
        templates.render("{{ 'hi' | shout }}", {})
