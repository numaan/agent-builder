"""Suspension, resumption and timeouts. Implements DESIGN.md section 7.2 and part of 7.3.

| Status | Waiting for | Resumed by |
|--------|-------------|------------|
| ``waiting_customer`` | Next customer message | Channel inbound |
| ``waiting_human`` | Human agent action | Desk API: ``resume`` or ``close`` |
| ``waiting_async_tool`` | Long-running tool | Tool callback or poller |
| ``waiting_timer`` | Scheduled follow-up | Scheduler |

Every row is exercised. ``waiting_customer`` uses the ``ask`` node; the other three use the
``wait`` node type the tests register, because DESIGN.md gives no core node that suspends into
them (they belong to phase 4's async tools and to a scheduler). Each resume happens on a
*different* ``Executor`` object from the one that suspended, because the design's whole claim is
that a run resumes from Postgres and not from anything held in memory.
"""

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import EngineError, Executor
from support_core.graph.pack import Pack
from tests.engine_support import (
    ENGINE_PACK,
    PACKS,
    Recorder,
    custom_node_types,
    outbound_texts,
    path,
    run_row,
)

CUSTOM_PACK = PACKS / "custom_pack"


@pytest.fixture
def custom_types() -> Iterator[None]:
    with custom_node_types():
        yield None


@pytest.fixture
def custom_pack(custom_types: None) -> Pack:
    return load_pack(CUSTOM_PACK)


@pytest.fixture
def engine_pack() -> Pack:
    return load_pack(ENGINE_PACK)


def _executor(pack: Pack, engine: AsyncEngine, recorder: Recorder | None = None) -> Executor:
    return Executor(pack, engine, hooks=(recorder or Recorder()).hooks())


async def _start(pack: Pack, engine: AsyncEngine, mode: str) -> tuple[Executor, uuid.UUID]:
    executor = _executor(pack, engine)
    conversation_id = await executor.start_conversation(
        context={"customer": {"attributes": {"mode": mode}}}
    )
    return executor, conversation_id


# -- waiting_customer ---------------------------------------------------------------------


async def test_an_ask_node_suspends_waiting_for_the_customer(
    engine_pack: Pack, engine: AsyncEngine
) -> None:
    executor = _executor(engine_pack, engine)
    conversation_id = await executor.start_conversation()
    outcome = await executor.on_inbound(conversation_id, "hi")

    assert outcome.status == "waiting_customer"
    assert await outbound_texts(engine, conversation_id) == [
        "Hello.",
        "How much was the charge?",
    ]
    row = await run_row(engine, conversation_id)
    assert row["awaiting"] == {
        "kind": "node",
        "status": "waiting_customer",
        "node": "collect",
        # The address the next resume event is delivered to, in durable state so that a crash
        # cannot make the engine guess it from a stack that has moved (review finding R2).
        "frame_seq": 0,
        "detail": {"node": "collect", "slots": ["amount"]},
    }
    assert row["suspended_at"] is not None
    # web chat: pack.yaml says 30 minutes, then close
    assert row["timeout_at"] - row["suspended_at"] == timedelta(minutes=30)


async def test_a_reply_days_later_on_another_executor_resumes_the_same_run(
    engine_pack: Pack, engine: AsyncEngine
) -> None:
    """DESIGN.md section 7.2: an email conversation waits for days and then continues."""
    first = _executor(engine_pack, engine)
    conversation_id = await first.start_conversation(
        channel="email", context={"customer": {"identity_verified": True}}
    )
    await first.on_inbound(conversation_id, "hi")

    later = Executor(engine_pack, engine, hooks=Recorder().hooks())
    outcome = await later.on_inbound(conversation_id, "40")

    assert outcome.status == "done"
    assert outcome.run_id is not None
    assert await path(engine, outcome.run_id) == [
        "welcome",
        "collect",  # the ask, suspending
        "collect",  # the resume: a second execution, so a second step id
        "identity",
        "decide",
        "settle",
        "finish",
    ]
    assert (await outbound_texts(engine, conversation_id))[-1] == "Settled."


async def test_the_resumed_ask_gets_a_new_step_id(engine_pack: Pack, engine: AsyncEngine) -> None:
    """``attempt`` is the fourth field of the step id, so suspend and resume never collide."""
    executor = _executor(engine_pack, engine)
    conversation_id = await executor.start_conversation(
        context={"customer": {"identity_verified": True}}
    )
    await executor.on_inbound(conversation_id, "hi")
    outcome = await executor.on_inbound(conversation_id, "40")

    assert outcome.run_id is not None
    asks = [
        step
        for step in await _steps(engine, outcome.run_id)
        if step.endswith(("collect:0", "collect:1"))
    ]
    assert [step.rsplit(":", 1)[-1] for step in asks] == ["0", "1"]


async def _steps(engine: AsyncEngine, run_id: uuid.UUID) -> list[str]:
    from tests.engine_support import trace_rows

    return [row["step_id"] for row in await trace_rows(engine, run_id)]


async def test_an_email_conversation_has_no_short_timeout(
    engine_pack: Pack, engine: AsyncEngine
) -> None:
    """DESIGN.md section 7.2: "in email it does nothing for days"; the pack says a week."""
    executor = _executor(engine_pack, engine)
    conversation_id = await executor.start_conversation(channel="email")
    await executor.on_inbound(conversation_id, "hi")

    row = await run_row(engine, conversation_id)
    assert row["timeout_at"] - row["suspended_at"] == timedelta(days=7)


# -- waiting_async_tool, waiting_timer, waiting_human -------------------------------------


@pytest.mark.parametrize(
    ("mode", "status", "resume"),
    [
        ("tool", "waiting_async_tool", "resume_async_tool"),
        ("timer", "waiting_timer", "resume_timer"),
        ("human", "waiting_human", "resume_human"),
    ],
)
async def test_every_suspend_status_resumes_on_a_fresh_executor(
    custom_pack: Pack, engine: AsyncEngine, mode: str, status: str, resume: str
) -> None:
    executor, conversation_id = await _start(custom_pack, engine, mode)
    outcome = await executor.on_inbound(conversation_id, "go")
    assert outcome.status == status

    fresh = _executor(custom_pack, engine)
    method = getattr(fresh, resume)
    kwargs: dict[str, object] = {"payload": {"detail": "ok"}}
    if resume == "resume_human":
        kwargs = {"patch": {"detail": "ok"}}
    elif resume == "resume_timer":
        # The scheduler carries no payload: a timer fires, it does not bring data.
        kwargs = {}
    resumed = await method(conversation_id, **kwargs)

    assert resumed.status == "done"
    expected = "Done with nothing." if resume == "resume_timer" else "Done with ok."
    assert (await outbound_texts(engine, conversation_id))[-1] == expected


async def test_resuming_the_wrong_status_is_refused(custom_pack: Pack, engine: AsyncEngine) -> None:
    executor, conversation_id = await _start(custom_pack, engine, "timer")
    await executor.on_inbound(conversation_id, "go")
    with pytest.raises(EngineError, match="not 'waiting_async_tool'"):
        await executor.resume_async_tool(conversation_id)


async def test_a_customer_message_does_not_jump_a_run_waiting_for_a_tool(
    custom_pack: Pack, engine: AsyncEngine
) -> None:
    """The message is durable and stays queued; the tool callback still gets its turn."""
    executor, conversation_id = await _start(custom_pack, engine, "tool")
    await executor.on_inbound(conversation_id, "go")
    outcome = await executor.on_inbound(conversation_id, "are you there?")

    assert outcome.messages_processed == 0
    assert outcome.status == "waiting_async_tool"
    from tests.engine_support import messages

    inbound = [
        row for row in await messages(engine, conversation_id) if row["direction"] == "inbound"
    ]
    assert [row["status"] for row in inbound] == ["received", "pending"]


async def test_closing_from_the_desk_ends_the_conversation(
    custom_pack: Pack, engine: AsyncEngine
) -> None:
    executor, conversation_id = await _start(custom_pack, engine, "human")
    await executor.on_inbound(conversation_id, "go")
    outcome = await executor.resume_human(conversation_id, close=True)

    assert outcome.status == "done"
    assert (await run_row(engine, conversation_id))["status"] == "done"


# -- timeouts -----------------------------------------------------------------------------


async def test_a_timed_out_async_tool_hands_off(custom_pack: Pack, engine: AsyncEngine) -> None:
    recorder = Recorder()
    executor = _executor(custom_pack, engine, recorder)
    conversation_id = await executor.start_conversation(
        context={"customer": {"attributes": {"mode": "tool"}}}
    )
    await executor.on_inbound(conversation_id, "go")

    before = await run_row(engine, conversation_id)
    outcomes = await executor.sweep_timeouts(before["timeout_at"] + timedelta(seconds=1))

    assert [outcome.handoff_reason for outcome in outcomes] == ["timeout"]
    assert [request.reason for request in recorder.handoffs] == ["timeout"]
    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human"
    assert row["timeout_at"] is None


async def test_a_timed_out_web_chat_customer_closes_the_conversation(
    engine_pack: Pack, engine: AsyncEngine
) -> None:
    executor = _executor(engine_pack, engine)
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "hi")

    row = await run_row(engine, conversation_id)
    await executor.sweep_timeouts(row["timeout_at"] + timedelta(seconds=1))

    assert (await run_row(engine, conversation_id))["status"] == "done"
    async with engine.connect() as connection:
        from sqlalchemy import text

        result = await connection.execute(
            text("SELECT status, closed_at FROM conversation WHERE id = :c"),
            {"c": conversation_id},
        )
        status, closed_at = result.one()
    assert status == "closed"
    assert closed_at is not None


async def test_a_timeout_whose_action_is_none_only_clears_the_deadline(
    custom_pack: Pack, engine: AsyncEngine
) -> None:
    executor, conversation_id = await _start(custom_pack, engine, "timer")
    await executor.on_inbound(conversation_id, "go")

    row = await run_row(engine, conversation_id)
    assert row["timeout_at"] is not None
    await executor.sweep_timeouts(row["timeout_at"] + timedelta(seconds=1))

    after = await run_row(engine, conversation_id)
    assert after["status"] == "waiting_timer"
    assert after["timeout_at"] is None


# -- failure routing (DESIGN.md section 7.3) ----------------------------------------------


async def test_a_node_failure_takes_the_on_error_edge_when_one_is_declared(
    custom_pack: Pack, engine: AsyncEngine
) -> None:
    recorder = Recorder()
    executor = _executor(custom_pack, engine, recorder)
    conversation_id = await executor.start_conversation(
        context={"customer": {"attributes": {"mode": "error_routed"}}}
    )
    outcome = await executor.on_inbound(conversation_id, "go")

    assert outcome.status == "done"
    assert recorder.handoffs == []
    assert outcome.run_id is not None
    assert await path(engine, outcome.run_id) == ["pick", "explode_routed", "recovered", "finish"]
    from tests.engine_support import trace_rows

    steps = await trace_rows(engine, outcome.run_id)
    failed = next(step for step in steps if step["node_id"] == "explode_routed")
    assert failed["edge"] == "on_error"
    assert "always fails" in failed["error"]


async def test_a_node_failure_without_an_on_error_edge_hands_off(
    custom_pack: Pack, engine: AsyncEngine
) -> None:
    recorder = Recorder()
    executor = _executor(custom_pack, engine, recorder)
    conversation_id = await executor.start_conversation(
        context={"customer": {"attributes": {"mode": "error"}}}
    )
    outcome = await executor.on_inbound(conversation_id, "go")

    assert outcome.status == "waiting_human"
    assert [request.reason for request in recorder.handoffs] == ["node_error"]
    assert recorder.handoffs[0].frames[-1].node_id == "explode"
