"""Expression tokenizer. Implements DESIGN.md section 6.4 (the expression language).

Hand-written on purpose: ``tokenize``/``ast`` from the standard library would accept the whole
of Python and then need a blocklist, which is the wrong shape for a sandbox. This lexer accepts
an allowlist of characters and turns everything else into a :class:`ParseError` naming the
offending text and its position.
"""

from dataclasses import dataclass
from enum import StrEnum

MAX_LENGTH = 2000
"""Longest expression source accepted. A graph predicate that needs more is a code smell."""

MAX_TOKENS = 500
"""Most tokens accepted, so a pathological one-line file cannot make the parser do real work."""

_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t", "r": "\r", "0": "\0"}
_PUNCTUATION = {".", "|", "(", ")", ","}
_TWO_CHAR_OPS = {"==", "!=", "<=", ">="}
_ONE_CHAR_OPS = {"<", ">", "-"}
_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789")
_DIGITS = frozenset("0123456789")
_SPACE = frozenset(" \t\r\n")


class ParseError(ValueError):
    """The source is not a valid expression.

    Carries the offending token text and its 0-based character position so a validator finding
    can quote both. Every rejection path in the lexer and the parser raises this and nothing
    else; :func:`support_core.graph.expr.parser.parse` is contractually total.
    """

    def __init__(self, message: str, *, source: str, position: int, token: str = "") -> None:
        self.source = source
        self.position = position
        self.token = token
        where = f"at position {position}"
        what = f" (token {token!r})" if token else ""
        super().__init__(f"{message} {where}{what}: {source!r}")


class TokenKind(StrEnum):
    NAME = "name"
    NUMBER = "number"
    STRING = "string"
    OP = "op"
    EOF = "eof"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str
    pos: int
    value: str | int | float | None = None
    """Decoded payload for STRING and NUMBER tokens; ``None`` for every other kind."""


def tokenize(source: str) -> list[Token]:
    """Split ``source`` into tokens, or raise :class:`ParseError`."""
    if len(source) > MAX_LENGTH:
        raise ParseError(
            f"expression is longer than {MAX_LENGTH} characters",
            source=source[:80],
            position=MAX_LENGTH,
        )
    tokens: list[Token] = []
    i = 0
    n = len(source)
    while i < n:
        char = source[i]
        if char in _SPACE:
            i += 1
            continue
        if len(tokens) >= MAX_TOKENS:
            raise ParseError(
                f"expression has more than {MAX_TOKENS} tokens", source=source, position=i
            )
        if char in _NAME_START:
            start = i
            while i < n and source[i] in _NAME_CHARS:
                i += 1
            text = source[start:i]
            if text.startswith("_"):
                raise ParseError(
                    "names starting with '_' are not addressable",
                    source=source,
                    position=start,
                    token=text,
                )
            tokens.append(Token(TokenKind.NAME, text, start))
            continue
        if char in _DIGITS:
            token, i = _read_number(source, i)
            tokens.append(token)
            continue
        if char in {"'", '"'}:
            token, i = _read_string(source, i)
            tokens.append(token)
            continue
        two = source[i : i + 2]
        if two in _TWO_CHAR_OPS:
            tokens.append(Token(TokenKind.OP, two, i))
            i += 2
            continue
        if char in _ONE_CHAR_OPS or char in _PUNCTUATION:
            tokens.append(Token(TokenKind.OP, char, i))
            i += 1
            continue
        if char in {"=", "!"}:
            raise ParseError(
                f"{char!r} is not an operator (did you mean {'==' if char == '=' else '!='}?)",
                source=source,
                position=i,
                token=char,
            )
        raise ParseError(f"unexpected character {char!r}", source=source, position=i, token=char)
    tokens.append(Token(TokenKind.EOF, "", n))
    return tokens


def _read_number(source: str, i: int) -> tuple[Token, int]:
    start = i
    n = len(source)
    while i < n and source[i] in _DIGITS:
        i += 1
    is_float = False
    if i < n and source[i] == "." and i + 1 < n and source[i + 1] in _DIGITS:
        is_float = True
        i += 1
        while i < n and source[i] in _DIGITS:
            i += 1
    if i < n and source[i] in {"e", "E"}:
        probe = i + 1
        if probe < n and source[probe] in {"+", "-"}:
            probe += 1
        if probe < n and source[probe] in _DIGITS:
            is_float = True
            i = probe
            while i < n and source[i] in _DIGITS:
                i += 1
    text = source[start:i]
    if i < n and source[i] in _NAME_CHARS:
        bad = text + source[i]
        raise ParseError(
            "number is followed by a name character", source=source, position=start, token=bad
        )
    value: int | float = float(text) if is_float else int(text)
    return Token(TokenKind.NUMBER, text, start, value), i


def _read_string(source: str, i: int) -> tuple[Token, int]:
    quote = source[i]
    start = i
    i += 1
    n = len(source)
    out: list[str] = []
    while i < n:
        char = source[i]
        if char == "\\":
            if i + 1 >= n:
                raise ParseError(
                    "string ends with a dangling backslash", source=source, position=i, token="\\"
                )
            escape = source[i + 1]
            if escape not in _ESCAPES:
                raise ParseError(
                    f"unknown escape sequence '\\{escape}'",
                    source=source,
                    position=i,
                    token=f"\\{escape}",
                )
            out.append(_ESCAPES[escape])
            i += 2
            continue
        if char == quote:
            text = source[start : i + 1]
            return Token(TokenKind.STRING, text, start, "".join(out)), i + 1
        if char == "\n":
            raise ParseError(
                "string literal is not closed before the end of the line",
                source=source,
                position=start,
                token=quote,
            )
        out.append(char)
        i += 1
    raise ParseError("string literal is not closed", source=source, position=start, token=quote)
