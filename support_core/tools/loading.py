"""Importing a pack's tools. Implements the "packs export ``TOOLS: list[Tool]``" half of
DESIGN.md section 8.3, and closes phase-1 deferred finding I.

Importing a pack executes the pack's Python. That is what DESIGN.md asks for - a tool is code -
and it is why ``load_pack`` is a startup step rather than a request-time one. What this module
guarantees is that an import that goes wrong becomes a *finding* rather than a stack trace
escaping the validator, and that a pack's ``tools/`` package is imported under a name derived
from its own path, so two packs (or two versions of one pack, DESIGN.md section 6.7) can be
loaded side by side without one shadowing the other.
"""

import hashlib
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from support_core.tools.base import Tool
from support_core.tools.registry import RegistryError, ToolRegistry

TOOLS_ATTRIBUTE = "TOOLS"


class PackToolsImportError(ImportError):
    """A pack's ``tools/`` package could not be imported, or does not export ``TOOLS``."""


def module_name_for(tools_dir: Path) -> str:
    """A unique, stable module name for one pack's ``tools`` package.

    Derived from the resolved path, so the same pack imports once and two different packs never
    collide - which matters because every pack's package is literally called ``tools``.
    """
    digest = hashlib.sha256(str(tools_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"support_pack_tools_{digest}"


def import_pack_tools(pack_path: Path) -> ModuleType:
    """Import ``<pack>/tools/__init__.py`` as a package and return the module."""
    tools_dir = Path(pack_path) / "tools"
    init = tools_dir / "__init__.py"
    if not init.is_file():
        msg = f"{init} does not exist; a pack must have a tools package (DESIGN.md section 5)"
        raise PackToolsImportError(msg)
    name = module_name_for(tools_dir)
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(tools_dir)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - only for an unreadable file
        msg = f"{init} cannot be loaded as a Python module"
        raise PackToolsImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:
        # A half-imported module must not stay in sys.modules: the next attempt would find it
        # and believe the pack imported cleanly.
        sys.modules.pop(name, None)
        msg = f"{init}: importing the pack's tools raised {type(exc).__name__}: {exc}"
        raise PackToolsImportError(msg) from exc
    return module


def forget_pack_tools(pack_path: Path) -> None:
    """Drop a pack's imported tools module. For tests that rewrite a pack on disk."""
    sys.modules.pop(module_name_for(Path(pack_path) / "tools"), None)


def registry_for_pack(pack_path: Path) -> ToolRegistry:
    """Import the pack and build its registry. Raises :class:`PackToolsImportError` on any
    problem, including a ``TOOLS`` export that is not a list of :class:`Tool`."""
    module = import_pack_tools(pack_path)
    exported = getattr(module, TOOLS_ATTRIBUTE, None)
    if exported is None:
        msg = (
            f"{pack_path}/tools/__init__.py does not define {TOOLS_ATTRIBUTE} "
            f"(DESIGN.md section 8.3)"
        )
        raise PackToolsImportError(msg)
    if isinstance(exported, Tool) or not isinstance(exported, Sequence):
        msg = f"{TOOLS_ATTRIBUTE} must be a list of Tool, not {type(exported).__name__}"
        raise PackToolsImportError(msg)
    try:
        return ToolRegistry(list(exported))
    except RegistryError as exc:
        msg = f"{pack_path}/tools/__init__.py: {exc}"
        raise PackToolsImportError(msg) from exc
