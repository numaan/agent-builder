"""The Anthropic provider. Implements DESIGN.md section 11.1:

    ``AnthropicProvider`` is the first implementation using the Anthropic Python SDK with
    tool-use for structured output and prompt caching for the static prefix.

Both of those are load-bearing and both are visible in :func:`build_payload`, which is a pure
function precisely so that the request this provider *would* send can be tested without a key:

* **Tool-use for structured output.** The schema becomes one tool. When the node has no
  model-callable tools the tool is forced (``tool_choice`` names it), so the only thing the
  model can do is answer in the shape it was given. When the node *does* have read tools
  (DESIGN.md section 8.4's bounded loop), the structured tool sits alongside them under
  ``tool_choice: auto``, which is exactly the loop's exit condition: "The loop ends when the
  model produces its structured decision."
* **Prompt caching on the static prefix.** Layers 1 to 3 of DESIGN.md section 11.2 are identical
  across turns, so the assembler marks that block ``cache=True`` and this provider puts a
  ``cache_control`` breakpoint on it. Caching is a prefix match: anything before the breakpoint
  that changes between turns silently costs the cache, which is why the assembler puts every
  volatile layer *after* it.

**This class is unexercised.** There is no ``ANTHROPIC_API_KEY`` in the environment this phase
was built in, so nothing here has ever spoken to the API. What is tested is the payload it
builds and the way it maps SDK errors onto DESIGN.md section 7.3's ladder; what is not tested is
whether the real API accepts that payload. reviews/phase-3.md says so in as many words. Two
things a live run should be watched for: models in the Fable and Mythos families reject a forced
``tool_choice`` and would need the structured-outputs parameter instead, and ``strict`` tool
validation requires every object in the schema to close itself, which
:func:`~support_core.llm.schemas.json_schema_for` does.
"""

import os
from typing import Any

from support_core.llm.provider import StructuredByCompletion
from support_core.llm.types import (
    CompletionRequest,
    CompletionResponse,
    LLMError,
    LLMUnavailableError,
    PromptMessage,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolUsePart,
    Usage,
)

API_KEY_ENV = "ANTHROPIC_API_KEY"


def api_key_present() -> bool:
    """Whether a live call could be made at all. The ``live`` test group skips on this."""
    return bool(os.environ.get(API_KEY_ENV))


def build_payload(req: CompletionRequest) -> dict[str, Any]:
    """The keyword arguments for ``messages.create``. Pure, so it is testable without a key."""
    system: list[dict[str, Any]] = []
    for block in req.system:
        entry: dict[str, Any] = {"type": "text", "text": block.text}
        if block.cache:
            entry["cache_control"] = {"type": "ephemeral"}
        system.append(entry)

    tools: list[dict[str, Any]] = [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in req.tools
    ]
    payload: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "messages": [_message(message) for message in req.messages],
    }
    if system:
        payload["system"] = system
    if req.structured is not None:
        tools.append(
            {
                "name": req.structured.name,
                "description": req.structured.description,
                "input_schema": req.structured.json_schema,
                "strict": True,
            }
        )
        # Forced only when there is nothing else the model could legitimately do. With read
        # tools present, forcing the answer tool would remove the loop DESIGN.md section 8.4
        # describes before it could gather anything.
        payload["tool_choice"] = (
            {"type": "auto"} if req.tools else {"type": "tool", "name": req.structured.name}
        )
    if tools:
        payload["tools"] = tools
    return payload


def _message(message: PromptMessage) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for part in message.content:
        match part:
            case TextPart():
                content.append({"type": "text", "text": part.text})
            case ToolUsePart():
                content.append(
                    {"type": "tool_use", "id": part.id, "name": part.name, "input": part.input}
                )
            case ToolResultPart():
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": part.tool_use_id,
                        "content": part.content,
                        "is_error": part.is_error,
                    }
                )
    return {"role": message.role, "content": content}


def parse_message(req: CompletionRequest, message: Any) -> CompletionResponse:
    """Turn an SDK ``Message`` into a :class:`CompletionResponse`.

    Takes ``Any`` rather than the SDK type so a test can pass a stand-in with the same shape;
    the SDK's own model is a Pydantic object with exactly these attributes.
    """
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    structured: dict[str, Any] | None = None
    answer_tool = req.structured.name if req.structured else None
    for block in getattr(message, "content", []):
        kind = getattr(block, "type", None)
        if kind == "text":
            text_parts.append(str(getattr(block, "text", "")))
        elif kind == "tool_use":
            name = str(getattr(block, "name", ""))
            arguments = dict(getattr(block, "input", {}) or {})
            if name == answer_tool:
                structured = arguments
            else:
                calls.append(
                    ToolCall(id=str(getattr(block, "id", "")), name=name, arguments=arguments)
                )
    raw_usage = getattr(message, "usage", None)
    usage = Usage(
        input_tokens=int(getattr(raw_usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(raw_usage, "output_tokens", 0) or 0),
        cache_read_input_tokens=int(getattr(raw_usage, "cache_read_input_tokens", 0) or 0),
        cache_creation_input_tokens=int(getattr(raw_usage, "cache_creation_input_tokens", 0) or 0),
    )
    return CompletionResponse(
        model=str(getattr(message, "model", req.model)),
        text="\n".join(part for part in text_parts if part) or None,
        tool_calls=calls,
        structured=structured,
        stop_reason=getattr(message, "stop_reason", None),
        usage=usage,
    )


class AnthropicProvider(StructuredByCompletion):
    """DESIGN.md section 11.1's first provider."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            import anthropic  # imported lazily: constructing a client needs credentials

            self._client = anthropic.AsyncAnthropic(
                api_key=api_key or os.environ.get(API_KEY_ENV),
                max_retries=max_retries,
                timeout=timeout,
            )

    @property
    def name(self) -> str:
        return "anthropic"

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        payload = build_payload(req)
        try:
            message = await self._client.messages.create(**payload)
        except Exception as exc:  # mapped below; the SDK's classes are imported lazily
            raise _translate(exc) from exc
        return parse_message(req, message)


_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def _translate(exc: Exception) -> LLMError:
    """Map an SDK failure onto DESIGN.md section 7.3's two kinds.

    Retryable (a timeout, a rate limit, a connection failure, a 5xx) becomes
    :class:`LLMUnavailableError`, which the service retries with backoff and then downgrades to
    the escalation model. Anything else - a 400 from a malformed request, a 401 - is a bug or a
    misconfiguration, and retrying it six times would only slow the handoff down.
    """
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if isinstance(exc, LLMError):
        return exc
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"}:
        return LLMUnavailableError(f"{name}: {exc}")
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return LLMUnavailableError(f"{name} ({status}): {exc}")
    if isinstance(status, int):
        return LLMError(f"{name} ({status}): {exc}")
    if name.startswith("API"):
        return LLMUnavailableError(f"{name}: {exc}")
    return LLMError(f"{name}: {exc}")
