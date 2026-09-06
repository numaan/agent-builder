"""The tool registry. Implements DESIGN.md section 8.3's first bullet.

    Packs export ``TOOLS: list[Tool]``. The registry rejects duplicate names and validates that
    models are JSON-schema serializable.

The registry is **the** source of a risk tier at run time. Phase 1 had to read tiers from the
pack-authored ``tools/tools.yaml``, which meant declaring ``issue_refund`` as ``read`` removed
every check (phase-1 deferred finding I). Nothing in the runtime reads that file any more: the
tier comes from the imported ``Tool`` object, and the validator reports a disagreement between
the two as an error rather than picking one.
"""

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from pydantic import BaseModel

from support_core.tools.base import Tool
from support_core.tools.risk import Risk


class RegistryError(ValueError):
    """A pack's ``TOOLS`` export cannot be turned into a registry."""


class ToolRegistry:
    """Every tool one pack can run, by name."""

    def __init__(self, tools: Iterable[Any] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.add(tool)

    def add(self, tool: Any) -> None:
        """Add one exported object, checking that it is a tool before believing it is.

        ``tool`` is typed ``Any`` because it comes from a pack's ``TOOLS`` list, which is
        arbitrary Python until this method has looked at it.
        """
        if not isinstance(tool, Tool):
            msg = (
                f"TOOLS must contain support_core.Tool instances; got "
                f"{type(tool).__name__} ({tool!r})"
            )
            raise RegistryError(msg)
        if tool.name in self._tools:
            msg = f"duplicate tool name {tool.name!r}; a pack may export each name once"
            raise RegistryError(msg)
        for kind, model in (("input", tool.input_model), ("output", tool.output_model)):
            _check_model(tool.name, kind, model)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools)) or "none"
            msg = f"no tool named {name!r} in this pack; it exports {known}"
            raise RegistryError(msg)
        return tool

    def risk_of(self, name: str) -> Risk | None:
        tool = self._tools.get(name)
        return tool.risk if tool is not None else None

    @property
    def risks(self) -> dict[str, Risk]:
        """Every tier, for the cross-check the model-loop gateway does before it calls."""
        return {name: tool.risk for name, tool in self._tools.items()}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    @property
    def exemptions(self) -> list[Tool]:
        """Every ``confirm_exempt`` tool, for the validator's report (DESIGN.md section 8.2)."""
        return [tool for tool in self._tools.values() if tool.confirm_exempt]

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __contains__(self, name: object) -> bool:
        return name in self._tools


def _check_model(tool_name: str, kind: str, model: Any) -> None:
    """A tool's models must be Pydantic models that produce a JSON schema.

    The schema is what the model is shown for a READ tool (DESIGN.md section 8.4) and what the
    validator type-checks argument expressions against, so a model that cannot produce one is a
    tool that cannot be used - better said at startup than on a customer's turn.
    """
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        msg = f"tool {tool_name!r}: {kind}_model must be a pydantic BaseModel subclass"
        raise RegistryError(msg)
    try:
        model.model_json_schema()
    except Exception as exc:
        # DESIGN.md 8.3: "validates that models are JSON-schema serializable". A field pydantic
        # cannot describe - an arbitrary class, a callable - raises here rather than at the
        # moment a model is offered the tool.
        msg = f"tool {tool_name!r}: {kind}_model has no JSON schema: {exc}"
        raise RegistryError(msg) from exc


def field_types(model: type[BaseModel]) -> dict[str, str]:
    """A tool model's fields as the type strings ``tools/tools.yaml`` writes.

    Used only to report drift between the imported registry and a pack's declarative manifest,
    so it is a best-effort rendering: an annotation it cannot name is rendered as-is and compared
    textually.
    """
    rendered: dict[str, str] = {}
    for name, info in model.model_fields.items():
        rendered[name] = _annotation_text(info.annotation)
    return rendered


def _annotation_text(annotation: Any) -> str:
    if annotation is None:
        return "None"
    text = getattr(annotation, "__name__", None)
    if isinstance(text, str) and not getattr(annotation, "__args__", None):
        return text
    return (
        str(annotation).replace("typing.", "").replace("Optional[", "").replace("NoneType", "None")
    )


def tool_input_names(tools: Sequence[Tool]) -> dict[str, tuple[str, ...]]:
    return {tool.name: tuple(tool.input_model.model_fields) for tool in tools}
