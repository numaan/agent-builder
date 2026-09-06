"""How the interrupt check reads a model's answer. DESIGN.md section 6.6 step 2.

The project's first live model run found two defects and BACKLOG.md's decisions log records
them as one family: *GLM spells absent and nested values as text rather than as JSON*. A message
that meant "say nothing" arrived as the four characters ``null``, and a nested object arrived as
a JSON string. Both were the model's spelling of a right answer, and both were read rather than
refused.

The interrupt check is the worst place in the system for a third instance, because its wrong
answers are not "a strange sentence reached the customer": they are *abandon the workflow the
customer is in the middle of* and *drop what they just asked for*. So its answer is normalised
before validation, and this is where that is pinned.

The rule the normalisation follows: read a *spelling* of an answer the model got right, never
guess at an answer it got wrong. Anything unrecognised falls through to ordinary validation and
fails, which the engine treats as ``continue``.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from support_core.llm.schemas import InterruptCheck


@pytest.mark.parametrize(
    ("payload", "kind", "intent"),
    [
        ({"kind": "continue"}, "continue", None),
        ({"kind": "new_intent", "intent": "refund"}, "new_intent", "refund"),
        # Quotes and whitespace around a label: the ordinary noise of a string field.
        ({"kind": ' "cancel" '}, "cancel", None),
        ({"kind": "CONTINUE"}, "continue", None),
        # DESIGN.md section 6.6's own notation. A model shown that vocabulary may well write it,
        # and the intent then has to be recovered from the other field's raw value.
        ({"kind": "new_intent(update_address)"}, "new_intent", "update_address"),
        ({"kind": "new_intent: refund"}, "new_intent", "refund"),
        ({"kind": 'new_intent("refund")'}, "new_intent", "refund"),
        # An intent already given wins over one carried inside `kind`.
        (
            {"kind": "new_intent(refund)", "intent": "update_address"},
            "new_intent",
            "update_address",
        ),
        # The recorded defect, in the field where it would cost the most: an absent intent
        # spelled as text names a workflow called "null", which resolves to nothing and reads as
        # `unclear` - safe, but for the wrong reason, and the request is gone a turn later.
        ({"kind": "new_intent", "intent": "null"}, "new_intent", None),
        ({"kind": "continue", "intent": "None"}, "continue", None),
        ({"kind": "continue", "intent": "  "}, "continue", None),
        ({"kind": "unclear", "intent": '"refund"'}, "unclear", "refund"),
    ],
)
def test_a_models_spelling_of_a_right_answer_is_read(
    payload: dict[str, Any], kind: str, intent: str | None
) -> None:
    reading = InterruptCheck.model_validate(payload)
    assert reading.kind == kind
    assert reading.intent == intent


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "resume"},
        {"kind": "new_topic"},
        {"kind": ""},
        {"kind": None},
        {"kind": ["continue"]},
        {},
        {"kind": "continue", "confidence": 1.5},
        {"kind": "continue", "what": "else"},
    ],
    ids=[
        "a word that is not one of the four",
        "a plausible synonym",
        "empty",
        "null",
        "a list",
        "nothing at all",
        "a confidence outside its range",
        "a field the schema does not declare",
    ],
)
def test_an_answer_that_is_not_one_of_the_four_is_refused(payload: dict[str, Any]) -> None:
    """Not guessed at, and not coerced into the nearest one.

    A refused answer becomes ``continue`` one layer up, in
    :class:`~support_core.llm.wiring.StructuredInterruptCheck` - the suspended node resumes with
    the customer's reply, which is what would have happened with no check at all. That is the one
    failure here that costs nothing.
    """
    with pytest.raises(ValidationError):
        InterruptCheck.model_validate(payload)


def test_confidence_arrives_as_a_number_however_it_was_written() -> None:
    """A float spelled as a string is the same defect family, and pydantic already reads it."""
    assert (
        InterruptCheck.model_validate({"kind": "continue", "confidence": "0.75"}).confidence == 0.75
    )
    assert InterruptCheck.model_validate({"kind": "continue"}).confidence == 0.0
