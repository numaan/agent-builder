"""Structured output contracts. Implements DESIGN.md section 11.3 and the constraint principle 2
puts on it: "The LLM chooses among transitions the graph allows ... It never invents a
transition."

The base shape is section 11.3 verbatim. What makes it safe is that an ``llm`` node never uses
the base shape directly: :func:`build_node_output_model` narrows ``decision`` to a
``Literal[...]`` over exactly the node's declared edge labels and re-types ``state_updates`` as
the node's own ``output_schema``. That model is what the provider is asked for *and* what the
answer is validated against, so a provider that ignores a schema, or a model that returns an
edge the graph does not declare, fails validation instead of steering the conversation.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from support_core.graph.types import build_model


class LlmNodeOutput(BaseModel):
    """DESIGN.md section 11.3.

    ``decision`` is ``str`` here and a ``Literal`` in the per-node subclass; this class is the
    documented shape and the fall-back for a node with no declared edges.
    """

    model_config = ConfigDict(extra="forbid")

    message_to_customer: str | None = Field(
        default=None, description="What to say to the customer, or null to say nothing."
    )
    decision: str = Field(description="One of the allowed decision labels, exactly as written.")
    state_updates: dict[str, Any] = Field(
        default_factory=dict, description="Values to write into the workflow state."
    )
    citations: list[str] = Field(
        default_factory=list,
        description="Ids of the knowledge passages supporting any factual claim made.",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="How confident you are in the decision, 0 to 1."
    )
    needs_handoff: bool = Field(
        default=False, description="True to ask for a human; the engine decides whether to."
    )


class SlotExtraction(BaseModel):
    """The base shape for an ``ask`` node's resume (DESIGN.md section 6.2).

    ``slots`` is re-typed per node by :func:`build_slot_model` so each declared slot keeps the
    type the graph gave it.
    """

    model_config = ConfigDict(extra="forbid")

    slots: dict[str, Any] = Field(
        default_factory=dict, description="The values found in the reply. Omit anything not said."
    )
    unfilled: list[str] = Field(default_factory=list, description="Slots the reply did not answer.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ConfirmReading(BaseModel):
    """How a customer's reply to a proposed action reads (DESIGN.md sections 6.2, 8.2).

    Three answers, not two. DESIGN.md 6.2 requires "an explicit yes", so a model that is asked
    for a boolean has to put "hmm, what would it cost me?" somewhere, and both places are wrong:
    ``true`` moves the customer's money on a question, ``false`` throws away what they wanted.
    ``unclear`` is the honest third answer and the ``confirm`` node asks again.
    """

    model_config = ConfigDict(extra="forbid")

    answer: Literal["yes", "no", "unclear"] = Field(
        description=(
            "yes only if the reply is an unambiguous agreement to the exact action proposed; "
            "no if it is a refusal; unclear for anything else, including a question, a "
            "condition, a change of subject, or agreement to something different."
        )
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ConversationSummary(BaseModel):
    """The rolling summary of DESIGN.md section 10."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="A factual summary of the conversation so far, for the agent's own memory."
    )


def _coerce_nested_object(value: Any) -> Any:
    """Accept the two shapes a model may use for a nested object it means to be empty or JSON.

    Structured output is a tool call, and not every model serialises a nested object the same
    way. Some send ``state_updates`` as a JSON *string*; some send ``null`` where they mean "I am
    writing nothing". Both are the model's spelling, not a different answer, so they are read
    rather than refused - and then validated against the node's own closed model exactly as
    before, so nothing about review finding V2's guarantee is relaxed: an undeclared schema still
    builds a model with no fields, and a payload that writes something still fails here.

    Anything else is passed through untouched and fails validation with the normal message.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return value
        return parsed if isinstance(parsed, dict) else value
    return value


def build_node_output_model(
    node_id: str,
    edges: Sequence[str],
    output_schema: Mapping[str, str] | None = None,
) -> type[LlmNodeOutput]:
    """The structured output model for one ``llm`` node.

    ``decision`` becomes ``Literal[<the node's edge labels>]``, which is what stops the model
    inventing a transition: an undeclared label is a validation failure with a message naming
    what was allowed, and the engine treats that as malformed output (DESIGN.md section 11.3),
    never as a decision.

    ``state_updates`` becomes the node's ``output_schema`` as a closed model - and an *empty*
    closed model when the node declares none, so absence of a schema means the model may write
    nothing rather than anything.
    """
    fields: dict[str, Any] = {}
    if edges:
        labels = tuple(dict.fromkeys(edges))
        fields["decision"] = (
            Literal[labels],
            Field(description=f"Exactly one of: {', '.join(labels)}."),
        )
    # Always a *typed* ``state_updates``, even when the node declares no ``output_schema``
    # (review finding V2). ``build_model`` closes the model with ``extra="forbid"``, so an
    # undeclared schema builds a model with no fields at all: the model may then write nothing,
    # and a payload that writes something fails validation here rather than being taken on trust
    # by ``frame.state.update``. The old code left ``state_updates`` as ``dict[str, Any]``
    # whenever the schema was absent, which is how a `chat` node's answer could set the graph's
    # own `outcome` field to an arbitrary string.
    built = build_model(
        f"{_camel(node_id)}StateUpdates", dict(output_schema or {}), all_optional=True
    )
    fields["state_updates"] = (
        built.model,
        Field(
            default_factory=built.model,
            description=(
                "Values to write into the workflow state."
                if output_schema
                else "This step declares no output schema and may write no state; leave it empty."
            ),
        ),
    )
    validators: dict[str, Any] = {
        "_read_state_updates": field_validator("state_updates", mode="before")(
            classmethod(lambda cls, value: _coerce_nested_object(value))
        )
    }
    model = create_model(
        f"{_camel(node_id)}Output",
        __base__=LlmNodeOutput,
        __validators__=validators,
        **fields,
    )
    return cast(type[LlmNodeOutput], model)


def build_slot_model(
    node_id: str, state_model: type[BaseModel], slots: Sequence[str]
) -> type[SlotExtraction]:
    """The structured output model for an ``ask`` node's slot extraction.

    Each slot keeps the type the graph declared for that state field, which is the whole reason
    the extractor needs more than the slot's name: "extracts slots via structured output"
    (DESIGN.md section 6.2) is only as good as the schema it extracts into.
    """
    definitions: dict[str, Any] = {}
    for slot in slots:
        info = state_model.model_fields.get(slot)
        annotation = Any if info is None else info.annotation
        definitions[slot] = (annotation, Field(default=None))
    inner = create_model(
        f"{_camel(node_id)}Slots",
        __config__=ConfigDict(extra="forbid", arbitrary_types_allowed=True),
        **definitions,
    )
    model = create_model(
        f"{_camel(node_id)}Extraction",
        __base__=SlotExtraction,
        slots=(inner, Field(default_factory=inner, description="The values found in the reply.")),
    )
    return cast(type[SlotExtraction], model)


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """A JSON schema a provider can use as a tool's ``input_schema``.

    Pydantic's own schema is already valid JSON Schema; two things are tightened so it can also
    be used with strict tool validation: every object rejects unknown properties, and every
    property is required (optional fields are nullable, which is the same information without
    letting a model silently omit a field it was asked to consider).
    """
    schema = model.model_json_schema(mode="serialization")
    _tighten(schema)
    return schema


def _tighten(node: Any) -> None:
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            node.setdefault("additionalProperties", False)
            node["required"] = sorted(properties)
        for value in node.values():
            _tighten(value)
    elif isinstance(node, list):
        for item in node:
            _tighten(item)


def _camel(node_id: str) -> str:
    return "".join(part.title() for part in node_id.split("_")) or "Node"
