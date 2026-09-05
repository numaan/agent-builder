"""Type strings in graph files. Implements DESIGN.md section 6.4 (``state``, ``inputs``,
``outputs``) and section 5.2 (the type checks that need real Pydantic models).

A graph file declares its state as ``name: <type string>``::

    state:
      charge_id: str | None
      charge: Charge | None            # Pydantic model exported by pack tools
      outcome: Literal["refunded", "denied", "escalated", "abandoned"]

Those strings are turned into real annotations by a second small recursive-descent parser (the
same no-``eval`` rule as the expression language: ``typing.get_type_hints`` and
``eval`` are both off the table because a graph file is untrusted data), and then into a real
Pydantic model with :func:`pydantic.create_model`. The model is what
:mod:`support_core.graph.expr.typecheck` walks.

Names the parser does not know (``Charge`` above) are *unresolved*: they become ``Any`` and are
reported, because the tools that export those models are stubs until phase 4.
"""

import keyword
from dataclasses import dataclass
from decimal import Decimal
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, create_model

BUILTIN_TYPES: dict[str, Any] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "Decimal": Decimal,
    "decimal": Decimal,
    "Any": Any,
    "None": type(None),
    "none": type(None),
    "null": type(None),
}
GENERIC_TYPES: frozenset[str] = frozenset({"list", "dict", "set", "tuple"})
RESERVED_FIELD_PREFIXES: tuple[str, ...] = ("_", "model_")
"""``_x`` is unaddressable by the expression language; ``model_x`` collides with Pydantic."""

_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789")
_DIGITS = frozenset("0123456789")


class TypeSpecError(ValueError):
    """A type string in a graph file is not a type this core understands."""

    def __init__(self, message: str, *, source: str, position: int) -> None:
        self.source = source
        self.position = position
        super().__init__(f"{message} at position {position} in {source!r}")


@dataclass(frozen=True, slots=True)
class ParsedType:
    annotation: Any
    unresolved: frozenset[str]
    """Names that are not builtin types and not resolvable in phase 1 (pack models)."""

    @property
    def optional(self) -> bool:
        return type(None) in _union_members(self.annotation)


def parse_type(source: str) -> ParsedType:
    """Parse a type string such as ``str | None`` or ``Literal["a", "b"]``."""
    return _TypeParser(source).parse()


@dataclass(frozen=True, slots=True)
class FieldIssue:
    """Something wrong with, or unresolved in, one declared field."""

    field: str
    code: str
    """``invalid_type``, ``unresolved_type``, ``bad_name`` or ``not_optional``."""
    message: str


@dataclass(frozen=True, slots=True)
class BuiltModel:
    model: type[BaseModel]
    issues: tuple[FieldIssue, ...]


def build_model(name: str, fields: dict[str, str], *, all_optional: bool = False) -> BuiltModel:
    """Build a Pydantic model from ``{field name: type string}``.

    ``all_optional`` gives every field the default ``None``. Graph *state* is built that way
    because a frame starts with nothing filled in (DESIGN.md section 6.1: "Nodes read and patch
    it"); ``inputs`` and ``outputs`` are not, because a caller supplies them in full.
    """
    definitions: dict[str, Any] = {}
    issues: list[FieldIssue] = []
    for field_name, spec in fields.items():
        problem = _field_name_problem(field_name)
        if problem is not None:
            issues.append(FieldIssue(field=field_name, code="bad_name", message=problem))
            continue
        try:
            parsed = parse_type(spec)
        except TypeSpecError as exc:
            issues.append(FieldIssue(field=field_name, code="invalid_type", message=str(exc)))
            definitions[field_name] = (Any, Field(default=None))
            continue
        if parsed.unresolved:
            issues.append(
                FieldIssue(
                    field=field_name,
                    code="unresolved_type",
                    message=(
                        f"{', '.join(sorted(parsed.unresolved))} is not a type this core knows; "
                        "it is typed as Any until the pack's tool models are registered (phase 4)"
                    ),
                )
            )
        if all_optional and not parsed.optional and parsed.annotation is not Any:
            issues.append(
                FieldIssue(
                    field=field_name,
                    code="not_optional",
                    message=(
                        f"state field {field_name!r} is declared {spec!r}, which is never empty, "
                        "but a frame starts with no state; declare it optional "
                        f"({spec} | None) so an unset value is representable"
                    ),
                )
            )
        default = Field(default=None) if all_optional or parsed.optional else Field()
        definitions[field_name] = (parsed.annotation, default)

    model = create_model(
        name,
        __config__=ConfigDict(extra="forbid", arbitrary_types_allowed=True),
        **definitions,
    )
    return BuiltModel(model=model, issues=tuple(issues))


def _field_name_problem(name: str) -> str | None:
    if not name or name[0] not in _NAME_START or any(c not in _NAME_CHARS for c in name):
        return f"{name!r} is not a valid field name"
    if name.startswith(RESERVED_FIELD_PREFIXES):
        return (
            f"{name!r} starts with a reserved prefix "
            f"({' or '.join(RESERVED_FIELD_PREFIXES)}) and would be unreachable"
        )
    if keyword.iskeyword(name):
        return f"{name!r} is a Python keyword"
    return None


def _union_members(annotation: Any) -> tuple[Any, ...]:
    if get_origin(annotation) in (Union, UnionType):
        return get_args(annotation)
    return (annotation,)


class _TypeParser:
    """``union := atom ("|" atom)*`` with ``atom := NAME ("[" ... "]")?``."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.i = 0
        self.unresolved: set[str] = set()

    def parse(self) -> ParsedType:
        annotation = self.union()
        self.skip_space()
        if self.i < len(self.source):
            raise self.fail("unexpected trailing text")
        return ParsedType(annotation=annotation, unresolved=frozenset(self.unresolved))

    # -- helpers -------------------------------------------------------------------------

    def fail(self, message: str) -> TypeSpecError:
        return TypeSpecError(message, source=self.source, position=self.i)

    def skip_space(self) -> None:
        while self.i < len(self.source) and self.source[self.i] in " \t\r\n":
            self.i += 1

    def at(self, char: str) -> bool:
        self.skip_space()
        return self.i < len(self.source) and self.source[self.i] == char

    def take(self, char: str) -> None:
        if not self.at(char):
            raise self.fail(f"expected {char!r}")
        self.i += 1

    def name(self) -> str:
        self.skip_space()
        if self.i >= len(self.source) or self.source[self.i] not in _NAME_START:
            raise self.fail("expected a type name")
        start = self.i
        while self.i < len(self.source) and self.source[self.i] in _NAME_CHARS:
            self.i += 1
        return self.source[start : self.i]

    # -- grammar -------------------------------------------------------------------------

    def union(self) -> Any:
        members = [self.atom()]
        while self.at("|"):
            self.take("|")
            members.append(self.atom())
        annotation = members[0]
        for member in members[1:]:
            annotation = annotation | member
        return annotation

    def atom(self) -> Any:
        name = self.name()
        if name == "Literal":
            return self.literal()
        if name == "Optional":
            self.take("[")
            inner = self.union()
            self.take("]")
            return inner | None
        if self.at("["):
            if name not in GENERIC_TYPES:
                raise self.fail(f"{name!r} does not take type parameters")
            self.take("[")
            args = [self.union()]
            while self.at(","):
                self.take(",")
                args.append(self.union())
            self.take("]")
            return self.generic(name, args)
        if name in BUILTIN_TYPES:
            return BUILTIN_TYPES[name]
        if name in GENERIC_TYPES:
            return {"list": list, "dict": dict, "set": set, "tuple": tuple}[name]
        self.unresolved.add(name)
        return Any

    def generic(self, name: str, args: list[Any]) -> Any:
        if name == "list":
            if len(args) != 1:
                raise self.fail("list takes exactly one type parameter")
            return list[args[0]]  # type: ignore[valid-type]
        if name == "set":
            if len(args) != 1:
                raise self.fail("set takes exactly one type parameter")
            return set[args[0]]  # type: ignore[valid-type]
        if name == "dict":
            if len(args) != 2:
                raise self.fail("dict takes exactly two type parameters")
            return dict[args[0], args[1]]  # type: ignore[valid-type]
        return tuple[tuple(args)]  # type: ignore[misc]

    def literal(self) -> Any:
        self.take("[")
        values: list[Any] = [self.literal_value()]
        while self.at(","):
            self.take(",")
            values.append(self.literal_value())
        self.take("]")
        return Literal[tuple(values)]

    def literal_value(self) -> Any:
        self.skip_space()
        if self.i >= len(self.source):
            raise self.fail("expected a literal value")
        char = self.source[self.i]
        if char in {"'", '"'}:
            self.i += 1
            start = self.i
            while self.i < len(self.source) and self.source[self.i] != char:
                if self.source[self.i] == "\\":
                    raise self.fail("escape sequences are not supported in Literal values")
                self.i += 1
            if self.i >= len(self.source):
                raise self.fail("unterminated string in Literal")
            value = self.source[start : self.i]
            self.i += 1
            return value
        if char in _DIGITS or char == "-":
            start = self.i
            if char == "-":
                self.i += 1
            while self.i < len(self.source) and self.source[self.i] in _DIGITS:
                self.i += 1
            text = self.source[start : self.i]
            if text in ("", "-"):
                raise self.fail("expected a number in Literal")
            return int(text)
        word = self.name()
        if word in ("True", "true"):
            return True
        if word in ("False", "false"):
            return False
        raise self.fail(f"{word!r} is not a valid Literal value")
