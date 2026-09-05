"""Expression AST. Implements DESIGN.md section 6.4 (the expression language).

Every node is a frozen dataclass carrying the source position it started at, so the evaluator,
the type checker and the validator can all point at the offending character. The AST is
deliberately closed: there is no generic "call" or "subscript" node, because there is nothing
in the language that could produce one.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal as TypingLiteral

ROOTS: frozenset[str] = frozenset({"state", "ctx", "result"})
"""The only names an expression may start from (DESIGN.md section 6.4)."""

CompareOp = TypingLiteral["==", "!=", "<", "<=", ">", ">="]
BoolOperator = TypingLiteral["and", "or"]

COMPARE_OPS: frozenset[str] = frozenset({"==", "!=", "<", "<=", ">", ">="})
ORDER_OPS: frozenset[str] = frozenset({"<", "<=", ">", ">="})


@dataclass(frozen=True, slots=True)
class Node:
    """Base for every AST node. ``pos`` is a 0-based character offset into the source."""

    pos: int


@dataclass(frozen=True, slots=True)
class Literal(Node):
    """A string, int, float, bool or None literal."""

    value: str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class Root(Node):
    """One of ``state``, ``ctx``, ``result``."""

    name: str


@dataclass(frozen=True, slots=True)
class Attribute(Node):
    """``<value>.<name>``."""

    value: "Expr"
    name: str


@dataclass(frozen=True, slots=True)
class FilterCall(Node):
    """``<value> | name`` or ``<value> | name(arg, ...)``. Arguments are literals only."""

    value: "Expr"
    name: str
    args: tuple[Literal, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Unary(Node):
    """Arithmetic negation, the only unary operator besides ``not``."""

    op: TypingLiteral["-"]
    value: "Expr"


@dataclass(frozen=True, slots=True)
class Not(Node):
    """``not <value>``."""

    value: "Expr"


@dataclass(frozen=True, slots=True)
class Compare(Node):
    """A single, non-chained comparison."""

    op: CompareOp
    left: "Expr"
    right: "Expr"


@dataclass(frozen=True, slots=True)
class BoolOp(Node):
    """``and`` / ``or`` over two or more operands, left-associative and flattened."""

    op: BoolOperator
    values: tuple["Expr", ...]


@dataclass(frozen=True, slots=True)
class BinaryOp(Node):
    """Reserved for a future arithmetic operator.

    Nothing constructs one today: DESIGN.md section 6.4 lists only comparisons, boolean logic,
    attribute access and filters. It exists so that adding arithmetic later does not change the
    shape of :func:`walk` or the exhaustiveness checks around it.
    """

    op: str
    left: "Expr"
    right: "Expr"


Expr = Literal | Root | Attribute | FilterCall | Unary | Not | Compare | BoolOp | BinaryOp


def walk(node: Expr) -> Iterator[Expr]:
    """Yield ``node`` and every descendant, parents before children."""
    yield node
    match node:
        case Literal() | Root():
            return
        case Attribute() | FilterCall() | Unary() | Not():
            yield from walk(node.value)
        case Compare() | BinaryOp():
            yield from walk(node.left)
            yield from walk(node.right)
        case BoolOp():
            for value in node.values:
                yield from walk(value)


def roots_used(node: Expr) -> set[str]:
    """The set of roots (``state``, ``ctx``, ``result``) the expression reads."""
    return {n.name for n in walk(node) if isinstance(n, Root)}


_STRING_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\t": "\\t", "\r": "\\r", "\0": "\\0"}
"""The only escapes the lexer accepts that ``repr`` would not produce identically."""


def literal_source(value: str | int | float | bool | None) -> str:
    """Render a literal so that the lexer reads it back to the same value.

    ``repr`` is not enough for strings: it writes ``'\\x07'`` for a control character, and the
    lexer has no ``\\x`` escape, so the "canonical" form would not re-parse (phase-1 review
    finding F6). The lexer refuses raw control characters in a literal, so the only ones that
    can reach here are those :data:`_STRING_ESCAPES` can write back.
    """
    if not isinstance(value, str):
        return repr(value)
    out = ["'"]
    for char in value:
        out.append(_STRING_ESCAPES.get(char, char))
    out.append("'")
    return "".join(out)


def unparse(node: Expr) -> str:
    """Render the AST back to canonical source. Used to compare two expressions for equality.

    DESIGN.md section 8.2 binds an approval to the arguments of an action; the validator
    compares a ``confirm`` node's declared arguments with the ``tool`` node's by comparing
    canonical text, so ``state.charge_id`` and ``state . charge_id`` count as the same. The
    output re-parses to the same AST, which is what makes that comparison trustworthy.
    """
    match node:
        case Literal():
            return literal_source(node.value)
        case Root():
            return node.name
        case Attribute():
            return f"{unparse(node.value)}.{node.name}"
        case FilterCall():
            args = ", ".join(literal_source(a.value) for a in node.args)
            call = f"({args})" if node.args else ""
            return f"{unparse(node.value)} | {node.name}{call}"
        case Unary():
            return f"-{unparse(node.value)}"
        case Not():
            return f"not {unparse(node.value)}"
        case Compare():
            return f"({unparse(node.left)} {node.op} {unparse(node.right)})"
        case BoolOp():
            joined = f" {node.op} ".join(unparse(v) for v in node.values)
            return f"({joined})"
        case BinaryOp():
            return f"({unparse(node.left)} {node.op} {unparse(node.right)})"
