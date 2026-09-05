"""The sandboxed expression language. Implements DESIGN.md sections 6.4 and 5.2.

DESIGN.md section 6.4: "Expressions (``state.x``, ``ctx.customer.y``, ``result.z``) use a small
sandboxed expression language, not Python ``eval``. Comparisons, boolean logic, attribute
access, and a fixed set of filters."

The language is deliberately tiny and has four independent pieces, none of which uses ``eval``,
``exec``, ``compile`` or ``ast``:

* :mod:`~support_core.graph.expr.lexer` turns source text into tokens with positions.
* :mod:`~support_core.graph.expr.parser` is a recursive-descent parser producing the frozen AST
  in :mod:`~support_core.graph.expr.syntax`. Every rejection is a :class:`ParseError` naming the
  offending token and its position.
* :mod:`~support_core.graph.expr.evaluate` walks the AST against real values at run time.
* :mod:`~support_core.graph.expr.typecheck` walks the same AST against Pydantic model fields at
  load time, so a bad expression is a validation finding rather than a run-time surprise.

The only names an expression can reach are the three roots ``state``, ``ctx`` and ``result``
(:data:`ROOTS`) and the four filters in :mod:`~support_core.graph.expr.filters`.
"""

from support_core.graph.expr.evaluate import EvaluationError, Scope, evaluate
from support_core.graph.expr.filters import FILTERS, FilterError
from support_core.graph.expr.lexer import Token, TokenKind, tokenize
from support_core.graph.expr.parser import MAX_DEPTH, MAX_LENGTH, MAX_TOKENS, ParseError, parse
from support_core.graph.expr.syntax import (
    ROOTS,
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
    roots_used,
    walk,
)
from support_core.graph.expr.typecheck import (
    TypeEnv,
    TypeError_,
    TypeInfo,
    infer,
    model_type_env,
)

__all__ = [
    "FILTERS",
    "MAX_DEPTH",
    "MAX_LENGTH",
    "MAX_TOKENS",
    "ROOTS",
    "Attribute",
    "BinaryOp",
    "BoolOp",
    "Compare",
    "EvaluationError",
    "Expr",
    "FilterCall",
    "FilterError",
    "Literal",
    "Not",
    "ParseError",
    "Root",
    "Scope",
    "Token",
    "TokenKind",
    "TypeEnv",
    "TypeError_",
    "TypeInfo",
    "Unary",
    "evaluate",
    "infer",
    "model_type_env",
    "parse",
    "roots_used",
    "tokenize",
    "walk",
]
