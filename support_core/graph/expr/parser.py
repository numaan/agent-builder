"""Recursive-descent expression parser. Implements DESIGN.md section 6.4.

Grammar (the whole language)::

    expression := or_expr
    or_expr    := and_expr ("or" and_expr)*
    and_expr   := not_expr ("and" not_expr)*
    not_expr   := "not" not_expr | comparison
    comparison := unary (("==" | "!=" | "<" | "<=" | ">" | ">=") unary)?   # non-associative
    unary      := "-" unary | postfix
    postfix    := primary ("." NAME | "|" filter)*
    primary    := "(" expression ")" | literal | ROOT
    filter     := NAME ("(" literal ("," literal)* ")")?
    ROOT       := "state" | "ctx" | "result"

Filters bind tighter than comparisons and looser than attribute access, as in Jinja, so
``state.x | len > 3`` parses as ``(state.x | len) > 3``.

:func:`parse` is total on ``str``: it either returns an AST or raises :class:`ParseError`. It
never raises ``RecursionError`` (nesting is capped by :data:`MAX_DEPTH`), never evaluates
anything, and never touches ``eval``, ``exec``, ``compile`` or the ``ast`` module.
"""

from support_core.graph.expr.filters import FILTERS
from support_core.graph.expr.lexer import (
    MAX_LENGTH,
    MAX_TOKENS,
    ParseError,
    Token,
    TokenKind,
    tokenize,
)
from support_core.graph.expr.syntax import (
    COMPARE_OPS,
    ROOTS,
    Attribute,
    BoolOp,
    Compare,
    CompareOp,
    Expr,
    FilterCall,
    Literal,
    Not,
    Root,
    Unary,
)

__all__ = ["MAX_DEPTH", "MAX_LENGTH", "MAX_TOKENS", "ParseError", "parse"]

MAX_DEPTH = 32
"""Maximum nesting depth. Without this, ``"(" * 100000`` is a ``RecursionError``, which is an
unexpected exception type escaping a function whose contract is "``ParseError`` or an AST"."""

KEYWORDS: frozenset[str] = frozenset({"and", "or", "not"})
NAMED_LITERALS: dict[str, str | int | float | bool | None] = {
    # Graph files are YAML, so the lowercase spellings are the ones authors actually write
    # (DESIGN.md section 6.4 uses `state.eligible == true`). The Python spellings are accepted
    # too because they are what a Python-shaped reviewer will type.
    "true": True,
    "false": False,
    "null": None,
    "none": None,
    "True": True,
    "False": False,
    "None": None,
}
RESERVED: frozenset[str] = frozenset(KEYWORDS | set(NAMED_LITERALS) | ROOTS)


def parse(source: str) -> Expr:
    """Parse ``source`` into an :data:`~support_core.graph.expr.syntax.Expr`.

    Raises :class:`ParseError` for every malformed input, including the empty string.
    """
    if not isinstance(source, str):  # pragma: no cover - defensive; callers are typed
        raise TypeError(f"expression source must be a string, not {type(source).__name__}")
    return _Parser(source).parse()


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = tokenize(source)
        self.index = 0
        self.depth = 0

    # -- token helpers -------------------------------------------------------------------

    def peek(self) -> Token:
        """The token at the cursor. A method, not a property, so mypy does not narrow its type
        across an ``advance()`` that changes it."""
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        if token.kind is not TokenKind.EOF:
            self.index += 1
        return token

    def at_op(self, *texts: str) -> bool:
        return self.peek().kind is TokenKind.OP and self.peek().text in texts

    def at_keyword(self, *words: str) -> bool:
        return self.peek().kind is TokenKind.NAME and self.peek().text in words

    def fail(self, message: str, token: Token | None = None) -> ParseError:
        token = token or self.peek()
        text = token.text if token.kind is not TokenKind.EOF else ""
        return ParseError(message, source=self.source, position=token.pos, token=text)

    # -- grammar -------------------------------------------------------------------------

    def parse(self) -> Expr:
        if self.peek().kind is TokenKind.EOF:
            raise self.fail("expression is empty")
        node = self.or_expr()
        if self.peek().kind is not TokenKind.EOF:
            raise self.fail("unexpected trailing input")
        return node

    def _descend(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.fail(f"expression nests deeper than {MAX_DEPTH} levels")

    def or_expr(self) -> Expr:
        self._descend()
        try:
            values = [self.and_expr()]
            while self.at_keyword("or"):
                self.advance()
                values.append(self.and_expr())
            if len(values) == 1:
                return values[0]
            return BoolOp(pos=values[0].pos, op="or", values=tuple(values))
        finally:
            self.depth -= 1

    def and_expr(self) -> Expr:
        self._descend()
        try:
            values = [self.not_expr()]
            while self.at_keyword("and"):
                self.advance()
                values.append(self.not_expr())
            if len(values) == 1:
                return values[0]
            return BoolOp(pos=values[0].pos, op="and", values=tuple(values))
        finally:
            self.depth -= 1

    def not_expr(self) -> Expr:
        if self.at_keyword("not"):
            self._descend()
            try:
                token = self.advance()
                return Not(pos=token.pos, value=self.not_expr())
            finally:
                self.depth -= 1
        return self.comparison()

    def comparison(self) -> Expr:
        self._descend()
        try:
            left = self.unary()
            if not (self.peek().kind is TokenKind.OP and self.peek().text in COMPARE_OPS):
                return left
            op_token = self.advance()
            right = self.unary()
            if self.peek().kind is TokenKind.OP and self.peek().text in COMPARE_OPS:
                raise self.fail(
                    "chained comparisons are not supported; use 'and' between two comparisons"
                )
            op: CompareOp = op_token.text  # type: ignore[assignment]
            return Compare(pos=left.pos, op=op, left=left, right=right)
        finally:
            self.depth -= 1

    def unary(self) -> Expr:
        if self.at_op("-"):
            self._descend()
            try:
                token = self.advance()
                return Unary(pos=token.pos, op="-", value=self.unary())
            finally:
                self.depth -= 1
        return self.postfix()

    def postfix(self) -> Expr:
        node = self.primary()
        while True:
            if self.at_op("."):
                self.advance()
                name_token = self.peek()
                if name_token.kind is not TokenKind.NAME:
                    raise self.fail("expected an attribute name after '.'")
                if name_token.text in RESERVED:
                    raise self.fail(
                        f"{name_token.text!r} is a reserved word and cannot be an attribute name"
                    )
                self.advance()
                node = Attribute(pos=node.pos, value=node, name=name_token.text)
                continue
            if self.at_op("|"):
                self.advance()
                node = self.filter_call(node)
                continue
            if self.at_op("("):
                raise self.fail("calls are not supported; only the declared filters take arguments")
            return node

    def filter_call(self, value: Expr) -> Expr:
        name_token = self.peek()
        if name_token.kind is not TokenKind.NAME:
            raise self.fail("expected a filter name after '|'")
        if name_token.text not in FILTERS:
            raise self.fail(
                f"unknown filter {name_token.text!r}; allowed filters are "
                f"{', '.join(sorted(FILTERS))}"
            )
        self.advance()
        args: list[Literal] = []
        if self.at_op("("):
            self.advance()
            if not self.at_op(")"):
                args.append(self.literal_argument())
                while self.at_op(","):
                    self.advance()
                    args.append(self.literal_argument())
            if not self.at_op(")"):
                raise self.fail("expected ')' to close the filter arguments")
            self.advance()
        spec = FILTERS[name_token.text]
        if not spec.min_args <= len(args) <= spec.max_args:
            raise self.fail(
                f"filter {name_token.text!r} takes between {spec.min_args} and {spec.max_args} "
                f"arguments, got {len(args)}",
                name_token,
            )
        return FilterCall(pos=value.pos, value=value, name=name_token.text, args=tuple(args))

    def literal_argument(self) -> Literal:
        node = self.primary()
        if not isinstance(node, Literal):
            raise self.fail("filter arguments must be literals")
        return node

    def primary(self) -> Expr:
        token = self.peek()
        if token.kind is TokenKind.EOF:
            raise self.fail("expression ends where a value was expected")
        if token.kind is TokenKind.OP and token.text == "(":
            self._descend()
            try:
                self.advance()
                inner = self.or_expr()
                if not self.at_op(")"):
                    raise self.fail("expected ')'")
                self.advance()
                return inner
            finally:
                self.depth -= 1
        if token.kind is TokenKind.NUMBER:
            self.advance()
            # The lexer always decodes NUMBER tokens to an int or a float.
            number: int | float = token.value if isinstance(token.value, int | float) else 0
            return Literal(pos=token.pos, value=number)
        if token.kind is TokenKind.STRING:
            self.advance()
            return Literal(pos=token.pos, value=str(token.value))
        if token.kind is TokenKind.NAME:
            if token.text in NAMED_LITERALS:
                self.advance()
                return Literal(pos=token.pos, value=NAMED_LITERALS[token.text])
            if token.text in ROOTS:
                self.advance()
                return Root(pos=token.pos, name=token.text)
            if token.text in KEYWORDS:
                raise self.fail(f"{token.text!r} is an operator, not a value")
            raise self.fail(
                f"unknown name {token.text!r}; expressions may only start from "
                f"{', '.join(sorted(ROOTS))}"
            )
        raise self.fail("expected a value")
