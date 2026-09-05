"""Sandboxed message templates. Implements DESIGN.md section 6.4 and 5.2.

``say``, ``ask`` and ``confirm`` nodes carry Jinja templates (DESIGN.md section 6.4 shows
``I can refund {{ state.charge.amount | money }} for {{ state.charge.description }}``).
Two rules make them safe and checkable:

1. Rendering happens in a :class:`jinja2.sandbox.SandboxedEnvironment` with no globals, with
   ``StrictUndefined``, and with *only* the four expression-language filters
   (:data:`support_core.graph.expr.filters.FILTERS`). A pack author therefore learns one filter
   vocabulary, not two.
2. At load time every template is parsed and its expressions are translated into the
   expression-language AST and type-checked against the graph's state model, so a template that
   reads ``state.chrage`` is a validation finding rather than a blank space in a customer
   message. Constructs the translation cannot express (``for``, ``set``, ``include``, calls,
   subscripts, tests, arithmetic) are rejected: templates are for phrasing, not for logic.

Untrusted text is data (DESIGN.md principle 7): nothing rendered here is ever re-parsed as a
template, so a customer message that contains ``{{ ... }}`` stays inert.
"""

from dataclasses import dataclass
from typing import Any

import jinja2
from jinja2 import nodes as jnodes
from jinja2.sandbox import SandboxedEnvironment

from support_core.graph.expr import syntax
from support_core.graph.expr.filters import FILTERS, FilterError
from support_core.graph.expr.syntax import ROOTS, Expr
from support_core.graph.expr.typecheck import TypeEnv, TypeError_, TypeNote, infer

ALLOWED_STATEMENTS: tuple[type[jnodes.Node], ...] = (
    jnodes.Template,
    jnodes.Output,
    jnodes.TemplateData,
    jnodes.If,
)
"""``{% if %}`` is allowed because a one-line conditional is phrasing. Loops and assignment are
not: they invite logic that belongs in the graph, and they bind names the type checker cannot
resolve to a declared root."""


class TemplateError(ValueError):
    """A template is not renderable, or uses a construct outside the allowed subset."""

    def __init__(self, message: str, *, line: int = 0) -> None:
        self.line = line
        super().__init__(f"{message} (line {line})" if line else message)


@dataclass(frozen=True, slots=True)
class TemplateIssue:
    code: str
    """``syntax_error``, ``unsupported``, ``unknown_root``, ``unknown_filter`` or ``type``."""

    message: str
    line: int
    fatal: bool = True


def make_environment() -> SandboxedEnvironment:
    """The one environment templates are parsed and rendered in."""
    env = SandboxedEnvironment(
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        keep_trailing_newline=False,
    )
    env.globals.clear()
    env.filters.clear()
    env.tests.clear()
    for name, spec in FILTERS.items():
        env.filters[name] = _wrap(spec.call)
    return env


def _wrap(call: Any) -> Any:
    def filter_(*args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, **kwargs)
        except FilterError as exc:
            raise TemplateError(str(exc)) from exc

    return filter_


ENVIRONMENT = make_environment()


def render(source: str, scope: dict[str, Any]) -> str:
    """Render ``source`` with the roots in ``scope``. Raises :class:`TemplateError`."""
    try:
        template = ENVIRONMENT.from_string(source)
        return template.render(**scope)
    except TemplateError:
        raise
    except jinja2.TemplateError as exc:
        raise TemplateError(str(exc)) from exc


def validate(source: str, env: TypeEnv) -> tuple[list[TemplateIssue], list[TypeNote]]:
    """Parse ``source``, reject anything outside the allowed subset, and type-check it.

    Returns ``(issues, notes)``. Issues with ``fatal`` are validator errors; notes become
    warnings (an attribute read through an optional value, for example).
    """
    issues: list[TemplateIssue] = []
    notes: list[TypeNote] = []
    try:
        tree = ENVIRONMENT.parse(source)
    except jinja2.TemplateSyntaxError as exc:
        return [TemplateIssue(code="syntax_error", message=str(exc), line=exc.lineno or 0)], notes

    for node in tree.find_all(jnodes.Node):
        if isinstance(node, jnodes.Stmt | jnodes.Template) and not isinstance(
            node, ALLOWED_STATEMENTS
        ):
            issues.append(
                TemplateIssue(
                    code="unsupported",
                    message=(
                        f"{{% {type(node).__name__.lower()} %}} is not allowed in a message "
                        "template; move the logic into the graph"
                    ),
                    line=node.lineno,
                )
            )

    for expression in _top_level_expressions(tree):
        try:
            converted = convert(expression)
        except TemplateError as exc:
            issues.append(TemplateIssue(code="unsupported", message=str(exc), line=exc.line))
            continue
        try:
            infer(converted, env, notes)
        except TypeError_ as exc:
            issues.append(
                TemplateIssue(code="type", message=str(exc), line=getattr(expression, "lineno", 0))
            )
    return issues, notes


def _top_level_expressions(tree: jnodes.Template) -> list[jnodes.Expr]:
    """The expressions a template evaluates: every ``{{ ... }}`` and every ``{% if ... %}``.

    Nested sub-expressions are reached by :func:`convert` recursing, so they are deliberately
    not listed twice.
    """
    found: list[jnodes.Expr] = []
    for node in tree.find_all((jnodes.Output, jnodes.If)):
        if isinstance(node, jnodes.Output):
            found.extend(c for c in node.nodes if not isinstance(c, jnodes.TemplateData))
        elif isinstance(node, jnodes.If) and isinstance(node.test, jnodes.Expr):
            found.append(node.test)
    return found


def convert(node: jnodes.Node) -> Expr:
    """Translate a Jinja expression into the expression-language AST.

    Anything the language cannot express raises :class:`TemplateError` naming the construct, so
    the allowed subset is defined by this function and nothing else.
    """
    line = getattr(node, "lineno", 0)
    match node:
        case jnodes.Name():
            if node.name not in ROOTS:
                raise TemplateError(
                    f"unknown variable {node.name!r}; templates may only read "
                    f"{', '.join(sorted(ROOTS))}",
                    line=line,
                )
            return syntax.Root(pos=0, name=node.name)
        case jnodes.Getattr():
            return syntax.Attribute(pos=0, value=convert(node.node), name=node.attr)
        case jnodes.Const():
            return syntax.Literal(pos=0, value=_const(node, line))
        case jnodes.Filter():
            return _filter(node, line)
        case jnodes.Not():
            return syntax.Not(pos=0, value=convert(node.node))
        case jnodes.Neg():
            return syntax.Unary(pos=0, op="-", value=convert(node.node))
        case jnodes.And():
            return syntax.BoolOp(pos=0, op="and", values=(convert(node.left), convert(node.right)))
        case jnodes.Or():
            return syntax.BoolOp(pos=0, op="or", values=(convert(node.left), convert(node.right)))
        case jnodes.Compare():
            return _compare(node, line)
        case jnodes.Getitem():
            raise TemplateError("subscripting is not supported; use attribute access", line=line)
        case jnodes.Call():
            raise TemplateError("calls are not supported in templates", line=line)
        case jnodes.Test():
            raise TemplateError(
                "tests ('is defined', 'is none') are not supported; compare with none or use "
                "the 'default' filter",
                line=line,
            )
        case _:
            raise TemplateError(
                f"{type(node).__name__} expressions are not supported in templates", line=line
            )


def _const(node: jnodes.Const, line: int) -> str | int | float | bool | None:
    value = node.value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TemplateError(f"{type(value).__name__} literals are not supported", line=line)


def _filter(node: jnodes.Filter, line: int) -> Expr:
    if node.name not in FILTERS:
        raise TemplateError(
            f"unknown filter {node.name!r}; allowed filters are {', '.join(sorted(FILTERS))}",
            line=line,
        )
    if node.node is None:
        raise TemplateError(f"filter {node.name!r} has no input value", line=line)
    if node.kwargs or node.dyn_args or node.dyn_kwargs:
        raise TemplateError(f"filter {node.name!r} takes positional literals only", line=line)
    args: list[syntax.Literal] = []
    for argument in node.args:
        if not isinstance(argument, jnodes.Const):
            raise TemplateError("filter arguments must be literals", line=line)
        args.append(syntax.Literal(pos=0, value=_const(argument, line)))
    spec = FILTERS[node.name]
    if not spec.min_args <= len(args) <= spec.max_args:
        raise TemplateError(
            f"filter {node.name!r} takes between {spec.min_args} and {spec.max_args} arguments, "
            f"got {len(args)}",
            line=line,
        )
    return syntax.FilterCall(pos=0, value=convert(node.node), name=node.name, args=tuple(args))


JINJA_COMPARE_OPS: dict[str, str] = {
    "eq": "==",
    "ne": "!=",
    "lt": "<",
    "lteq": "<=",
    "gt": ">",
    "gteq": ">=",
}
"""Jinja spells its comparison operators as words; the expression language uses symbols."""


def _compare(node: jnodes.Compare, line: int) -> Expr:
    if len(node.ops) != 1:
        raise TemplateError("chained comparisons are not supported", line=line)
    operand = node.ops[0]
    if operand.op not in JINJA_COMPARE_OPS:
        raise TemplateError(f"comparison operator {operand.op!r} is not supported", line=line)
    return syntax.Compare(
        pos=0,
        op=JINJA_COMPARE_OPS[operand.op],  # type: ignore[arg-type]
        left=convert(node.expr),
        right=convert(operand.expr),
    )
