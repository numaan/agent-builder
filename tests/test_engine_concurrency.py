"""Single writer per conversation. Implements DESIGN.md section 17 "Concurrency".

"``pg_advisory_xact_lock(hash(conversation_id))`` around each turn. Inbound messages that
arrive while locked are stored in ``message`` with ``status = pending`` and processed in order
when the lock frees."

The contention here is real. Two separate OS processes, two Postgres connections, one of them
deliberately slow while holding the lock. Sequential calls that merely look concurrent would
prove nothing: they would pass against an engine with no lock at all.

The pack echoes the message that drove each turn, so the order two processes handled two
messages in is visible in the customer's transcript and not inferred from timing.
"""

import asyncio
import json
import subprocess
import sys
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor, conversation_lock, lock_key
from support_core.graph.pack import Pack
from tests.engine_support import (
    PACKS,
    REPO_ROOT,
    Recorder,
    messages,
    outbound_texts,
    run_row,
    trace_rows,
)

QUEUE_PACK = PACKS / "queue_pack"


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack(QUEUE_PACK)


def _child(conversation_id: uuid.UUID, text_: str, **options: Any) -> subprocess.Popen[bytes]:
    payload = {
        "pack": str(QUEUE_PACK),
        "conversation": str(conversation_id),
        "text": text_,
        **options,
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.engine_child", json.dumps(payload)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _result(child: subprocess.Popen[bytes]) -> dict[str, Any]:
    out, err = child.communicate(timeout=90)
    assert child.returncode == 0, err.decode(errors="replace")
    return dict(json.loads(out.decode()))


async def _wait_until_claimed(engine: AsyncEngine, conversation_id: uuid.UUID, body: str) -> None:
    """Wait until the other process has the lock and has claimed its message."""
    for _ in range(400):
        rows = await messages(engine, conversation_id)
        if any(row["text"] == body and row["status"] == "received" for row in rows):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"the other process never claimed {body!r}")


async def test_two_processes_send_at_once_and_the_turns_run_in_order(
    pack: Pack, engine: AsyncEngine
) -> None:
    executor = Executor(pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation()
    first = await executor.on_inbound(conversation_id, "start")
    assert first.status == "waiting_customer"

    # Process A takes the lock and stays inside its turn for a second and a half.
    slow = _child(conversation_id, "one", sleep_at="after_checkpoint", sleep_seconds=1.5)
    await _wait_until_claimed(engine, conversation_id, "one")
    # Process B arrives while A holds the lock. Its message must not be lost, and must not
    # overtake A's.
    fast = _child(conversation_id, "two")

    slow_result, fast_result = _result(slow), _result(fast)

    assert await outbound_texts(engine, conversation_id) == [
        "Say something.",
        "You said one.",
        "Say something.",
    ], "the second message was handled after the first, by whichever process got the lock"
    inbound = [
        row for row in await messages(engine, conversation_id) if row["direction"] == "inbound"
    ]
    assert [(row["text"], row["status"]) for row in inbound] == [
        ("start", "received"),
        ("one", "received"),
        ("two", "received"),
    ]
    assert slow_result["messages_processed"] + fast_result["messages_processed"] == 2
    assert not slow_result["queued"] and not fast_result["queued"]

    row = await run_row(engine, conversation_id)
    steps = await trace_rows(engine, row["id"])
    assert [step["seq"] for step in steps] == list(range(1, len(steps) + 1))
    assert len({step["step_id"] for step in steps}) == len(steps)
    assert row["checkpoint_seq"] == len(steps)
    assert row["status"] == "waiting_customer", "the run is parked on the second turn's ask"


async def test_a_message_that_arrives_while_locked_waits_and_is_then_processed(
    pack: Pack, engine: AsyncEngine
) -> None:
    """The queue itself: with the lock held elsewhere, the message is durable and pending."""
    executor = Executor(pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "start")

    impatient = Executor(pack, engine, hooks=Recorder().hooks(), lock_wait_seconds=0)
    async with conversation_lock(engine, conversation_id, wait_seconds=0) as held:
        assert held, "the test could not take the lock it is about to defend"
        outcome = await impatient.on_inbound(conversation_id, "one")
        assert outcome.queued is True
        assert outcome.messages_processed == 0
        rows = await messages(engine, conversation_id)
        assert [row["status"] for row in rows if row["direction"] == "inbound"] == [
            "received",
            "pending",
        ]

    processed = await impatient.drain(conversation_id)
    assert processed.messages_processed == 1
    assert (await outbound_texts(engine, conversation_id))[-1] == "You said one."


async def test_the_lock_is_per_conversation(pack: Pack, engine: AsyncEngine) -> None:
    """Two conversations never block each other; the same conversation always does."""
    executor = Executor(pack, engine, hooks=Recorder().hooks())
    first = await executor.start_conversation()
    second = await executor.start_conversation()
    assert lock_key(first) != lock_key(second)

    async with conversation_lock(engine, first, wait_seconds=0) as held_first:
        assert held_first
        async with conversation_lock(engine, second, wait_seconds=0) as held_second:
            assert held_second, "a different conversation must not be blocked"
        async with conversation_lock(engine, first, wait_seconds=0) as held_again:
            assert not held_again, "the same conversation must be blocked"


async def test_a_dead_process_releases_the_lock(pack: Pack, engine: AsyncEngine) -> None:
    """The lock lives in a transaction, so nobody has to run an unlock after a crash."""
    executor = Executor(pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "start")

    child = _child(conversation_id, "one", crash_at="checkpoint_before_commit", crash_after=0)
    _, err = child.communicate(timeout=90)
    assert child.returncode == 9, err.decode(errors="replace")

    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid IS NOT NULL")
        )
        assert result.scalar_one() >= 0  # the query is here to prove pg_locks is readable
    async with conversation_lock(engine, conversation_id, wait_seconds=2) as acquired:
        assert acquired, "the dead process's lock was never released"

    recovered = await executor.drain(conversation_id)
    assert recovered.status == "done"
    assert (await outbound_texts(engine, conversation_id))[-1] == "You said one."
    inbound = [
        row for row in await messages(engine, conversation_id) if row["direction"] == "inbound"
    ]
    assert [row["status"] for row in inbound] == ["received", "received"], (
        "the message the dead process had claimed was replayed, not lost"
    )


async def test_a_failure_that_is_not_a_busy_lock_is_reported_not_swallowed(
    pack: Pack, engine: AsyncEngine
) -> None:
    """Independent review finding R5.

    ``conversation_lock`` treated every ``DBAPIError`` from the lock statement as "somebody else
    is holding it", so a dropped connection, a cancelled statement or a permissions error came
    back to the caller as ``queued=True`` with the message left pending and the failure recorded
    nowhere at all. Only SQLSTATE ``55P03`` (``lock_timeout`` fired) means busy.

    The cancellation here is a ``statement_timeout`` shorter than the lock wait, which is a real
    Postgres error (``57014``) raised by the same statement on the same path.
    """
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from support_core.storage.config import test_database_url

    executor = Executor(pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation()

    impatient = create_async_engine(
        test_database_url(),
        poolclass=NullPool,
        connect_args={"server_settings": {"statement_timeout": "150"}},
    )
    try:
        async with conversation_lock(engine, conversation_id, wait_seconds=0) as held:
            assert held
            with pytest.raises(DBAPIError) as failure:
                async with conversation_lock(impatient, conversation_id, wait_seconds=30):
                    pass  # pragma: no cover - the lock statement never returns
        assert getattr(failure.value.orig, "sqlstate", None) == "57014"
    finally:
        await impatient.dispose()

    # A genuinely busy lock is still reported as busy rather than raised.
    async with conversation_lock(engine, conversation_id, wait_seconds=0) as held:
        assert held
        async with conversation_lock(engine, conversation_id, wait_seconds=0.05) as second:
            assert not second
