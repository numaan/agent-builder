"""Expression evaluator. Implements DESIGN.md section 6.4.

Walks the AST directly. There is no compilation step and no Python fallback, so the set of
operations an expression can perform is exactly the set of ``match`` arms below.

Attribute access is allowlisted rather than blocklisted: a name resolves only to a declared
Pydantic model field or a mapping key. Methods, dunders, class attributes and anything else
reachable through ``getattr`` are invisible, so no expression can reach ``__class__`` or call
anything. The parser already refuses names starting with ``_``; this is the second lock.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from support_core.graph.expr.filters import FILTERS, FilterError
from support_core.graph.expr.syntax import (
    ORDER_OPS,
    Attribute,
    BinaryOp,
    BoolOp,
    Compare,
    Expr,
    FilterCall,
    Literal,
    Not,
    Root,
    Unary,
)

Scope = Mapping[str, Any]
"""Root name (``state``, ``ctx``, ``result``) to the object it stands for."""

_NUMBERS = (int, float, Decimal)


class EvaluationError(ValueError):
    """An expression that parsed and type-checked could not be evaluated against real values.

    Raised for a missing field, an attribute read through a ``None``, a filter applied to the
    wrong run-time type, or an ordered comparison between incomparable values. Never a crash:
    the engine turns this into a node failure it can route through ``on_error`` (DESIGN 7.3).
    """


def evaluate(node: Expr, scope: Scope) -> Any:
    """Evaluate ``node`` against ``scope``."""
    match node:
        case Literal():
            return node.value
        case Root():
            if node.name not in scope:
                raise EvaluationError(f"root {node.name!r} is not available in this scope")
            return scope[node.name]
        case Attribute():
            return _attribute(evaluate(node.value, scope), node)
        case FilterCall():
            value = evaluate(node.value, scope)
            spec = FILTERS[node.name]
            try:
                return spec.call(value, *[a.value for a in node.args])
            except FilterError as exc:
                raise EvaluationError(str(exc)) from exc
        case Unary():
            value = evaluate(node.value, scope)
            if isinstance(value, bool) or not isinstance(value, _NUMBERS):
                raise EvaluationError(f"cannot negate {type(value).__name__}")
            return -value
        case Not():
            return not _truthy(evaluate(node.value, scope))
        case BoolOp():
            return _bool_op(node, scope)
        case Compare():
            return _compare(node, scope)
        case BinaryOp():  # pragma: no cover - nothing constructs one yet
            raise EvaluationError(f"operator {node.op!r} is not implemented")


def _bool_op(node: BoolOp, scope: Scope) -> Any:
    """``and`` / ``or`` with Python's short-circuit and value-returning semantics."""
    result: Any = None
    for index, operand in enumerate(node.values):
        result = evaluate(operand, scope)
        if index == len(node.values) - 1:
            return result
        if node.op == "and" and not _truthy(result):
            return result
        if node.op == "or" and _truthy(result):
            return result
    return result


def _compare(node: Compare, scope: Scope) -> bool:
    left = evaluate(node.left, scope)
    right = evaluate(node.right, scope)
    if node.op == "==":
        return bool(left == right)
    if node.op == "!=":
        return bool(left != right)
    if node.op in ORDER_OPS:
        _require_ordered(left, right, node.op)
        if node.op == "<":
            return bool(left < right)
        if node.op == "<=":
            return bool(left <= right)
        if node.op == ">":
            return bool(left > right)
        return bool(left >= right)
    raise EvaluationError(f"unknown comparison operator {node.op!r}")  # pragma: no cover


def _require_ordered(left: Any, right: Any, op: str) -> None:
    both_numbers = _is_number(left) and _is_number(right)
    both_strings = isinstance(left, str) and isinstance(right, str)
    if not (both_numbers or both_strings):
        raise EvaluationError(
            f"cannot compare {type(left).__name__} {op} {type(right).__name__}; "
            "ordered comparisons need two numbers or two strings"
        )


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, _NUMBERS)


def _truthy(value: Any) -> bool:
    return bool(value)


def _attribute(owner: Any, node: Attribute) -> Any:
    if owner is None:
        raise EvaluationError(
            f"cannot read {node.name!r} because the value before it is None (position {node.pos})"
        )
    if isinstance(owner, BaseModel):
        if node.name not in type(owner).model_fields:
            raise EvaluationError(
                f"{type(owner).__name__} has no field {node.name!r} (position {node.pos})"
            )
        return getattr(owner, node.name)
    if isinstance(owner, Mapping):
        if node.name not in owner:
            raise EvaluationError(f"mapping has no key {node.name!r} (position {node.pos})")
        return owner[node.name]
    raise EvaluationError(
        f"cannot read attribute {node.name!r} of {type(owner).__name__}; expressions may only "
        "traverse Pydantic models and mappings (position " + str(node.pos) + ")"
    )
