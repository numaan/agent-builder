"""Wire types for the LLM layer. Implements DESIGN.md section 11.1's request and response
shapes and the errors section 7.3's failure ladder routes on.

Everything here is provider-neutral: :class:`CompletionRequest` is what
:class:`~support_core.llm.provider.LLMProvider` implementations translate into their own SDK's
call, and :class:`CompletionResponse` is what they translate back. Two consequences the rest of
the phase leans on:

* **A request is canonical and hashable.** :meth:`CompletionRequest.fingerprint` is a SHA-256
  over the whole request in a stable JSON form. The fake provider replays by it, the trace
  records it (DESIGN.md section 15 lists "prompt hash" among the span attributes), and a change
  anywhere in prompt assembly changes it, which is what makes a stale recorded fixture a loud
  failure instead of a silent one.
* **Nothing here knows about layers or packs.** Prompt assembly (section 11.2) produces these
  types; it is not built on top of them.
"""

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "assistant"]


class LLMError(Exception):
    """Anything the LLM layer could not do."""


class LLMUnavailableError(LLMError):
    """The provider could not be reached, or kept failing.

    DESIGN.md section 7.3: "LLM call failure: retry with backoff, then fall back to
    ``escalation_model``, then handoff with reason ``llm_unavailable``." This is what reaches
    the engine once all three rungs of that ladder are exhausted.
    """


class StructuredOutputError(LLMError):
    """The model answered, but not with something the node's schema accepts.

    Distinct from :class:`LLMUnavailableError` on purpose: a provider that is down is retried
    with backoff and then downgraded to the escalation model, while a model that returns an
    undeclared edge or a malformed payload is retried *once* and then handed off (DESIGN.md
    sections 11.3 and 7.3). Confusing the two would retry a deterministic failure six times.
    """


class Usage(BaseModel):
    """Token counts for one call, for the cost metrics of DESIGN.md section 15."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    """Tokens served from the prompt cache. Zero across repeated turns means the static prefix
    of DESIGN.md section 11.2 is not stable and something is invalidating it."""

    cache_creation_input_tokens: int = 0


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str


class ToolUsePart(BaseModel):
    """A tool call the model asked for (DESIGN.md section 8.4)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultPart(BaseModel):
    """What the runtime fed back. ``content`` is untrusted data and is fenced before it gets
    here (DESIGN.md section 8.3: "Tool outputs are treated as untrusted data")."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


Part = Annotated[TextPart | ToolUsePart | ToolResultPart, Field(discriminator="type")]


class PromptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: list[Part]


class SystemBlock(BaseModel):
    """One block of the system prompt.

    ``cache`` marks the end of the static prefix - DESIGN.md section 11.2: "Layers 1 to 3 are
    identical across turns and are cached." A provider that supports prompt caching puts its
    breakpoint on the last block carrying it; one that does not ignores the flag.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    cache: bool = False


class ModelTool(BaseModel):
    """A tool as the *model* sees it (DESIGN.md section 8.1: ``description`` is "shown to the
    model"). The runtime's own tool object, with its risk tier and its callable, is phase 4."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any]


class StructuredSpec(BaseModel):
    """The schema a structured call must answer with (DESIGN.md sections 11.1, 11.3).

    ``AnthropicProvider`` turns this into a single forced tool, which is what DESIGN.md 11.1
    means by "using the Anthropic Python SDK with tool-use for structured output".
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    json_schema: dict[str, Any]


class CompletionRequest(BaseModel):
    """One call to a model. Providers translate; they do not add."""

    model_config = ConfigDict(extra="forbid")

    model: str
    system: list[SystemBlock] = Field(default_factory=list)
    messages: list[PromptMessage] = Field(default_factory=list)
    tools: list[ModelTool] = Field(default_factory=list)
    structured: StructuredSpec | None = None
    max_tokens: int = 4096
    purpose: str = "node"
    """What this call is for (``node``, ``slots``, ``summary``). Carried into the trace and the
    cassette so a recorded interaction is readable; part of the fingerprint, so two calls that
    happen to assemble the same text but mean different things do not share a recording."""

    def canonical(self) -> dict[str, Any]:
        """The request in the exact form the fingerprint is taken over."""
        return {
            "model": self.model,
            "purpose": self.purpose,
            "max_tokens": self.max_tokens,
            "system": [block.model_dump(mode="json") for block in self.system],
            "messages": [message.model_dump(mode="json") for message in self.messages],
            "tools": [tool.model_dump(mode="json") for tool in self.tools],
            "structured": self.structured.model_dump(mode="json") if self.structured else None,
        }

    def fingerprint(self) -> str:
        """SHA-256 over :meth:`canonical`, with sorted keys so it is stable across processes."""
        payload = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ToolCall(BaseModel):
    """A tool call lifted out of a response, for the bounded loop of DESIGN.md section 8.4."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class CompletionResponse(BaseModel):
    """What a provider answered."""

    model_config = ConfigDict(extra="forbid")

    model: str
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    structured: dict[str, Any] | None = None
    """The arguments of the forced structured tool, unvalidated. The caller validates it against
    the node's model, because a provider that ignores a schema must not get a free pass."""

    stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)

    def assistant_message(self) -> PromptMessage:
        """This response as a message to append to the next request in a tool loop."""
        content: list[Part] = []
        if self.text:
            content.append(TextPart(text=self.text))
        content.extend(
            ToolUsePart(id=call.id, name=call.name, input=call.arguments)
            for call in self.tool_calls
        )
        if not content:  # a model that said nothing at all still needs a well-formed turn
            content.append(TextPart(text=""))
        return PromptMessage(role="assistant", content=content)
