"""Providers that answer without a network. Implements the phase 3 backlog line "``FakeProvider``
that replays recorded responses keyed by prompt hash for tests".

Two of them, for two different jobs:

* :class:`FakeProvider` replays a :class:`~support_core.llm.recording.Cassette` by request
  fingerprint and raises :class:`~support_core.llm.recording.CassetteMiss` on anything it has
  not seen. It is the honest stand-in for a real provider: the request has to match to the byte,
  so a test that passes against it is a test that would have sent exactly the recorded prompt.
* :class:`ScriptedProvider` answers from a small ordered rule table. It is what the cassette
  *generator* runs against when there is no API key, and what tests whose subject is engine
  behaviour - a malformed decision, a refused tool, a retry - use directly, because those tests
  need to control the answer rather than replay one.

Neither is used outside tests and the cassette generator; nothing in the engine imports them.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from support_core.llm.provider import StructuredByCompletion
from support_core.llm.recording import Cassette, CassetteMiss
from support_core.llm.types import (
    CompletionRequest,
    CompletionResponse,
    LLMError,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolUsePart,
    Usage,
)


class FakeProvider(StructuredByCompletion):
    """Replay recorded responses, keyed by prompt hash."""

    def __init__(self, cassette: Cassette) -> None:
        self.cassette = cassette
        self.calls: list[CompletionRequest] = []

    @property
    def name(self) -> str:
        return "fake"

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        recorded = self.cassette.get(req)
        if recorded is None:
            raise CassetteMiss(req, self.cassette.path)
        return recorded


@dataclass(frozen=True, slots=True)
class Rule:
    """One scripted answer: what has to appear in the request, and what to answer with."""

    when: str
    """Substring that must appear in the flattened request (see :func:`render_request`). Usually
    a phrase from the node's instructions, so a rule names the node it answers for."""

    respond: Mapping[str, Any] | None = None
    """The structured payload, as the model would have produced it."""

    text: str | None = None
    tool_calls: Sequence[ToolCall] = ()
    purpose: str | None = None
    """Restrict the rule to ``node``, ``slots`` or ``summary`` calls."""

    uses: int | None = None
    """How many times this rule may fire. ``None`` is unlimited; a tool-call rule usually wants
    1, so the loop makes progress instead of asking for the same tool for ever."""

    stop_reason: str | None = None


class ScriptedProvider(StructuredByCompletion):
    """A deterministic stand-in model driven by an ordered rule table."""

    def __init__(self, rules: Sequence[Rule], *, model: str = "scripted-model") -> None:
        self.rules = list(rules)
        self.model = model
        self.fired: dict[int, int] = {}
        self.calls: list[CompletionRequest] = []

    @property
    def name(self) -> str:
        return "scripted"

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        haystack = render_request(req)
        for index, rule in enumerate(self.rules):
            if rule.purpose is not None and rule.purpose != req.purpose:
                continue
            if rule.when not in haystack:
                continue
            if rule.uses is not None and self.fired.get(index, 0) >= rule.uses:
                continue
            self.fired[index] = self.fired.get(index, 0) + 1
            return CompletionResponse(
                model=req.model,
                text=rule.text,
                tool_calls=list(rule.tool_calls),
                structured=dict(rule.respond) if rule.respond is not None else None,
                stop_reason=rule.stop_reason or ("tool_use" if rule.tool_calls else "end_turn"),
                usage=Usage(input_tokens=len(haystack) // 4, output_tokens=32),
            )
        msg = (
            f"no scripted rule matches this {req.purpose!r} request; the rules look for "
            f"{[rule.when for rule in self.rules]}"
        )
        raise LLMError(msg)


@dataclass(slots=True)
class FailingProvider(StructuredByCompletion):
    """Fails a fixed number of times, then delegates. For DESIGN.md section 7.3's ladder."""

    inner: StructuredByCompletion
    failures: int
    error: Exception = field(default_factory=lambda: LLMError("provider is down"))
    seen: int = 0

    @property
    def name(self) -> str:
        return "failing"

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.seen += 1
        if self.seen <= self.failures:
            raise self.error
        return await self.inner.complete(req)


def render_request(req: CompletionRequest) -> str:
    """Flatten a request into the text a :class:`Rule` matches against."""
    parts = [block.text for block in req.system]
    for message in req.messages:
        for item in message.content:
            match item:
                case TextPart():
                    parts.append(item.text)
                case ToolUsePart():
                    parts.append(f"{item.name} {json.dumps(item.input, sort_keys=True)}")
                case ToolResultPart():
                    parts.append(item.content)
    parts.extend(tool.name for tool in req.tools)
    return "\n".join(parts)
