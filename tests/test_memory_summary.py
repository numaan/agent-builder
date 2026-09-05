"""The rolling conversation summary. DESIGN.md section 10.

Two things have to be true, and the second one matters more than the first:

1. The summary is rewritten every K turns and reaches the next prompt (the feature).
2. Nothing a turn *depends on* comes out of it (the rule phase 2's two must-fix findings were
   both about). The strongest available statement of that is a differential run: the same
   conversation, once normally and once with the summary destroyed between every turn, must
   produce the same durable outcome down to the trace.
"""

import dataclasses
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.engine.hooks import SummaryRequest
from support_core.graph.pack import Pack
from support_core.llm.fake import Rule, ScriptedProvider
from support_core.llm.wiring import StructuredSlotExtractor, service_for_pack
from support_core.memory import LlmSummarizer
from tests.engine_support import PACKS, Recorder, outbound_texts, run_row, trace_rows

LLM_PACK = PACKS / "llm_pack"
CLASSIFY = "Classify the customer's latest message"
CHAT = "Reply to the small talk"
EXTRACT = "The workflow asked the customer for specific values"
SUMMARY = "Write a short factual summary"

RULES = [
    Rule(
        when=CLASSIFY,
        respond={
            "message_to_customer": None,
            "decision": "small_talk",
            "state_updates": {"intent": "small_talk"},
            "citations": [],
            "confidence": 0.9,
            "needs_handoff": False,
        },
    ),
    Rule(
        when=CHAT,
        respond={
            "message_to_customer": "Hello there.",
            "decision": "done",
            "state_updates": {},
            "citations": [],
            "confidence": 0.9,
            "needs_handoff": False,
        },
    ),
    Rule(when=SUMMARY, respond={"summary": "The customer said hello twice."}),
]


@pytest.fixture
def pack() -> Pack:
    return load_pack(LLM_PACK)


def build(pack: Pack, engine: AsyncEngine) -> tuple[Executor, ScriptedProvider, Recorder]:
    provider = ScriptedProvider(list(RULES))
    service = service_for_pack(pack, provider)
    recorder = Recorder()
    hooks = dataclasses.replace(
        recorder.hooks(),
        extract_slots=StructuredSlotExtractor(service),
        summarize=LlmSummarizer(service, max_chars=pack.manifest.memory.max_summary_chars),
    )
    return Executor(pack, engine, hooks=hooks, llm=service), provider, recorder


async def conversation_row(engine: AsyncEngine, conversation_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT summary, turn_count, summary_turn FROM conversation WHERE id = :c"),
            {"c": conversation_id},
        )
        return dict(result.mappings().one())


async def test_the_summary_is_written_every_k_turns(pack: Pack, engine: AsyncEngine) -> None:
    """``summarize_every_turns`` is 2 here, so one turn is not due and the second one is."""
    executor, _, _ = build(pack, engine)
    conversation_id = await executor.start_conversation()

    await executor.on_inbound(conversation_id, "hello")
    first = await conversation_row(engine, conversation_id)
    assert first["turn_count"] == 1
    assert first["summary"] is None, "one turn is not K turns"

    await executor.on_inbound(conversation_id, "hello again")
    second = await conversation_row(engine, conversation_id)
    assert second["turn_count"] == 2
    assert second["summary"] == "The customer said hello twice."
    assert second["summary_turn"] == 2


async def test_the_turn_counter_is_durable_and_advances_with_the_claim(
    pack: Pack, engine: AsyncEngine
) -> None:
    """Phase 2's lesson: a counter the turn depends on lives in the transaction that starts it.

    Killing the process inside the claim transaction must leave the count where it was, and the
    message still pending, so the retried turn is counted once and only once.
    """
    executor, _, _ = build(pack, engine)
    conversation_id = await executor.start_conversation()
    assert (await conversation_row(engine, conversation_id))["turn_count"] == 0

    dying = Recorder(crash_at="claim_before_commit")
    hooks = dataclasses.replace(
        dying.hooks(),
        extract_slots=executor.hooks.extract_slots,
        summarize=executor.hooks.summarize,
    )
    crasher = Executor(pack, engine, hooks=hooks, llm=executor.llm)
    from tests.engine_support import SimulatedCrash

    with pytest.raises(SimulatedCrash):
        await crasher.on_inbound(conversation_id, "hello")
    assert (await conversation_row(engine, conversation_id))["turn_count"] == 0

    await executor.drain(conversation_id)
    assert (await conversation_row(engine, conversation_id))["turn_count"] == 1


async def test_the_summary_reaches_the_next_prompt_as_fenced_data(
    pack: Pack, engine: AsyncEngine
) -> None:
    """Layer 9 (DESIGN.md section 11.2), and fenced: the summary is written from customer text,
    so it inherits that text's trust level."""
    executor, provider, _ = build(pack, engine)
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "hello")
    await executor.on_inbound(conversation_id, "hello again")
    await executor.on_inbound(conversation_id, "and again")

    from support_core.llm.fake import render_request

    last = render_request([call for call in provider.calls if call.purpose == "node"][-1])
    assert "-----BEGIN UNTRUSTED DATA (conversation summary so far)-----" in last
    assert "The customer said hello twice." in last


async def test_a_summariser_that_fails_does_not_fail_the_turn(
    pack: Pack, engine: AsyncEngine
) -> None:
    """Memory is a nice-to-have; the turn is not."""

    async def broken(request: SummaryRequest) -> str | None:
        msg = "the summariser is on fire"
        raise RuntimeError(msg)

    provider = ScriptedProvider(list(RULES))
    service = service_for_pack(pack, provider)
    recorder = Recorder()
    hooks = dataclasses.replace(recorder.hooks(), summarize=broken)
    executor = Executor(pack, engine, hooks=hooks, llm=service)

    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "hello")
    outcome = await executor.on_inbound(conversation_id, "hello again")

    assert outcome.status == "done"
    assert recorder.handoffs == []
    assert (await conversation_row(engine, conversation_id))["summary"] is None


async def test_nothing_a_turn_depends_on_comes_from_the_summary(
    pack: Pack, engine: AsyncEngine
) -> None:
    """The durability rule, stated as a differential run.

    Two identical conversations: one keeps its rolling summary, the other has it deleted from
    the database between every turn. If any durable outcome differed - the path taken, the state
    written, what the customer was told - then a lost or stale summary would be able to change
    what the agent *did*, and memory would have become authority.
    """
    signatures = []
    prompts = []
    for destroy in (False, True):
        executor, provider, _ = build(pack, engine)
        conversation_id = await executor.start_conversation()
        for message in ("hello", "hello again", "and again"):
            await executor.on_inbound(conversation_id, message)
            if destroy:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("UPDATE conversation SET summary = NULL WHERE id = :c"),
                        {"c": conversation_id},
                    )
        row = await run_row(engine, conversation_id)
        steps = await trace_rows(engine, row["id"])
        prompts.append([call.fingerprint() for call in provider.calls if call.purpose == "node"])
        signatures.append(
            {
                "status": row["status"],
                "frames": row["frames"],
                "steps": [
                    (step["node_id"], step["edge"], step["state_patch"], step["error"])
                    for step in steps
                ],
                "outbound": await outbound_texts(engine, conversation_id),
            }
        )
    assert signatures[0] == signatures[1]
    # ... and the comparison is not vacuous: the summary really did reach one set of prompts and
    # not the other, so the runs asked the model different questions and still did the same thing.
    assert prompts[0] != prompts[1]


async def test_a_pack_that_asks_for_no_summary_never_calls_the_summariser(
    pack: Pack, engine: AsyncEngine, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``summarize_every_turns: 0`` is off, and off means no model call at all."""
    import shutil

    target = tmp_path_factory.mktemp("packs") / "pack"
    shutil.copytree(LLM_PACK, target)
    manifest = target / "pack.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "summarize_every_turns: 2", "summarize_every_turns: 0"
        ),
        encoding="utf-8",
    )
    quiet = load_pack(target)
    calls: list[SummaryRequest] = []

    async def counting(request: SummaryRequest) -> str | None:
        calls.append(request)
        return "never"

    provider = ScriptedProvider(list(RULES))
    service = service_for_pack(quiet, provider)
    hooks = dataclasses.replace(Recorder().hooks(), summarize=counting)
    executor = Executor(quiet, engine, hooks=hooks, llm=service)
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "hello")
    await executor.on_inbound(conversation_id, "hello again")

    assert calls == []
    assert (await conversation_row(engine, conversation_id))["summary"] is None
