"""The package tree matches DESIGN.md section 18 and PLAN.md's docstring convention."""

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

import support_core

DESIGN_SUBPACKAGES = [
    "api",
    "engine",
    "graph",
    "tools",
    "knowledge",
    "llm",
    "memory",
    "channels",
    "handoff",
    "guardrails",
    "observability",
    "eval",
    "storage",
    "cli",
]
"""Every directory listed in DESIGN.md section 18."""


DESIGN_REFERENCE = re.compile(r"DESIGN\.md\s+sections?\s+\d")
"""Matches 'DESIGN.md section 17' even when a docstring wraps between the words."""


@pytest.mark.parametrize("name", DESIGN_SUBPACKAGES)
def test_design_subpackage_exists_with_design_reference(name: str) -> None:
    module = importlib.import_module(f"support_core.{name}")
    assert module.__doc__, f"support_core.{name} needs a docstring naming its DESIGN.md section"
    assert DESIGN_REFERENCE.search(module.__doc__)


def test_no_undocumented_modules() -> None:
    """PLAN.md: every module under support_core/ starts with a docstring naming its section."""
    root = Path(support_core.__file__).parent
    offenders: list[str] = []
    for info in pkgutil.walk_packages([str(root)], prefix="support_core."):
        if ".migrations" in info.name:
            continue
        module = importlib.import_module(info.name)
        if not DESIGN_REFERENCE.search(module.__doc__ or ""):
            offenders.append(info.name)
    assert offenders == []


def test_subpackage_set_is_exactly_the_design_list() -> None:
    root = Path(support_core.__file__).parent
    on_disk = sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "__init__.py").exists())
    assert on_disk == sorted(DESIGN_SUBPACKAGES)


def test_version_is_a_string() -> None:
    assert isinstance(support_core.__version__, str)
