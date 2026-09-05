"""Sandboxed message templates (DESIGN.md sections 5.2 and 6.4)."""

from typing import Any

import pytest
from pydantic import BaseModel

from support_core.graph.context import ConversationContext, CustomerContext
from support_core.graph.expr import model_type_env
from support_core.graph.templates import ENVIRONMENT, TemplateError, render, validate


class Charge(BaseModel):
    amount: float
    description: str


class State(BaseModel):
    charge: Charge | None = None
    name: str | None = None
    attempts: int = 0


def env() -> dict[str, Any]:
    return model_type_env(state=State, ctx=ConversationContext)


def scope(**kwargs: Any) -> dict[str, Any]:
    return {
        "state": State(**kwargs),
        "ctx": ConversationContext(customer=CustomerContext(name="Ada")),
    }


@pytest.mark.parametrize(
    "source",
    [
        "I can refund {{ state.charge.amount | money }} for {{ state.charge.description }}.",
        "Hi {{ ctx.customer.name | default('there') }}",
        "{{ state.name | default('friend') | lower }}",
        "{% if state.attempts > 2 %}Let me get a colleague.{% endif %}",
        "{% if state.name != none %}Hi {{ state.name }}{% endif %}",
        "no interpolation at all",
    ],
)
def test_accepted_templates(source: str) -> None:
    issues, _notes = validate(source, env())
    assert issues == [], [i.message for i in issues]


@pytest.mark.parametrize(
    ("source", "code", "fragment"),
    [
        ("{{ oops", "syntax_error", "unexpected end of template"),
        ("{{ nope }}", "unsupported", "unknown variable 'nope'"),
        ("{% for c in state %}{{ c }}{% endfor %}", "unsupported", "{% for %} is not allowed"),
        ("{% set x = 1 %}", "unsupported", "not allowed"),
        ("{{ state.charge.amount + 1 }}", "unsupported", "Add expressions are not supported"),
        ("{{ state.name.upper() }}", "unsupported", "calls are not supported"),
        ("{{ state.charge['amount'] }}", "unsupported", "subscripting is not supported"),
        ("{{ state.name is defined }}", "unsupported", "tests"),
        ("{{ state.name | upper }}", "unsupported", "unknown filter 'upper'"),
        ("{{ state.chrage }}", "type", "has no field 'chrage'"),
        ("{{ state.attempts | lower }}", "type", "does not accept"),
        ("{{ result.anything }}", "type", "root 'result' is not available"),
        ("{{ state.name | default(state.name) }}", "unsupported", "must be literals"),
    ],
)
def test_rejected_templates(source: str, code: str, fragment: str) -> None:
    issues, _notes = validate(source, env())
    assert issues, source
    assert code in {i.code for i in issues}
    assert any(fragment in i.message for i in issues), [i.message for i in issues]


def test_optional_attribute_is_a_note() -> None:
    _issues, notes = validate("{{ ctx.summary.length }}", env())
    assert "optional_attribute" in {n.code for n in notes}


def test_rendering() -> None:
    values = scope(charge=Charge(amount=1234.5, description="Duplicate charge"))
    out = render(
        "I can refund {{ state.charge.amount | money }} for "
        "{{ state.charge.description | lower }}, {{ ctx.customer.name }}.",
        values,
    )
    assert out == "I can refund 1,234.50 for duplicate charge, Ada."


def test_missing_attributes_raise_rather_than_render_blank() -> None:
    with pytest.raises(TemplateError, match="has no attribute 'missing'"):
        render("{{ state.missing }}", scope())


def test_filter_errors_become_template_errors() -> None:
    with pytest.raises(TemplateError, match="money expects a number"):
        render("{{ state.name | money }}", scope(name="not a number"))


def test_environment_is_sandboxed_and_has_only_the_four_filters() -> None:
    assert set(ENVIRONMENT.filters) == {"money", "lower", "len", "default"}
    assert ENVIRONMENT.globals == {}
    assert ENVIRONMENT.tests == {}


@pytest.mark.parametrize(
    "source",
    [
        "{{ state.__class__ }}",
        "{{ ''.__class__.__mro__ }}",
        "{{ self.__init__ }}",
        "{{ cycler }}",
        "{{ range(10) }}",
        "{{ lipsum() }}",
    ],
)
def test_escape_attempts_are_rejected_at_load_time(source: str) -> None:
    issues, _notes = validate(source, env())
    assert issues, source


def test_customer_text_containing_template_syntax_is_inert() -> None:
    """DESIGN.md principle 7: untrusted text is data. A rendered value is never re-rendered."""
    out = render("Customer said: {{ state.name }}", scope(name="{{ ctx.customer.email }}"))
    assert out == "Customer said: {{ ctx.customer.email }}"


@pytest.mark.parametrize(
    "source",
    [
        "{% include 'other' %}",
        "{{ state.count ** 99999 }}",
        "{% import 'x' as y %}",
        "{% extends 'base' %}",
    ],
)
def test_render_only_ever_raises_template_error(source: str) -> None:
    """Phase-1 review nit N1: Jinja leaks TypeError and ValueError out of these.

    Unreachable from a validated pack - ``validate`` rejects all four - but DESIGN.md 7.3 wants
    a node failure rather than a crash, and phase 2's hot reload may render before validating.
    """
    assert validate(source, env())[0], source
    with pytest.raises(TemplateError):
        render(source, scope())
