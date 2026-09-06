"""The handoff packet, its sinks, and every failure path that ends in one.

DESIGN.md section 13 for the packet and the sinks; section 7.3 for the reasons:

    Handoff is also the universal fallback for every failure path in section 7.3.

Two things are being asserted, and they are different in kind. That a packet *contains* what
section 13 lists is a property of one function and is tested directly. That every way the engine
can give up produces one, with the reason it gave up for, is a property of the executor and is
tested by making the engine give up in each of those ways - a limit, a node error carrying its
own diagnosis, a timeout, a pack the run no longer fits, a recovery that ran out of attempts -
and reading the queue afterwards.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.graph.pack import Pack
from support_core.handoff import (
    CompositeSink,
    CustomerRef,
    HandoffPacket,
    HandoffService,
    NullSink,
    PostgresQueueSink,
    SinkError,
    WebhookSink,
    default_sink,
)
from support_core.llm.fake import Rule, ScriptedProvider
from support_core.llm.types import LLMError
from support_core.llm.wiring import service_for_pack
from tests.engine_support import PACKS, outbound_texts, run_row

INTERRUPT_PACK = PACKS / "interrupt_pack"
ENGINE_PACK = PACKS / "engine_pack"
CUSTOM_PACK = PACKS / "custom_pack"
CLASSIFY = "Decide which workflow the customer's message belongs to"
HANDOFF = "handed to a human support agent"


def _answer(label: str) -> dict[str, Any]:
    return {
        "message_to_customer": None,
        "decision": label,
        "state_updates": {"intent": label},
        "citations": [],
        "confidence": 0.9,
        "needs_handoff": False,
    }


async def _handoffs(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            sql_text(
                "SELECT id, run_id, queue, reason, status, graph_id, node_id, step_id, packet, "
                "       sla_due_at FROM handoff WHERE conversation_id = :c ORDER BY created_at, id"
            ),
            {"c": conversation_id},
        )
        return [dict(row) for row in result.mappings()]


def _service(engine: AsyncEngine, executor: Executor, **kwargs: Any) -> HandoffService:
    return HandoffService(
        executor.pack, executor.sessions, sink=default_sink(executor.sessions), **kwargs
    )


# -- the packet -------------------------------------------------------------------------------


async def test_the_packet_carries_everything_section_13_lists(engine: AsyncEngine) -> None:
    """Field by field, from the sample pack's own refund conversation.

    The refund is the conversation worth building a packet from: it has a verified identity, a
    HIGH-risk tool call that really happened, and a customer whose money moved. Reading the
    packet after it is what shows that a person picking this up would not have to guess.
    """
    from support_core.llm.fake import FakeProvider
    from support_core.llm.recording import Cassette
    from tests.cassettes.scenarios import ACME_REFUND, play

    conversation_id, executor = await play(
        ACME_REFUND, engine, FakeProvider(Cassette.load(ACME_REFUND.cassette_path))
    )
    row = await run_row(engine, conversation_id)
    service = _service(engine, executor)

    from support_core.engine.types import Frame
    from support_core.handoff import PacketRequest

    packet = await service.build(
        PacketRequest(
            conversation_id=conversation_id,
            run_id=row["id"],
            reason="tool_failed",
            detail="the billing system said no",
            frames=[Frame.model_validate(frame) for frame in row["frames"]] or [],
            node_id="issue_refund",
            step_id="test-step",
        )
    )

    assert packet.reason == "tool_failed"
    assert packet.detail == "the billing system said no"
    assert packet.identity_verified is True
    assert packet.customer.ref == "cus_acme_1"
    assert packet.customer.email == "me@example.com"
    assert packet.queue == "billing-tier-1"
    assert packet.sla_minutes == 30
    assert packet.transcript_url == f"/desk/conversations/{conversation_id}/transcript"
    assert packet.conversation_id == conversation_id
    # Every WRITE and HIGH call of the conversation, and nothing else: send_otp and verify_otp
    # are WRITE, issue_refund is HIGH, and the three READ lookups are not here.
    assert [action.tool for action in packet.actions_taken] == [
        "send_otp",
        "verify_otp",
        "issue_refund",
    ]
    assert all(action.risk in {"write", "high"} for action in packet.actions_taken)
    refund = packet.actions_taken[-1]
    assert refund.status == "succeeded"
    assert refund.args == {"charge_id": "ch_1002", "amount": 29.0}
    assert refund.idempotency_key
    assert packet.pending_action is None, "nothing is half-done: the refund succeeded"
    assert packet.suggested_next_steps
    assert packet.citations == [], "retrieval is phase 5; a packet does not invent a citation"


async def test_an_approved_action_that_has_not_run_is_the_pending_action(
    engine: AsyncEngine,
) -> None:
    """The field that stops two people refunding the same charge.

    The customer said yes and the call has not been made. A human who did not know that could
    either refund it again or tell the customer it was never approved; both are wrong.
    """
    import dataclasses

    from support_core.llm.fake import FakeProvider
    from support_core.llm.recording import Cassette
    from tests.cassettes.scenarios import ACME_REFUND, play

    # Stop after the confirmation's "yes" is recorded but before the tool node runs, by making
    # the tool node itself the crash point.
    scenario = dataclasses.replace(ACME_REFUND, turns=ACME_REFUND.turns[:2])
    conversation_id, executor = await play(
        scenario, engine, FakeProvider(Cassette.load(ACME_REFUND.cassette_path))
    )
    row = await run_row(engine, conversation_id)
    async with engine.begin() as connection:
        await connection.execute(
            sql_text(
                "INSERT INTO action_approval (conversation_id, run_id, frame_seq, node_id, "
                "step_id, tool, args, args_hash, approved_by, approved_at) VALUES "
                "(:c, :r, 1, 'confirm_refund', 'test:1', 'issue_refund', "
                " CAST(:args AS jsonb), 'abc', 'customer', now())"
            ),
            {
                "c": conversation_id,
                "r": row["id"],
                "args": '{"charge_id": "ch_1002", "amount": 29.0}',
            },
        )

    from support_core.handoff import PacketRequest

    packet = await _service(engine, executor).build(
        PacketRequest(conversation_id=conversation_id, run_id=row["id"], reason="timeout")
    )

    assert packet.pending_action is not None
    assert packet.pending_action.tool == "issue_refund"
    assert packet.pending_action.status == "proposed"
    assert packet.pending_action.approved_by == "customer"
    assert any("has not run" in step for step in packet.suggested_next_steps)


async def test_the_summary_is_written_by_the_escalation_model(engine: AsyncEngine) -> None:
    """DESIGN.md section 5.1: ``escalation_model`` is "used for handoff summaries"."""
    models: list[str] = []

    class Watching(ScriptedProvider):
        async def complete(self, req: Any) -> Any:
            models.append(req.model)
            return await super().complete(req)

    pack = load_pack(INTERRUPT_PACK)
    provider = Watching([Rule(when=HANDOFF, respond={"summary": "A person is needed."})])
    service = service_for_pack(pack, provider)
    executor = Executor(pack, engine, llm=service)
    conversation_id = await executor.start_conversation()

    from support_core.handoff import PacketRequest

    packet = await _service(engine, executor, llm=service).build(
        PacketRequest(conversation_id=conversation_id, run_id=None, reason="node_error")
    )

    assert packet.summary == "A person is needed."
    assert models == ["test-escalation-model"]


async def test_a_summariser_that_fails_does_not_stop_the_handoff(engine: AsyncEngine) -> None:
    """The customer is already waiting for a person. Facts beat nothing."""

    class Broken(ScriptedProvider):
        async def complete(self, req: Any) -> Any:
            msg = "the summariser is down"
            raise LLMError(msg)

    pack = load_pack(INTERRUPT_PACK)
    service = service_for_pack(pack, Broken([]))
    executor = Executor(pack, engine, llm=service)
    conversation_id = await executor.start_conversation()
    handoff = _service(engine, executor, llm=service)

    from support_core.handoff import PacketRequest

    packet, delivered = await handoff.raise_handoff(
        PacketRequest(
            conversation_id=conversation_id, run_id=None, reason="llm_unavailable", detail="down"
        )
    )
    assert delivered, "the summary failed; the queue did not"

    assert packet.summary.startswith("No model summary was available.")
    assert "Reason: llm_unavailable" in packet.summary
    assert "Engine detail: down" in packet.summary
    assert handoff.failures, "a failed summary must be visible somewhere"
    assert await _handoffs(engine, conversation_id), "the packet still reached the queue"


# -- the sinks --------------------------------------------------------------------------------


async def test_the_postgres_sink_writes_the_row_the_desk_reads(engine: AsyncEngine) -> None:
    pack = load_pack(INTERRUPT_PACK)
    executor = Executor(pack, engine)
    conversation_id = await executor.start_conversation()
    handoff = HandoffService(pack, executor.sessions, sink=PostgresQueueSink(executor.sessions))

    from support_core.handoff import PacketRequest

    await handoff.raise_handoff(
        PacketRequest(
            conversation_id=conversation_id, run_id=None, reason="node_error", step_id="s:1"
        )
    )

    rows = await _handoffs(engine, conversation_id)
    assert len(rows) == 1
    assert rows[0]["queue"] == "interrupt-tests"
    assert rows[0]["reason"] == "node_error"
    assert rows[0]["status"] == "open"
    assert rows[0]["sla_due_at"] is not None, "the pack names an sla_minutes"
    assert rows[0]["packet"]["reason"] == "node_error"


async def test_a_packet_delivered_twice_under_one_step_queues_once(engine: AsyncEngine) -> None:
    """A ``handoff`` node builds its packet before the checkpoint that parks the run.

    So a crash in that window re-executes the node and delivers again - and a person must not be
    paged twice for one conversation. ``(run_id, step_id)`` is what makes the second delivery a
    no-op, the same device an approval uses against a re-executed confirm.
    """
    pack = load_pack(INTERRUPT_PACK)
    executor = Executor(pack, engine)
    conversation_id = await executor.start_conversation()
    row = await run_row(engine, conversation_id)
    handoff = HandoffService(pack, executor.sessions, sink=PostgresQueueSink(executor.sessions))

    from support_core.handoff import PacketRequest

    request = PacketRequest(
        conversation_id=conversation_id, run_id=row["id"], reason="node_error", step_id="s:1"
    )
    await handoff.raise_handoff(request)
    await handoff.raise_handoff(request)

    assert len(await _handoffs(engine, conversation_id)) == 1


async def test_the_webhook_sink_posts_the_packet(engine: AsyncEngine) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []

    async def post(url: str, payload: Any) -> None:
        posted.append((url, dict(payload)))

    packet = HandoffPacket(
        reason="node_error",
        summary="s",
        identity_verified=False,
        customer=CustomerRef(),
        workflow="root",
        node="classify",
        transcript_url="/t",
        conversation_id=uuid.uuid4(),
    )
    sink = WebhookSink("https://desk.example/hook", post=post)

    assert await sink.deliver(packet) == "https://desk.example/hook"
    assert posted[0][0] == "https://desk.example/hook"
    assert posted[0][1]["reason"] == "node_error"


async def test_one_sink_failing_does_not_stop_the_others(engine: AsyncEngine) -> None:
    """A webhook that is down must not cost the row a desk can still find."""

    class Refusing:
        name = "refusing"

        async def deliver(self, packet: HandoffPacket) -> str | None:
            msg = "the queue is down"
            raise RuntimeError(msg)

    kept = NullSink()
    composite = CompositeSink([Refusing(), kept])
    packet = HandoffPacket(
        reason="timeout",
        summary="s",
        identity_verified=False,
        customer=CustomerRef(),
        workflow="root",
        node="classify",
        transcript_url="/t",
        conversation_id=uuid.uuid4(),
    )

    with pytest.raises(SinkError):
        await composite.deliver(packet)
    assert kept.delivered == [packet], "the second sink was skipped by the first one's failure"


async def test_a_sink_that_refuses_never_fails_the_turn(engine: AsyncEngine) -> None:
    """The run is parked either way; a sink that raised into the turn would be a second crash."""

    class Refusing:
        name = "refusing"

        async def deliver(self, packet: HandoffPacket) -> str | None:
            msg = "no"
            raise RuntimeError(msg)

    pack = load_pack(INTERRUPT_PACK)
    executor = Executor(pack, engine)
    conversation_id = await executor.start_conversation()
    handoff = HandoffService(pack, executor.sessions, sink=Refusing())

    from support_core.handoff import PacketRequest

    packet, delivered = await handoff.raise_handoff(
        PacketRequest(conversation_id=conversation_id, run_id=None, reason="node_error")
    )

    assert packet.reason == "node_error"
    assert delivered is False, "nothing took it, and the service says so (review finding P2)"
    assert handoff.failures and "RuntimeError" in handoff.failures[0]


# -- the handoff node -------------------------------------------------------------------------


async def test_the_handoff_node_parks_the_run_and_queues_a_packet(engine: AsyncEngine) -> None:
    """The sample pack's ``no_workflow`` node, which phase 6 made real.

    Its message says a specialist has been given the conversation and will reply here. Both
    halves have to be true: a row on the queue with the state and the transcript link, and a
    desk that can write into the conversation. This is the first half.
    """
    from support_core.llm.fake import FakeProvider
    from support_core.llm.recording import Cassette
    from tests.cassettes.scenarios import ACME_ACCOUNT_QUESTION, play

    conversation_id, _executor = await play(
        ACME_ACCOUNT_QUESTION,
        engine,
        FakeProvider(Cassette.load(ACME_ACCOUNT_QUESTION.cassette_path)),
    )

    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human"
    assert row["awaiting"]["kind"] == "node"
    assert row["awaiting"]["node"] == "no_workflow"
    assert row["awaiting"]["detail"]["kind"] == "handoff"
    assert row["awaiting"]["detail"]["reason"] == "no_workflow"
    assert row["awaiting"]["detail"]["queued"] is True

    rows = await _handoffs(engine, conversation_id)
    assert len(rows) == 1
    assert rows[0]["reason"] == "no_workflow"
    assert rows[0]["graph_id"] == "root"
    assert rows[0]["node_id"] == "no_workflow"
    assert rows[0]["packet"]["suggested_next_steps"][0].startswith("Look up the charge")
    said = await outbound_texts(engine, conversation_id)
    assert "passed this to a billing specialist" in said[-1]
    assert "They will reply here." in said[-1]


async def test_a_queue_that_is_down_does_not_promise_a_specialist(engine: AsyncEngine) -> None:
    """Review finding P2. DESIGN.md section 14: never promise an escalation that did not happen.

    The same conversation as the test above, with every sink refusing. The pack's sentence -
    "I have passed this to a billing specialist ... They will reply here" - is the promise this
    phase's own pack change was written to make true, and it is a lie when nothing took the
    packet. So core replaces it, the suspension records that nobody was told, and the queue has
    no row claiming otherwise. The run is still parked and the conversation is still durable,
    which is all the replacement sentence claims.
    """
    from support_core.llm.fake import FakeProvider
    from support_core.llm.recording import Cassette
    from tests.cassettes.scenarios import ACME_ACCOUNT_QUESTION, play

    class Refusing:
        name = "refusing"

        async def deliver(self, packet: HandoffPacket) -> str | None:
            msg = "the queue is down"
            raise RuntimeError(msg)

    conversation_id, _executor = await play(
        ACME_ACCOUNT_QUESTION,
        engine,
        FakeProvider(Cassette.load(ACME_ACCOUNT_QUESTION.cassette_path)),
        sink=Refusing(),
    )

    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human", "the conversation is still parked and still durable"
    assert row["awaiting"]["detail"]["queued"] is False, "'the hook did not raise' is not a page"
    assert not await _handoffs(engine, conversation_id), "nothing took it, so there is no row"

    said = await outbound_texts(engine, conversation_id)
    assert "passed this to a billing specialist" not in said[-1]
    assert "They will reply here" not in said[-1]
    assert "do not want to tell you somebody has it" in said[-1]
    assert "Nothing you have told me is lost" in said[-1]


async def test_the_handoff_nodes_message_promises_only_what_happens(
    engine: AsyncEngine,
) -> None:
    """DESIGN.md section 14's forbidden promises, checked against the pack's own words.

    The old ``no_workflow`` said "I cannot do that yet" because there was no handover. There is
    one now, so the message may say so - but every clause of it still has to be a thing the
    system does, and this test is where that is written down rather than assumed.
    """
    pack = load_pack(REPO_PACK)
    node = pack.graphs["root"].nodes["no_workflow"]
    message = getattr(node, "message", "") or ""

    assert "passed this to a billing specialist" in message
    assert "They will reply here." in message
    # Nothing about when. The pack's `handoff.sla_minutes` is a target for the queue, not a
    # promise to a customer, and the system cannot keep a promise about a person's time.
    assert "minutes" not in message and "hour" not in message and "shortly" not in message
    assert "sorry" not in message.lower(), "an apology is not a fact about what happens next"


# -- every failure path of DESIGN.md 7.3 ------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("error", "node_error"), ("gate_loop", "limit_exceeded")],
    ids=["a node that failed with no on_error edge", "the per-turn node limit"],
)
async def test_a_failure_path_reaches_the_queue_with_its_reason(
    custom_pack: Pack, engine: AsyncEngine, mode: str, reason: str
) -> None:
    """DESIGN.md section 7.3's "otherwise handoff", with the reason the engine gave it."""
    executor, conversation_id = await _custom(custom_pack, engine, mode)

    await executor.on_inbound(conversation_id, "go")

    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human"
    rows = await _handoffs(engine, conversation_id)
    assert [item["reason"] for item in rows] == [reason]
    assert rows[0]["packet"]["reason"] == reason
    assert rows[0]["step_id"], "a packet raised by a step records which one"


async def test_a_timeout_reaches_the_queue(custom_pack: Pack, engine: AsyncEngine) -> None:
    """DESIGN.md section 7.2's timeouts are one of 7.3's failure paths too.

    An async tool that never called back: the pack's rule for ``waiting_async_tool`` is
    ``handoff``, which is the timeout that means "somebody has to look at this" rather than
    "close the tab".
    """
    executor, conversation_id = await _custom(custom_pack, engine, "async")
    await executor.on_inbound(conversation_id, "go")
    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_async_tool"
    async with engine.begin() as connection:
        await connection.execute(
            sql_text("UPDATE run SET timeout_at = now() - interval '1 hour' WHERE id = :r"),
            {"r": row["id"]},
        )

    await executor.sweep_timeouts()

    rows = await _handoffs(engine, conversation_id)
    assert [item["reason"] for item in rows] == ["timeout"]
    assert any("stopped replying" in step for step in rows[0]["packet"]["suggested_next_steps"])
    assert rows[0]["packet"]["detail"] == "timed out in waiting_async_tool"


async def test_a_recovery_that_gives_up_reaches_the_queue(
    custom_pack: Pack, engine: AsyncEngine
) -> None:
    """Review finding R3's parking, now with somebody told about it."""
    executor, conversation_id = await _custom(custom_pack, engine, "crash")
    with pytest.raises(RuntimeError):
        await executor.on_inbound(conversation_id, "go")

    for _ in range(3):
        await executor.recover_stalled(older_than=timedelta(seconds=-1))

    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human"
    assert row["awaiting"]["reason"] == "engine_error"
    rows = await _handoffs(engine, conversation_id)
    assert [item["reason"] for item in rows] == ["engine_error"]


# -- the retry bound --------------------------------------------------------------------------


async def test_a_node_that_keeps_failing_stops_rather_than_repeating(
    custom_pack: Pack, engine: AsyncEngine
) -> None:
    """The run-time half of the shape phase 4's resolution left open.

    An ``on_error`` edge that leads back to its own node retries for as long as the node keeps
    failing, and for a WRITE tool that needs no approval that is one side effect per failure.
    ``limits.max_node_errors`` bounds it: the conversation goes to a person instead, with the
    limit and the last failure in the packet.

    Two turns, not two nodes: the retry loop goes through an ``ask``, which is the only shape a
    pack can even express - a tight ``on_error`` cycle back into a node is refused at load by
    ``graph.unsuspended_cycle``. So the count has to survive a suspension, which is why it lives
    in the frame.
    """
    executor, conversation_id = await _custom(custom_pack, engine, "retry")

    first = await executor.on_inbound(conversation_id, "go")
    assert first.status == "waiting_customer", "the first failure took the on_error edge"

    await executor.on_inbound(conversation_id, "yes, try again")

    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human"
    assert row["awaiting"]["reason"] == "limit_exceeded"
    detail = row["awaiting"]["detail"]["detail"]
    assert "max_node_errors=2" in detail
    assert "failed 2 times in a row" in detail
    rows = await _handoffs(engine, conversation_id)
    assert [item["reason"] for item in rows] == ["limit_exceeded"]


REPO_PACK = PACKS.parent.parent / "packs" / "acme_billing"


@pytest.fixture
def custom_pack() -> Any:
    """The engine test pack, with its registered node types (DESIGN.md section 6.2)."""
    from tests.engine_support import custom_node_types

    with custom_node_types():
        yield load_pack(CUSTOM_PACK)


async def _custom(pack: Any, engine: AsyncEngine, mode: str) -> tuple[Executor, uuid.UUID]:
    """An executor whose handoff hook writes to the queue, driving one scenario of that pack."""
    executor = Executor(pack, engine)
    executor.hooks.handoff = HandoffService(
        pack, executor.sessions, sink=PostgresQueueSink(executor.sessions)
    )
    conversation_id = await executor.start_conversation(
        context={"customer": {"attributes": {"mode": mode}}}
    )
    return executor, conversation_id
