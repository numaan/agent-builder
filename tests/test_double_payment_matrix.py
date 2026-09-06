"""The independent review's ten double-payment attempts, kept as a test.

reviews/phase-4.md's "Double-payment attempts" table asks one question ten ways: can one
customer intent produce two side effects? Eight of the ten were safe when the review ran; rows 9
and 10 were not, and both were finding R1 - an async call whose identity changed between the
dispatch and the callback, and the ``on_error`` re-entry that turned the resulting failure into
a second dispatch.

Rows 3 and 5 kill a real OS process, which needs a subprocess and a file-backed ledger;
``tests/test_tool_crash_recovery.py`` owns them and is not duplicated here. Everything else is
in-process and is here, with the ledger - the only honest answer to "did it run twice" - as the
assertion.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.storage import repositories as repo
from support_core.storage.session import make_session_factory
from support_core.tools import ToolFailed, ToolRefused
from support_core.tools.approval import canonical_args
from tests.tool_support import (
    CHARGE,
    LEDGER,
    approve,
    conversation_and_run,
    runtime,
    site,
)


@pytest.fixture(autouse=True)
def ledger() -> None:
    LEDGER.reset()


async def _kill_after_claim(engine: AsyncEngine, key: str, *, status: str = "running") -> None:
    """Leave the row a dead process would leave: claimed, with no outcome recorded."""
    sessions = make_session_factory(engine)
    async with sessions() as session, session.begin():
        await session.execute(
            text("UPDATE tool_call SET status = :s, result = NULL WHERE idempotency_key = :k"),
            {"s": status, "k": key},
        )


async def test_01_a_death_before_the_claim_leaves_nothing_and_the_retry_runs_once(
    engine: AsyncEngine,
) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    async with engine.connect() as connection:
        rows = await connection.execute(text("SELECT count(*) FROM tool_call"))
        assert rows.scalar_one() == 0

    await runtime(engine).invoke(
        tool_name="ping", args={}, site=site(conversation_id, run_id), caller="tool_node"
    )
    assert LEDGER.executed == [("ping", 0.0)]


async def test_02_a_death_after_the_claim_refuses_a_non_idempotent_retry(
    engine: AsyncEngine,
) -> None:
    """Indistinguishable from row 3 by construction, which is why refusing is the right answer."""
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    args = canonical_args(CHARGE, {"amount": 29.0})
    await approve(engine, conversation_id=conversation_id, run_id=run_id, tool="charge", args=args)
    await runtime(engine).invoke(
        tool_name="charge",
        args={"amount": 29.0},
        site=where,
        caller="tool_node",
        requires_approval="confirm_it",
    )
    await _kill_after_claim(engine, where.step_id)

    with pytest.raises(ToolRefused, match="not idempotent"):
        await runtime(engine).invoke(
            tool_name="charge",
            args={"amount": 29.0},
            site=where,
            caller="tool_node",
            requires_approval="confirm_it",
        )
    assert LEDGER.executed == [("charge", 29.0)], "the first attempt, and nothing after it"
    async with engine.connect() as connection:
        status = await connection.execute(text("SELECT status FROM tool_call"))
        assert status.scalar_one() == "indeterminate"


async def test_04_a_death_during_the_record_is_the_same_state_and_the_same_answer(
    engine: AsyncEngine,
) -> None:
    """An idempotent tool repeats; the row is the same ``running`` row as row 2."""
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    args = canonical_args(CHARGE, {"amount": 7.0})
    await approve(
        engine, conversation_id=conversation_id, run_id=run_id, tool="charge_again", args=args
    )
    await runtime(engine).invoke(
        tool_name="charge_again",
        args={"amount": 7.0},
        site=where,
        caller="tool_node",
        requires_approval="confirm_it",
    )
    await _kill_after_claim(engine, where.step_id)

    await runtime(engine).invoke(
        tool_name="charge_again",
        args={"amount": 7.0},
        site=where,
        caller="tool_node",
        requires_approval="confirm_it",
    )
    assert LEDGER.executed == [("charge", 7.0), ("charge", 7.0)], "declared safe to repeat"
    async with engine.connect() as connection:
        rows = await connection.execute(text("SELECT count(*), max(attempts) FROM tool_call"))
        assert tuple(rows.one()) == (1, 2), "one key, two attempts, one approval"


async def test_05_two_callers_racing_one_step_id(engine: AsyncEngine) -> None:
    """The unique ``idempotency_key`` makes one of them the claimant."""
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)

    async def attempt() -> str:
        try:
            await runtime(engine).invoke(tool_name="ping", args={}, site=where, caller="tool_node")
        except (ToolRefused, ToolFailed):
            return "refused"
        return "ran"

    outcomes = await asyncio.gather(attempt(), attempt())
    assert LEDGER.executed == [("ping", 0.0)], "one side effect from two racing claimants"
    assert "ran" in outcomes
    async with engine.connect() as connection:
        rows = await connection.execute(text("SELECT count(*) FROM tool_call"))
        assert rows.scalar_one() == 1


async def test_06_two_concurrent_callers_spending_one_approval(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    args = canonical_args(CHARGE, {"amount": 29.0})
    await approve(engine, conversation_id=conversation_id, run_id=run_id, tool="charge", args=args)

    async def attempt(node_id: str) -> str:
        try:
            await runtime(engine).invoke(
                tool_name="charge",
                args={"amount": 29.0},
                site=site(conversation_id, run_id, node_id=node_id),
                caller="tool_node",
                requires_approval="confirm_it",
            )
        except ToolRefused:
            return "refused"
        return "ran"

    outcomes = await asyncio.gather(attempt("one"), attempt("two"))
    assert sorted(outcomes) == ["ran", "refused"]
    assert LEDGER.executed == [("charge", 29.0)]


async def test_07_a_resumed_run_cannot_collide_its_step_id_with_a_live_one(
    engine: AsyncEngine,
) -> None:
    """The key carries the run id and the monotonic frame sequence, neither of which is reused."""
    conversation_id, run_id = await conversation_and_run(engine)
    first = site(conversation_id, run_id, frame_seq=1)
    second = site(conversation_id, run_id, frame_seq=2)
    assert first.step_id != second.step_id

    await runtime(engine).invoke(tool_name="ping", args={}, site=first, caller="tool_node")
    await runtime(engine).invoke(tool_name="ping", args={}, site=second, caller="tool_node")
    assert LEDGER.executed == [("ping", 0.0), ("ping", 0.0)], "two frames, two calls"
    async with engine.connect() as connection:
        keys = await connection.execute(
            text("SELECT count(DISTINCT idempotency_key) FROM tool_call")
        )
        assert keys.scalar_one() == 2


async def test_08_two_different_logical_calls_under_one_step_id(engine: AsyncEngine) -> None:
    """The key belongs to the call it was claimed for (commit 14c5e32)."""
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    await runtime(engine).invoke(
        tool_name="peek", args={"amount": 1.0}, site=where, caller="tool_node"
    )
    with pytest.raises(ToolRefused, match="with different arguments"):
        await runtime(engine).invoke(
            tool_name="peek", args={"amount": 2.0}, site=where, caller="tool_node"
        )
    assert LEDGER.executed == [("peek", 1.0)]


async def test_09_and_10_the_async_dispatch_and_its_callback(engine: AsyncEngine) -> None:
    """Rows 9 and 10, which were the two that broke: see ``tests/test_async_tool_flow.py``.

    That file drives the whole thing through the executor. What is asserted here is the property
    underneath it: an async call is completed under the key its dispatch claimed, and completing
    it does not run the tool a second time.
    """
    from support_core.tools import FunctionTool, Risk, ToolContext, ToolRegistry
    from support_core.tools.runtime import ToolRuntime
    from tests.tool_support import Amount, Flag

    async def _dispatch(payload: Any, ctx: ToolContext) -> Flag:
        LEDGER.executed.append(("dispatch", payload.amount))
        return Flag()

    tool = FunctionTool(
        name="dispatch",
        description="A long-running write.",
        input_model=Amount,
        output_model=Flag,
        risk=Risk.WRITE,
        confirm_exempt=True,
        confirm_exempt_reason="dispatching is not the act",
        async_=True,
        handler=_dispatch,
    )
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    tools = ToolRuntime(ToolRegistry([tool]), make_session_factory(engine))

    started = await tools.invoke(
        tool_name="dispatch", args={"amount": 4.0}, site=where, caller="tool_node"
    )
    assert started.pending
    finished = await tools.complete_async(
        tool_name="dispatch",
        site=where,
        payload={"ok": True},
        key=started.idempotency_key,
    )
    assert not finished.pending
    assert LEDGER.executed == [("dispatch", 4.0)], "dispatched once, completed once"
    sessions = make_session_factory(engine)
    async with sessions() as session:
        calls = await repo.tool_calls_for_run(session, run_id)
    assert [call.status for call in calls] == ["succeeded"]


def test_03_and_the_process_kills_live_in_the_crash_recovery_tests() -> None:
    """Rows 3 and 5's real ``os._exit`` cases: named here so the matrix is complete."""
    from pathlib import Path

    crash = Path(__file__).with_name("test_tool_crash_recovery.py")
    assert crash.exists()
    source = crash.read_text(encoding="utf-8")
    assert "os._exit" in source or "SUPPORT_TEST_CRASH" in source
