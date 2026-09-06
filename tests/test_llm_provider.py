"""The provider abstraction, the Anthropic request it builds, and the recorded-response fakes.
DESIGN.md section 11.1; PLAN.md's "recorded-response fake provider ... live calls only in a
marked ``live`` test group".

The AnthropicProvider cannot be exercised here - there is no ``ANTHROPIC_API_KEY`` in this
environment - so what is tested is everything about it that does not need one: the payload it
builds (tool-use for structured output, a cache breakpoint on the static prefix), the response
it parses, and the way it maps SDK failures onto DESIGN.md section 7.3's two kinds. The one test
that would call the API is in the opt-in ``live`` group and skips cleanly without a key.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from support_core.llm.anthropic_provider import (
    DEFAULT_MIN_CACHEABLE_TOKENS,
    AnthropicProvider,
    _translate,
    api_key_present,
    build_payload,
    min_cacheable_tokens,
    parse_message,
)
from support_core.llm.fake import FakeProvider, Rule, ScriptedProvider
from support_core.llm.prompt import PromptInputs, assemble
from support_core.llm.recording import Cassette, CassetteMiss, RecordingProvider
from support_core.llm.schemas import LlmNodeOutput, json_schema_for
from support_core.llm.types import (
    CompletionRequest,
    LLMError,
    LLMUnavailableError,
    ModelTool,
    PromptMessage,
    StructuredOutputError,
    StructuredSpec,
    SystemBlock,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolUsePart,
)


class Answer(BaseModel):
    """Answer the question."""

    model_config = ConfigDict(extra="forbid")

    verdict: str


def a_request(**overrides: Any) -> CompletionRequest:
    base: dict[str, Any] = {
        "model": "claude-sonnet-5",
        "system": [SystemBlock(text="static prefix", cache=True), SystemBlock(text="volatile")],
        "messages": [PromptMessage(role="user", content=[TextPart(text="hello")])],
        "structured": StructuredSpec(
            name="decide", description="Answer.", json_schema=json_schema_for(Answer)
        ),
    }
    base.update(overrides)
    return CompletionRequest(**base)


# -- the request the AnthropicProvider would send -------------------------------------------


def test_structured_output_is_a_tool_and_the_tool_is_forced_when_there_is_no_loop() -> None:
    """DESIGN.md section 11.1: "using the Anthropic Python SDK with tool-use for structured
    output"."""
    payload = build_payload(a_request())
    assert [tool["name"] for tool in payload["tools"]] == ["decide"]
    assert payload["tools"][0]["strict"] is True
    assert payload["tools"][0]["input_schema"]["properties"]["verdict"]["type"] == "string"
    assert payload["tool_choice"] == {"type": "tool", "name": "decide"}


def test_with_read_tools_the_answer_tool_is_offered_rather_than_forced() -> None:
    """DESIGN.md section 8.4: the loop "ends when the model produces its structured decision",
    which it cannot do if the decision tool is the only thing it is allowed to call."""
    payload = build_payload(
        a_request(
            tools=[
                ModelTool(
                    name="get_balance", description="Balance.", input_schema={"type": "object"}
                )
            ]
        )
    )
    assert [tool["name"] for tool in payload["tools"]] == ["get_balance", "decide"]
    assert payload["tool_choice"] == {"type": "auto"}


def test_the_static_prefix_carries_the_cache_breakpoint_when_it_is_long_enough() -> None:
    """DESIGN.md section 11.1: "prompt caching for the static prefix"; 11.2 says which layers.

    Review finding V3: a breakpoint on a prefix shorter than the model's minimum is accepted,
    ignored, and reported nowhere, so the phase shipped code that looked like it cached and did
    not. The breakpoint is now conditional on the measured prefix.
    """
    long_prefix = "word " * 900  # comfortably over the 1024-token Sonnet minimum
    payload = build_payload(a_request(system=[SystemBlock(text=long_prefix, cache=True)]))
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_a_prefix_below_the_models_minimum_is_not_marked_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shipped pack's own case: 676 estimated tokens against a 1024-token minimum."""
    # The migration harness runs alembic's ``fileConfig``, which disables every logger that
    # already exists, so a test that runs after it sees nothing unless the logger is revived.
    logger = logging.getLogger("support_core.llm.anthropic_provider")
    logger.disabled = False
    logger.propagate = True
    with caplog.at_level(logging.INFO, logger=logger.name):
        payload = build_payload(a_request())
    assert "cache_control" not in payload["system"][0]
    assert "cache_control" not in payload["system"][1]
    assert "prompt cache breakpoint skipped" in caplog.text


def test_the_minimum_cacheable_prefix_is_model_aware() -> None:
    """``claude-opus-5`` caches from 512 tokens, so the same prefix is worth marking there."""
    prefix = "word " * 500  # about 625 estimated tokens: under Sonnet's floor, over Opus's
    sonnet = build_payload(
        a_request(model="claude-sonnet-5", system=[SystemBlock(text=prefix, cache=True)])
    )
    opus = build_payload(
        a_request(model="claude-opus-5", system=[SystemBlock(text=prefix, cache=True)])
    )
    assert "cache_control" not in sonnet["system"][0]
    assert opus["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert min_cacheable_tokens("claude-haiku-4-5") == 2048
    assert min_cacheable_tokens("some-unknown-model") == DEFAULT_MIN_CACHEABLE_TOKENS


def test_tool_use_and_tool_results_round_trip_into_the_payload() -> None:
    request = a_request(
        messages=[
            PromptMessage(role="user", content=[TextPart(text="hello")]),
            PromptMessage(
                role="assistant",
                content=[ToolUsePart(id="tu_1", name="get_balance", input={"ref": "c1"})],
            ),
            PromptMessage(
                role="user",
                content=[ToolResultPart(tool_use_id="tu_1", content="12.50", is_error=False)],
            ),
        ]
    )
    payload = build_payload(request)
    assert payload["messages"][1]["content"][0]["type"] == "tool_use"
    assert payload["messages"][2]["content"][0]["tool_use_id"] == "tu_1"


class _Block:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _Message:
    def __init__(self, content: list[Any], **kwargs: Any) -> None:
        self.content = content
        self.model = "claude-sonnet-5"
        self.stop_reason = "tool_use"
        self.usage = _Block(
            input_tokens=10,
            output_tokens=4,
            cache_read_input_tokens=7,
            cache_creation_input_tokens=0,
        )
        self.__dict__.update(kwargs)


def test_parse_message_separates_the_answer_from_the_tool_calls() -> None:
    request = a_request(
        tools=[ModelTool(name="get_balance", description="b", input_schema={"type": "object"})]
    )
    message = _Message(
        [
            _Block(type="text", text="thinking out loud"),
            _Block(type="tool_use", id="tu_1", name="get_balance", input={"ref": "c1"}),
            _Block(type="tool_use", id="tu_2", name="decide", input={"verdict": "yes"}),
        ]
    )
    response = parse_message(request, message)
    assert response.structured == {"verdict": "yes"}
    assert [call.name for call in response.tool_calls] == ["get_balance"]
    assert response.text == "thinking out loud"
    assert response.usage.cache_read_input_tokens == 7


@pytest.mark.parametrize(
    "exc,expected",
    [
        (type("APIConnectionError", (Exception,), {})("down"), LLMUnavailableError),
        (type("RateLimitError", (Exception,), {})("slow down"), LLMUnavailableError),
        (type("APIStatusError", (Exception,), {"status_code": 503})("busy"), LLMUnavailableError),
        (type("BadRequestError", (Exception,), {"status_code": 400})("bad"), LLMError),
        (ValueError("something else"), LLMError),
    ],
)
def test_sdk_failures_map_onto_the_two_kinds_design_7_3_distinguishes(
    exc: Exception, expected: type[Exception]
) -> None:
    """Retryable means the ladder retries and then escalates; the rest fails fast."""
    translated = _translate(exc)
    assert isinstance(translated, expected)
    if expected is LLMError:
        assert not isinstance(translated, LLMUnavailableError)


async def test_the_provider_translates_sdk_errors_rather_than_leaking_them() -> None:
    class Boom:
        class messages:  # mirrors the SDK's attribute shape
            @staticmethod
            async def create(**kwargs: Any) -> Any:
                raise type("APITimeoutError", (Exception,), {})("too slow")

    provider = AnthropicProvider(client=Boom())
    with pytest.raises(LLMUnavailableError, match="APITimeoutError"):
        await provider.complete(a_request())


# -- structured output is validated whoever answered ----------------------------------------


async def test_structured_validates_the_answer_on_the_way_back() -> None:
    provider = ScriptedProvider([Rule(when="hello", respond={"verdict": "yes"})])
    answer = await provider.structured(a_request(), Answer)
    assert isinstance(answer, Answer)
    assert answer.verdict == "yes"


async def test_a_provider_that_ignores_the_schema_does_not_get_a_free_pass() -> None:
    provider = ScriptedProvider([Rule(when="hello", respond={"nonsense": 1})])
    with pytest.raises(StructuredOutputError, match="does not fit the schema"):
        await provider.structured(a_request(), Answer)


async def test_an_answer_with_no_structured_payload_is_an_error() -> None:
    provider = ScriptedProvider([Rule(when="hello", text="I would rather chat")])
    with pytest.raises(StructuredOutputError, match="without the structured payload"):
        await provider.structured(a_request(), Answer)


# -- the cassette format ---------------------------------------------------------------------


async def test_the_fake_provider_replays_by_prompt_hash(tmp_path: Path) -> None:
    scripted = ScriptedProvider([Rule(when="hello", respond={"verdict": "yes"})])
    recorder = RecordingProvider(scripted)
    request = a_request()
    await recorder.complete(request)
    path = recorder.cassette.save(tmp_path / "one.json")

    replay = FakeProvider(Cassette.load(path))
    response = await replay.complete(request)
    assert response.structured == {"verdict": "yes"}
    assert list(Cassette.load(path).interactions) == [request.fingerprint()]


async def test_a_changed_prompt_misses_loudly_rather_than_answering_the_wrong_question(
    tmp_path: Path,
) -> None:
    """The whole point of keying on the hash: a prompt change invalidates the recording."""
    scripted = ScriptedProvider([Rule(when="hello", respond={"verdict": "yes"})])
    recorder = RecordingProvider(scripted)
    await recorder.complete(a_request())
    replay = FakeProvider(recorder.cassette)

    changed = a_request(system=[SystemBlock(text="static prefix!", cache=True)])
    with pytest.raises(CassetteMiss) as raised:
        await replay.complete(changed)
    assert changed.fingerprint() in str(raised.value)
    assert "re-record" in str(raised.value)


async def test_a_cassette_can_be_regenerated_against_another_provider_without_a_test_changing(
    tmp_path: Path,
) -> None:
    """The property the format exists for.

    A cassette stores the whole canonical request beside its hash, so a machine that has an API
    key can replay the recorded *requests* through the live provider and rewrite the responses.
    The keys do not move, because the requests did not: every test that looked one up still
    finds it, and nothing in a test file mentions a hash.
    """
    first = RecordingProvider(ScriptedProvider([Rule(when="hello", respond={"verdict": "yes"})]))
    await first.complete(a_request())
    path = first.cassette.save(tmp_path / "before.json")
    before = Cassette.load(path)

    # Stands in for AnthropicProvider on a machine with a key: same requests, different answers.
    live_ish = RecordingProvider(
        ScriptedProvider([Rule(when="hello", respond={"verdict": "no"})]), Cassette()
    )
    for recorded in before.requests():
        await live_ish.complete(recorded)
    after = live_ish.cassette
    after.save(path)

    assert list(after.interactions) == list(before.interactions)
    replay = FakeProvider(Cassette.load(path))
    response = await replay.complete(a_request())
    assert response.structured == {"verdict": "no"}


def test_a_cassette_refuses_a_version_it_does_not_understand(tmp_path: Path) -> None:
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 99, "interactions": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="cassette version"):
        Cassette.load(path)


def test_the_fingerprint_covers_everything_that_changes_the_answer() -> None:
    base = a_request()
    for change in (
        {"model": "claude-opus-5"},
        {"purpose": "summary"},
        {"max_tokens": 100},
        {"system": [SystemBlock(text="different", cache=True)]},
        {"messages": [PromptMessage(role="user", content=[TextPart(text="other")])]},
        {"tools": [ModelTool(name="t", description="d", input_schema={})]},
        {
            "structured": StructuredSpec(
                name="decide", description="Answer.", json_schema=json_schema_for(LlmNodeOutput)
            )
        },
    ):
        assert a_request(**change).fingerprint() != base.fingerprint(), change
    assert a_request().fingerprint() == base.fingerprint()


def test_a_real_prompt_hashes_stably_across_processes() -> None:
    """Two assemblies of the same inputs must produce the same key, or replay is a lottery."""
    inputs = PromptInputs(persona="p", policies="q", node_instructions="r", state={"a": 1})
    first, second = assemble(inputs), assemble(inputs)
    request = CompletionRequest(model="m", system=first.system, messages=first.messages)
    other = CompletionRequest(model="m", system=second.system, messages=second.messages)
    assert request.fingerprint() == other.fingerprint()


def test_a_tool_call_round_trips_through_the_assistant_message() -> None:
    scripted_response = ScriptedProvider([])
    assert scripted_response.name == "scripted"
    from support_core.llm.types import CompletionResponse

    response = CompletionResponse(
        model="m", text="one moment", tool_calls=[ToolCall(id="tu_1", name="get_balance")]
    )
    message = response.assistant_message()
    assert [part.type for part in message.content] == ["text", "tool_use"]


# -- the live group ---------------------------------------------------------------------------


@pytest.mark.live
async def test_the_live_provider_answers_a_structured_call() -> None:
    """Opt in with ``pytest -m live``. Skips cleanly when there is no key.

    This is the only test in the phase that would spend money, and the only one that would tell
    us whether :func:`build_payload` produces something the API accepts.
    """
    if not api_key_present():
        pytest.skip("ANTHROPIC_API_KEY is not set; the live group needs one")
    provider = AnthropicProvider()
    request = CompletionRequest(
        model="claude-sonnet-5",
        system=[SystemBlock(text="Answer with the tool you are given.", cache=True)],
        messages=[PromptMessage(role="user", content=[TextPart(text="Say the word yes.")])],
        max_tokens=256,
    )
    answer = await provider.structured(request, Answer)
    assert isinstance(answer.verdict, str)


@pytest.mark.live
async def test_the_cache_breakpoint_is_read_back_on_the_second_turn() -> None:
    """Review finding V3's one-line settlement, which needs a key.

    A static prefix long enough for the model to cache should show
    ``cache_creation_input_tokens`` on the first call and ``cache_read_input_tokens`` on the
    second. If this fails with both at zero, the breakpoint is not doing what section 11.1 says
    it does and :data:`MIN_CACHEABLE_TOKENS` is wrong for this model.
    """
    if not api_key_present():
        pytest.skip("ANTHROPIC_API_KEY is not set; the live group needs one")
    provider = AnthropicProvider()
    prefix = "You are a careful assistant. " * 200  # well over the 1024-token minimum
    request = CompletionRequest(
        model="claude-sonnet-5",
        system=[SystemBlock(text=prefix, cache=True)],
        messages=[PromptMessage(role="user", content=[TextPart(text="Say the word yes.")])],
        max_tokens=64,
    )
    first = await provider.complete(request)
    second = await provider.complete(request)
    assert first.usage.cache_creation_input_tokens or second.usage.cache_read_input_tokens
