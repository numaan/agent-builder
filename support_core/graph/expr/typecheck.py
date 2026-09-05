"""Static type checker for expressions. Implements DESIGN.md sections 5.2 and 6.4.

DESIGN.md section 5.2 requires that "argument expressions type-check against the tool's input
model" and that "sub-graph input and output mappings type-check". This module is the machinery
behind both: it walks the *same* AST the evaluator walks, but against Pydantic model fields
instead of values, so ``state.elegible`` (a typo) and ``state.eligible == "true"`` (a bool
compared with a string) are load-time findings rather than run-time surprises.

It is a small, deliberately incomplete type system: enough to catch the mistakes a pack author
actually makes, honest about what it does not know (a field annotated ``Any``, or an opaque
model exported by pack tools, is :attr:`TypeInfo.unknown` and every operation on it is allowed).
"""

import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel

from support_core.graph.expr.filters import FILTERS
from support_core.graph.expr.syntax import (
    ORDER_OPS,
    Attribute,
    BinaryOp,
    BoolOp,
    Compare,
    FilterCall,
    Not,
    Root,
    Unary,
)
from support_core.graph.expr.syntax import (
    Expr as ExprNode,
)
from support_core.graph.expr.syntax import (
    Literal as LiteralNode,
)


class TypeError_(ValueError):
    """An expression is well-formed but cannot be typed against the declared models.

    Named with a trailing underscore so it does not shadow the builtin ``TypeError``, which
    would make ``except TypeError`` in unrelated code catch a validation problem.
    """

    def __init__(self, message: str, *, position: int) -> None:
        self.position = position
        super().__init__(f"{message} (position {position})")


@dataclass(frozen=True, slots=True)
class TypeNote:
    """A non-fatal observation, surfaced by the validator as a warning finding."""

    code: str
    message: str
    position: int


@dataclass(frozen=True, slots=True)
class TypeInfo:
    """What the checker knows about one expression's value."""

    annotation: Any = None
    """The non-optional part of the declared annotation, or ``None`` when unknown."""

    optional: bool = False
    """The value may be ``None``."""

    unknown: bool = False
    """The annotation is ``Any`` or an unresolved pack model; every operation is allowed."""

    literal_values: tuple[Any, ...] | None = None
    """When the annotation was ``Literal[...]``, the permitted values."""

    def describe(self) -> str:
        if self.unknown:
            return "unknown"
        base = _annotation_name(self.annotation)
        return f"{base} | None" if self.optional else base

    @property
    def category(self) -> str:
        """Coarse shape used for filter and comparison checks."""
        if self.unknown:
            return "unknown"
        annotation = self.annotation
        if annotation is None or annotation is type(None):
            return "none"
        if annotation is bool:
            return "bool"
        if annotation in (int, float, Decimal):
            return "number"
        if annotation is str:
            return "str"
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return "model"
        origin = get_origin(annotation)
        if origin in (list, tuple, set, frozenset, dict) or annotation in (list, dict):
            return "collection"
        return "other"


UNKNOWN = TypeInfo(unknown=True)
BOOL = TypeInfo(annotation=bool)
STR = TypeInfo(annotation=str)
INT = TypeInfo(annotation=int)

TypeEnv = Mapping[str, TypeInfo]
"""Root name to the type standing behind it. A missing root is a type error, not a crash."""


@dataclass(slots=True)
class _Context:
    env: TypeEnv
    notes: list[TypeNote] = field(default_factory=list)


def model_type_env(
    *,
    state: type[BaseModel] | None = None,
    ctx: type[BaseModel] | None = None,
    result: type[BaseModel] | Any | None = None,
) -> dict[str, TypeInfo]:
    """Build a :data:`TypeEnv` from the models behind ``state``, ``ctx`` and ``result``.

    A root left as ``None`` is simply absent from the environment, so an expression that
    mentions it fails with "root 'result' is not available here" rather than being typed
    ``Any``. That matters: ``result`` only exists inside a ``tool`` node's ``into`` mapping.
    """
    env: dict[str, TypeInfo] = {}
    if state is not None:
        env["state"] = TypeInfo(annotation=state)
    if ctx is not None:
        env["ctx"] = TypeInfo(annotation=ctx)
    if result is not None:
        env["result"] = from_annotation(result)
    return env


def from_annotation(annotation: Any) -> TypeInfo:
    """Turn a Python annotation into a :class:`TypeInfo`, unwrapping ``X | None``."""
    if annotation is Any:
        return UNKNOWN
    if annotation is None or annotation is type(None):
        return TypeInfo(annotation=type(None), optional=True)
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        optional = len(args) != len(get_args(annotation))
        if not args:
            return TypeInfo(annotation=type(None), optional=True)
        if len(args) > 1:
            # A genuine union of two real types. Nothing in DESIGN.md's graph format needs one,
            # and pretending to type it would be worse than admitting ignorance.
            return TypeInfo(unknown=True, optional=optional)
        inner = from_annotation(args[0])
        return TypeInfo(
            annotation=inner.annotation,
            optional=True,
            unknown=inner.unknown,
            literal_values=inner.literal_values,
        )
    if origin is Literal:
        values = get_args(annotation)
        bases = {type(v) for v in values}
        base = bases.pop() if len(bases) == 1 else str
        return TypeInfo(annotation=base, literal_values=values)
    return TypeInfo(annotation=annotation)


def infer(node: ExprNode, env: TypeEnv, notes: list[TypeNote] | None = None) -> TypeInfo:
    """Infer the type of ``node``, raising :class:`TypeError_` when it cannot be typed.

    Non-fatal observations (for example an attribute read through an optional value) are
    appended to ``notes`` when one is supplied.
    """
    context = _Context(env=env, notes=notes if notes is not None else [])
    return _infer(node, context)


def _infer(node: ExprNode, ctx: _Context) -> TypeInfo:
    match node:
        case LiteralNode():
            return _literal_type(node.value)
        case Root():
            if node.name not in ctx.env:
                available = ", ".join(sorted(ctx.env)) or "nothing"
                raise TypeError_(
                    f"root {node.name!r} is not available here; available roots: {available}",
                    position=node.pos,
                )
            return ctx.env[node.name]
        case Attribute():
            return _attribute(node, ctx)
        case FilterCall():
            return _filter(node, ctx)
        case Unary():
            inner = _infer(node.value, ctx)
            if inner.category not in ("number", "unknown"):
                raise TypeError_(f"cannot negate {inner.describe()}", position=node.pos)
            return inner if inner.category == "number" else UNKNOWN
        case Not():
            _infer(node.value, ctx)
            return BOOL
        case BoolOp():
            kinds = [_infer(value, ctx) for value in node.values]
            if all(k.category == "bool" for k in kinds):
                return BOOL
            return UNKNOWN
        case Compare():
            return _compare(node, ctx)
        case BinaryOp():  # pragma: no cover - nothing constructs one yet
            raise TypeError_(f"operator {node.op!r} is not implemented", position=node.pos)


def _literal_type(value: Any) -> TypeInfo:
    if value is None:
        return TypeInfo(annotation=type(None), optional=True)
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return INT
    if isinstance(value, float):
        return TypeInfo(annotation=float)
    return STR


def _attribute(node: Attribute, ctx: _Context) -> TypeInfo:
    owner = _infer(node.value, ctx)
    if owner.unknown:
        return UNKNOWN
    if owner.optional:
        ctx.notes.append(
            TypeNote(
                code="optional_attribute",
                message=(
                    f"{_source_hint(node.value)} may be None, so reading {node.name!r} through "
                    "it can fail at run time; guard it with a router or 'default'"
                ),
                position=node.pos,
            )
        )
    annotation = owner.annotation
    if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
        raise TypeError_(
            f"cannot read {node.name!r} of {owner.describe()}; only declared models have fields",
            position=node.pos,
        )
    fields = annotation.model_fields
    if node.name not in fields:
        known = ", ".join(sorted(fields)) or "none"
        raise TypeError_(
            f"{annotation.__name__} has no field {node.name!r}; declared fields: {known}",
            position=node.pos,
        )
    return from_annotation(fields[node.name].annotation)


def _filter(node: FilterCall, ctx: _Context) -> TypeInfo:
    value = _infer(node.value, ctx)
    spec = FILTERS[node.name]
    if value.optional and not spec.accepts_none:
        ctx.notes.append(
            TypeNote(
                code="optional_filter_input",
                message=(
                    f"filter {node.name!r} receives {value.describe()}, which fails at run time "
                    f"when the value is None; chain 'default' first"
                ),
                position=node.pos,
            )
        )
    if not value.unknown:
        category = value.category
        accepted = spec.accepts
        ok = (
            "any" in accepted
            or category in accepted
            or (category == "collection" and "sized" in accepted)
            or (category == "str" and "sized" in accepted)
        )
        if not ok:
            raise TypeError_(
                f"filter {node.name!r} does not accept {value.describe()}",
                position=node.pos,
            )
    if spec.returns == "str":
        return STR
    if spec.returns == "int":
        return INT
    if spec.returns == "same":
        return value
    # "unwrap_optional": `default` removes the None from the input type.
    fallback = _literal_type(node.args[0].value) if node.args else UNKNOWN
    if value.unknown:
        return UNKNOWN
    if fallback.category not in (value.category, "unknown", "none"):
        raise TypeError_(
            f"filter 'default' fallback is {fallback.describe()} but the value is "
            f"{value.describe()}",
            position=node.pos,
        )
    return TypeInfo(
        annotation=value.annotation, optional=False, literal_values=value.literal_values
    )


_COMPARABLE_PAIRS: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"number"}),
        frozenset({"str"}),
        frozenset({"bool"}),
        frozenset({"model"}),
        frozenset({"collection"}),
        frozenset({"other"}),
    }
)


def _compare(node: Compare, ctx: _Context) -> TypeInfo:
    left = _infer(node.left, ctx)
    right = _infer(node.right, ctx)
    if left.unknown or right.unknown:
        return BOOL
    if node.op in ORDER_OPS:
        if not (
            (left.category == "number" and right.category == "number")
            or (left.category == "str" and right.category == "str")
        ):
            raise TypeError_(
                f"cannot compare {left.describe()} {node.op} {right.describe()}; ordered "
                "comparisons need two numbers or two strings",
                position=node.pos,
            )
        if left.optional or right.optional:
            ctx.notes.append(
                TypeNote(
                    code="optional_comparison",
                    message=(
                        f"ordered comparison between {left.describe()} and {right.describe()} "
                        "raises at run time when either side is None"
                    ),
                    position=node.pos,
                )
            )
        return BOOL

    _check_equality(node, left, right)
    return BOOL


def _check_equality(node: Compare, left: TypeInfo, right: TypeInfo) -> None:
    categories = {left.category, right.category}
    if "none" in categories:
        other = left if right.category == "none" else right
        if not other.optional and other.category != "none":
            raise TypeError_(
                f"{other.describe()} is never None, so this comparison is constant",
                position=node.pos,
            )
        return
    if frozenset(categories) not in _COMPARABLE_PAIRS:
        raise TypeError_(
            f"cannot compare {left.describe()} {node.op} {right.describe()}",
            position=node.pos,
        )
    _check_literal_membership(node, left, right)


def _check_literal_membership(node: Compare, left: TypeInfo, right: TypeInfo) -> None:
    """A ``Literal[...]`` field compared with a value outside its set can never be true."""
    for typed, other_node in ((left, node.right), (right, node.left)):
        if typed.literal_values is None or not isinstance(other_node, LiteralNode):
            continue
        if other_node.value not in typed.literal_values:
            allowed = ", ".join(repr(v) for v in typed.literal_values)
            raise TypeError_(
                f"{other_node.value!r} is not one of the declared values ({allowed}), so this "
                "comparison can never be true",
                position=node.pos,
            )


def _annotation_name(annotation: Any) -> str:
    if annotation is None:
        return "unknown"
    if annotation is type(None):
        return "None"
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _source_hint(node: ExprNode) -> str:
    from support_core.graph.expr.syntax import unparse

    return unparse(node)
