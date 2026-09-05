"""Type strings in graph files (DESIGN.md section 6.4) and the models built from them."""

from typing import Any, Literal, get_args, get_origin

import pytest

from support_core.graph.types import TypeSpecError, build_model, parse_type


@pytest.mark.parametrize(
    ("source", "optional", "unresolved"),
    [
        ("str", False, set()),
        ("str | None", True, set()),
        ("None", True, set()),
        ("int", False, set()),
        ("float", False, set()),
        ("bool", False, set()),
        ("Any", False, set()),
        ("list[str]", False, set()),
        ("dict[str, int]", False, set()),
        ("list[str] | None", True, set()),
        ("Optional[str]", True, set()),
        ('Literal["a", "b"]', False, set()),
        ("Charge", False, {"Charge"}),
        ("Charge | None", True, {"Charge"}),
    ],
)
def test_parse_type(source: str, optional: bool, unresolved: set[str]) -> None:
    parsed = parse_type(source)
    assert parsed.optional is optional
    assert set(parsed.unresolved) == unresolved


def test_literal_values_are_preserved() -> None:
    parsed = parse_type('Literal["refunded", "denied"]')
    assert get_origin(parsed.annotation) is Literal
    assert get_args(parsed.annotation) == ("refunded", "denied")


@pytest.mark.parametrize(
    "source",
    [
        "",
        "list[str, int]",
        "dict[str]",
        "str[int]",
        "Literal[]",
        "Literal[oops]",
        'Literal["unterminated',
        "str |",
        "str extra",
        "list[",
    ],
)
def test_invalid_type_strings(source: str) -> None:
    with pytest.raises(TypeSpecError):
        parse_type(source)


def test_build_model_makes_a_real_pydantic_model() -> None:
    built = build_model("S", {"charge_id": "str | None", "attempts": "int"})
    assert built.issues == ()
    instance = built.model(charge_id="ch_1", attempts=2)
    assert instance.model_dump() == {"charge_id": "ch_1", "attempts": 2}


def test_state_models_are_constructible_empty() -> None:
    built = build_model("S", {"charge_id": "str | None"}, all_optional=True)
    assert built.model().model_dump() == {"charge_id": None}


def test_unknown_keys_are_rejected() -> None:
    built = build_model("S", {"a": "str | None"})
    with pytest.raises(ValueError, match="Extra inputs"):
        built.model(a=None, b=1)


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"a": "list[str, int]"}, "invalid_type"),
        ({"a": "Charge"}, "unresolved_type"),
        ({"model_a": "str"}, "bad_name"),
        ({"_a": "str"}, "bad_name"),
        ({"class": "str"}, "bad_name"),
        ({"a b": "str"}, "bad_name"),
    ],
)
def test_field_issues(fields: dict[str, str], code: str) -> None:
    built = build_model("S", fields, all_optional=True)
    assert code in {issue.code for issue in built.issues}


def test_a_non_optional_state_field_is_flagged() -> None:
    built = build_model("S", {"a": "str"}, all_optional=True)
    assert {i.code for i in built.issues} == {"not_optional"}
    assert built.model().model_dump() == {"a": None}


def test_unresolved_names_become_any() -> None:
    built = build_model("S", {"charge": "Charge | None"}, all_optional=True)
    annotation = built.model.model_fields["charge"].annotation
    assert Any in get_args(annotation)
