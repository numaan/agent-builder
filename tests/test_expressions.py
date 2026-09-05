"""The sandboxed expression language (DESIGN.md section 6.4).

The parser's contract is total: for *any* string it returns an AST or raises ``ParseError``.
The property tests at the bottom are the ones that matter; the examples above them pin the
behaviour a pack author sees.
"""

import string
import sys
from typing import Any, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from support_core.graph.expr import (
    MAX_DEPTH,
    MAX_LENGTH,
    MAX_TOKENS,
    EvaluationError,
    ParseError,
    TypeError_,
    evaluate,
    infer,
    model_type_env,
    parse,
    roots_used,
)
from support_core.graph.expr.syntax import unparse, walk
from support_core.graph.expr.typecheck import TypeNote


class Charge(BaseModel):
    id: str
    amount: float
    description: str
    tags: list[str] = []


class Customer(BaseModel):
    identity_verified: bool = False
    name: str | None = None


class Ctx(BaseModel):
    customer: Customer = Customer()
    channel: Literal["web_chat", "email"] = "web_chat"


class State(BaseModel):
    charge_id: str | None = None
    charge: Charge | None = None
    eligible: bool | None = None
    attempts: int = 0
    outcome: Literal["refunded", "denied", "escalated", "abandoned"] | None = None


def env() -> dict[str, Any]:
    return model_type_env(state=State, ctx=Ctx)


def scope(**state_kwargs: Any) -> dict[str, Any]:
    return {"state": State(**state_kwargs), "ctx": Ctx(customer=Customer(identity_verified=True))}


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "canonical"),
    [
        ("state.eligible", "state.eligible"),
        ("state . eligible", "state.eligible"),
        ("ctx.customer.identity_verified", "ctx.customer.identity_verified"),
        ("state.eligible == true", "(state.eligible == True)"),
        ("state.eligible == True", "(state.eligible == True)"),
        ("state.charge_id != none", "(state.charge_id != None)"),
        ("state.charge_id != null", "(state.charge_id != None)"),
        ("not state.eligible", "not state.eligible"),
        ("state.attempts > 3", "(state.attempts > 3)"),
        ("state.attempts >= -1", "(state.attempts >= -1)"),
        ("state.charge.amount | money", "state.charge.amount | money"),
        ("state.charge_id | default('x')", "state.charge_id | default('x')"),
        ("state.charge.tags | len == 0", "(state.charge.tags | len == 0)"),
        (
            "state.eligible == true and state.attempts < 2",
            "((state.eligible == True) and (state.attempts < 2))",
        ),
        ("(state.eligible or state.attempts > 0)", "(state.eligible or (state.attempts > 0))"),
        ("'a' == \"a\"", "('a' == 'a')"),
        ("1.5e2 > 1", "(150.0 > 1)"),
    ],
)
def test_accepted_expressions(source: str, canonical: str) -> None:
    assert unparse(parse(source)) == canonical


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("", "expression is empty"),
        ("   ", "expression is empty"),
        ("foo.bar", "unknown name 'foo'"),
        ("__import__('os')", "names starting with '_'"),
        ("state.__class__", "names starting with '_'"),
        ("state.charge.amount + 1", "unexpected character '+'"),
        ("state.charge_id == 'x' ; drop table", "unexpected character ';'"),
        ("state.eligible = true", "'=' is not an operator"),
        ("1 < 2 < 3", "chained comparisons"),
        ("state.upper()", "calls are not supported"),
        ("state.charge_id | upper", "unknown filter 'upper'"),
        ("state.charge_id | default", "takes between 1 and 1 arguments"),
        ("state.charge_id | default(state.charge_id)", "filter arguments must be literals"),
        ("state.charge_id | len(1)", "takes between 0 and 0 arguments"),
        ("state.", "expected an attribute name after '.'"),
        ("state.and", "reserved word"),
        ("(state.eligible", "expected ')'"),
        ("state.eligible)", "unexpected trailing input"),
        ("and state.eligible", "'and' is an operator, not a value"),
        ("'unterminated", "string literal is not closed"),
        ("'bad\\qescape'", "unknown escape sequence"),
        ("1abc", "number is followed by a name character"),
        ("state[0]", "unexpected character '['"),
        ("{'a': 1}", "unexpected character '{'"),
        ("lambda: 1", "unexpected character ':'"),
        ("state.charge_id if true else none", "unexpected trailing input"),
    ],
)
def test_rejected_expressions(source: str, fragment: str) -> None:
    with pytest.raises(ParseError) as exc:
        parse(source)
    assert fragment in str(exc.value)
    assert exc.value.position >= 0


def test_parse_error_names_the_token_and_position() -> None:
    with pytest.raises(ParseError) as exc:
        parse("state.eligible == $")
    assert exc.value.token == "$"
    assert exc.value.position == 18


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("(" * (MAX_DEPTH + 2) + "1" + ")" * (MAX_DEPTH + 2), "nests deeper"),
        ("not " * (MAX_DEPTH + 2) + "true", "nests deeper"),
        ("-" * (MAX_DEPTH + 2) + "1", "nests deeper"),
        ("state" + ".a" * MAX_TOKENS, "more than"),
        ("state" + ".a" * (MAX_DEPTH + 2), "nests deeper"),
        ("state" + " | len" * (MAX_DEPTH + 2), "nests deeper"),
        ("state.a" + " | len" * (MAX_DEPTH + 2), "nests deeper"),
        ("x" * (MAX_LENGTH + 1), "longer than"),
    ],
)
def test_pathological_input_is_a_parse_error_not_a_recursion_error(
    source: str, fragment: str
) -> None:
    with pytest.raises(ParseError) as exc:
        parse(source)
    assert fragment in str(exc.value)


def _stack_depth() -> int:
    depth = 0
    frame: Any = sys._getframe()
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


def test_the_deepest_accepted_expression_walks_within_a_small_stack_budget() -> None:
    """Phase-1 review F5: attribute and filter chains used to escape ``MAX_DEPTH`` entirely.

    They were bounded only by ``MAX_TOKENS``, so the deepest accepted chain cost about 750
    Python frames per walker and ``infer`` raised ``RecursionError`` from a moderately deep
    caller stack. Every AST level is now counted, so the whole language fits in a budget that
    does not depend on how deep the caller already is.
    """
    longest = "state"
    while True:
        candidate = longest + ".a"
        try:
            parse(candidate)
        except ParseError:
            break
        longest = candidate
    assert longest.count(".a") < MAX_DEPTH

    class Deep(BaseModel):
        a: Any = None

    links = longest.count(".a")
    nested: Any = None
    for _ in range(links):
        nested = Deep(a=nested)

    budget = 120
    original = sys.getrecursionlimit()
    sys.setrecursionlimit(_stack_depth() + budget)
    try:
        tree = parse(longest)
        assert unparse(tree) == longest
        assert len(list(walk(tree))) == links + 1
        assert infer(tree, model_type_env(state=Deep)).describe() == "unknown"
        assert evaluate(tree, {"state": nested}) is None
    finally:
        sys.setrecursionlimit(original)


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("1e400", "too large"),
        ("1e311", "too large"),
        ("-1e400", "too large"),
        ("1.5e999999", "too large"),
        ("'\x07'", "control character"),
        ("'\x00'", "control character"),
        ("'\x7f'", "control character"),
        ("'\x1b['", "control character"),
    ],
)
def test_literals_that_could_not_round_trip_are_rejected(source: str, fragment: str) -> None:
    """Phase-1 review F6: ``unparse`` has to be a canonical form, or 8.2's argument comparison
    silently equates two different texts."""
    with pytest.raises(ParseError) as exc:
        parse(source)
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    "source",
    ["'\\n'", "'\\t'", "'\\r'", "'\\0'", "'\\\\'", "'it\\'s'", "'é中'", "1e308", "'x' == '\\t'"],
)
def test_escapes_and_extremes_round_trip_through_unparse(source: str) -> None:
    once = unparse(parse(source))
    assert unparse(parse(once)) == once


def test_roots_used() -> None:
    assert roots_used(parse("state.a == ctx.customer.name")) == {"state", "ctx"}
    assert roots_used(parse("1 == 1")) == set()


# --------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("state.attempts", 2),
        ("state.attempts > 1", True),
        ("state.attempts > 1 and state.eligible", True),
        ("state.eligible == true", True),
        ("not state.eligible", False),
        ("state.charge_id != none", True),
        ("state.charge.amount | money", "1,234.50"),
        ("state.charge.description | lower", "duplicate charge"),
        ("state.charge.tags | len", 2),
        ("ctx.customer.identity_verified", True),
        ("ctx.customer.name | default('there')", "there"),
        ("state.outcome | default('pending')", "pending"),
        ("ctx.channel == 'web_chat'", True),
    ],
)
def test_evaluation(source: str, expected: Any) -> None:
    values = scope(
        attempts=2,
        eligible=True,
        charge_id="ch_1",
        charge=Charge(
            id="ch_1", amount=1234.5, description="Duplicate charge", tags=["dup", "card"]
        ),
    )
    assert evaluate(parse(source), values) == expected


def test_evaluation_short_circuits_before_a_none_attribute() -> None:
    values = scope(charge=None)
    assert evaluate(parse("state.charge != none and state.charge.amount > 0"), values) is False


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("state.charge.amount", "the value before it is None"),
        ("state.charge_id | money", "money expects a number"),
        ("state.attempts | lower", "lower expects a string"),
        ("state.attempts | len", "len expects a string or a collection"),
        ("state.charge_id < 3", "ordered comparisons need"),
        ("-state.charge_id", "cannot negate"),
    ],
)
def test_evaluation_errors(source: str, fragment: str) -> None:
    with pytest.raises(EvaluationError) as exc:
        evaluate(parse(source), scope(attempts=1, charge_id="ch_1"))
    assert fragment in str(exc.value)


def test_result_root_is_absent_unless_supplied() -> None:
    with pytest.raises(EvaluationError, match="not available"):
        evaluate(parse("result.eligible"), scope())


def test_evaluation_cannot_reach_methods_or_dunders() -> None:
    """The parser blocks ``_`` names; this pins that plain methods are unreachable too."""
    with pytest.raises(EvaluationError, match="has no field 'model_dump'"):
        evaluate(parse("state.model_dump"), scope())


# --------------------------------------------------------------------------------------
# Static type checking
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "described"),
    [
        ("state.charge_id", "str | None"),
        ("state.attempts", "int"),
        ("state.attempts > 1", "bool"),
        ("state.charge.amount | money", "str"),
        ("state.charge_id | default('x')", "str"),
        ("state.charge.tags | len", "int"),
        ("not state.eligible", "bool"),
        ("ctx.customer.identity_verified", "bool"),
        ("state.outcome == 'refunded'", "bool"),
    ],
)
def test_inferred_types(source: str, described: str) -> None:
    assert infer(parse(source), env()).describe() == described


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("state.elegible", "has no field 'elegible'"),
        ("state.eligible == 'true'", "cannot compare"),
        ("state.attempts == 'two'", "cannot compare"),
        ("state.charge_id > 3", "ordered comparisons need"),
        ("state.attempts | lower", "does not accept"),
        ("state.attempts | len", "does not accept"),
        ("state.charge_id.length", "only declared models have fields"),
        ("result.anything", "root 'result' is not available"),
        ("state.attempts == none", "never None"),
        ("state.outcome == 'refunded_maybe'", "not one of the declared values"),
        ("state.charge_id | default(3)", "fallback is int"),
        ("-state.charge_id", "cannot negate"),
    ],
)
def test_type_errors(source: str, fragment: str) -> None:
    with pytest.raises(TypeError_) as exc:
        infer(parse(source), env())
    assert fragment in str(exc.value)


def test_optional_attribute_is_a_note_not_an_error() -> None:
    notes: list[TypeNote] = []
    assert infer(parse("state.charge.amount"), env(), notes).describe() == "float"
    assert [n.code for n in notes] == ["optional_attribute"]


def test_optional_filter_input_is_a_note() -> None:
    notes: list[TypeNote] = []
    infer(parse("state.charge_id | lower"), env(), notes)
    assert [n.code for n in notes] == ["optional_filter_input"]


def test_the_checker_and_the_evaluator_agree_about_mapping_traversal() -> None:
    """Phase-1 review F8: the checker refused what the evaluator supports.

    ``ctx.customer.attributes`` is the CRM record of DESIGN.md section 10, a ``dict[str, Any]``.
    Refusing it at load time made every such expression an error and the evaluator's mapping
    arm unreachable.
    """

    class Attrs(BaseModel):
        plan: dict[str, str] = {}
        raw: dict[str, Any] = {}
        counted: dict[int, str] = {}

    e = model_type_env(state=Attrs)
    assert infer(parse("state.plan.tier"), e).describe() == "str"
    assert infer(parse("state.raw.anything"), e).describe() == "unknown"
    assert evaluate(parse("state.plan.tier"), {"state": Attrs(plan={"tier": "pro"})}) == "pro"

    with pytest.raises(TypeError_):
        infer(parse("state.counted.nope"), e)  # a non-str key can never be read this way
    with pytest.raises(EvaluationError):
        evaluate(parse("state.plan.tier"), {"state": Attrs()})  # the key is simply absent
    with pytest.raises(EvaluationError):
        evaluate(parse("state.plan.tier.oops"), {"state": Attrs(plan={"tier": "pro"})})


def test_unknown_annotations_allow_everything() -> None:
    class Opaque(BaseModel):
        payload: Any = None

    e = model_type_env(state=Opaque)
    assert infer(parse("state.payload.anything.at.all"), e).describe() == "unknown"
    assert infer(parse("state.payload.anything | money"), e).describe() == "str"


# --------------------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------------------

_ALPHABET = string.printable + "é中 "

_QUOTED = st.text(st.characters(), max_size=12).map(lambda s: "'" + s.replace("'", "") + "'")
"""A quoted literal over the *whole* character space, control characters included.

The original generator drew from ``string.printable`` only, so it essentially never produced a
control character inside quotes and could not falsify the round-trip property (F6)."""

_NUMERIC = st.builds(
    lambda mantissa, exponent: f"{mantissa}e{exponent}",
    st.integers(min_value=0, max_value=99),
    st.integers(min_value=-400, max_value=400),
)
"""Exponent forms, including the ones that used to overflow to ``inf`` (F6)."""

_SOURCE = st.one_of(
    _QUOTED,
    _NUMERIC,
    st.text(alphabet=_ALPHABET, max_size=60),
    st.text(alphabet="state.ctx result | ()'\"=!<>-_ and or not 019.", max_size=60),
    st.lists(
        st.sampled_from(
            [
                "state",
                "ctx",
                "result",
                ".",
                "amount",
                "|",
                "money",
                "len",
                "default",
                "(",
                ")",
                ",",
                "'x'",
                "1",
                "==",
                "!=",
                "<",
                ">=",
                "and",
                "or",
                "not",
                "true",
                "none",
                " ",
            ]
        ),
        max_size=25,
    ).map("".join),
)


@settings(max_examples=600, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(_SOURCE)
def test_parse_never_raises_anything_but_parse_error(source: str) -> None:
    """The one property that matters: no input string escapes as an unexpected exception."""
    try:
        parse(source)
    except ParseError:
        return
    except Exception as exc:  # pragma: no cover - this failing is the point of the test
        raise AssertionError(f"{source!r} raised {type(exc).__name__}: {exc}") from exc


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(_SOURCE)
def test_parsed_expressions_round_trip_through_unparse(source: str) -> None:
    """Anything that parses re-parses to the same shape, so ``unparse`` is a canonical form.

    Positions differ (``unparse`` adds parentheses), so the comparison is on the rendered text,
    which is what the validator compares when it matches a confirm's arguments against a tool
    node's (DESIGN.md section 8.2).
    """
    try:
        tree = parse(source)
    except ParseError:
        return
    once = unparse(tree)
    assert unparse(parse(once)) == once


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(_SOURCE)
def test_typecheck_never_raises_anything_but_type_error(source: str) -> None:
    try:
        tree = parse(source)
    except ParseError:
        return
    try:
        infer(tree, env())
    except TypeError_:
        return
    except Exception as exc:  # pragma: no cover - this failing is the point of the test
        raise AssertionError(f"{source!r} raised {type(exc).__name__}: {exc}") from exc


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(_SOURCE)
def test_evaluation_never_raises_anything_but_evaluation_error(source: str) -> None:
    """A type-checked expression evaluates or fails cleanly; it never crashes the engine."""
    try:
        tree = parse(source)
        infer(tree, env())
    except (ParseError, TypeError_):
        return
    try:
        evaluate(tree, scope(charge=Charge(id="c", amount=1.0, description="d")))
    except EvaluationError:
        return
    except Exception as exc:  # pragma: no cover - this failing is the point of the test
        raise AssertionError(f"{source!r} raised {type(exc).__name__}: {exc}") from exc
