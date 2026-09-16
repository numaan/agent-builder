"""Form schemas a pack declares on a graph node. Implements DESIGN.md section 12's generative UI.

A node that waits for the customer (an ``ask`` or a ``confirm``) may declare a ``form``: a schema
the client renders instead of asking for a free-text reply. The schema is pure description - what
to render and how to validate it, never a value the engine holds - and it lives on the node, in
the graph file, beside the step it belongs to (see :mod:`support_core.graph.nodes`). When that
node's gate opens the AG-UI transport emits it as a ``render_form`` tool call
(:mod:`support_core.channels.ag_ui`); a client with no form support answers the same gate with
text. This module is a leaf: it imports nothing from the graph package, so both the node model and
the channel layer can depend on it.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FormFieldType = Literal["text", "email", "date", "select", "radio", "checkbox", "checklist"]
"""The input kinds a form may declare. Deliberately small: a schema a browser can render without a
component library, and a set core can reason about (which need options and which validate)."""

CHOICE_FIELDS = {"select", "radio", "checklist"}


class FormOption(BaseModel):
    """One choice in a select, radio group or checklist."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)
    label: str = Field(min_length=1)


class FormCondition(BaseModel):
    """A field is shown, or becomes required, when another field holds ``equals``."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    equals: str


class FormField(BaseModel):
    """One field of a node's form.

    A field is pure description: what to render and how to validate it, never a value the engine
    holds. The constraints here are what let a client render it safely and what let the graph
    loader catch a broken form at startup rather than in a customer's browser.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)
    type: FormFieldType = "text"
    required: bool = False
    hint: str | None = None
    pattern: str | None = None
    """A regular expression the value must match. Compiled at load, so a broken pattern is a
    startup failure rather than a validation that never fires."""
    options: list[FormOption] = Field(default_factory=list)
    show_if: FormCondition | None = None
    required_if: FormCondition | None = None

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                msg = f"field pattern {value!r} is not a valid regular expression: {exc}"
                raise ValueError(msg) from exc
        return value

    @model_validator(mode="after")
    def _choices_have_options(self) -> "FormField":
        if self.type in CHOICE_FIELDS and not self.options:
            msg = f"a {self.type} field ({self.key!r}) needs at least one option"
            raise ValueError(msg)
        if self.type not in CHOICE_FIELDS and self.options:
            msg = f"a {self.type} field ({self.key!r}) cannot have options"
            raise ValueError(msg)
        return self


class FormSection(BaseModel):
    """A titled group of fields."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    fields: list[FormField] = Field(min_length=1)


class FormSchema(BaseModel):
    """A form a node declares, for a client to render (DESIGN.md section 12).

    Attached to an ``ask`` or ``confirm`` node in the graph file; when that node's gate opens, the
    AG-UI transport emits it as a ``render_form`` tool call. Every field key is unique across the
    whole form, so a submission is a flat mapping.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    intro: str | None = None
    submit_label: str = "Submit"
    sections: list[FormSection] = Field(min_length=1)

    @model_validator(mode="after")
    def _keys_are_unique(self) -> "FormSchema":
        keys = [field.key for section in self.sections for field in section.fields]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            msg = f"form field keys must be unique; repeated: {', '.join(duplicates)}"
            raise ValueError(msg)
        return self
