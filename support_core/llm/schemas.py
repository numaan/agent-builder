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

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

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


class ConversationSummary(BaseModel):
    """The rolling summary of DESIGN.md section 10."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="A factual summary of the conversation so far, for the agent's own memory."
    )


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
    """
    fields: dict[str, Any] = {}
    if edges:
        labels = tuple(dict.fromkeys(edges))
        fields["decision"] = (
            Literal[labels],
            Field(description=f"Exactly one of: {', '.join(labels)}."),
        )
    if output_schema:
        built = build_model(
            f"{_camel(node_id)}StateUpdates", dict(output_schema), all_optional=True
        )
        fields["state_updates"] = (
            built.model,
            Field(
                default_factory=built.model,
                description="Values to write into the workflow state.",
            ),
        )
    model = create_model(
        f"{_camel(node_id)}Output",
        __base__=LlmNodeOutput,
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
