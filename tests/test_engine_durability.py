"""Crash and resume. Implements the phase-2 exit criterion and DESIGN.md sections 7.1 and 7.3.

"Engine crash mid-node: on resume, the step is re-executed. Tool idempotency keys make
re-execution safe." The invariant this file proves is stronger and simpler to state:

    **Wherever the process dies, the conversation resumes to the same outcome.**

"Same outcome" is checked as a whole: the ordered trace (sequence number, node, edge, state
patch), the final frame stack, the final status, and the customer-visible messages - not one
assertion at one convenient point. The kill happens at four different places in the loop, at
every node of the turn, which is twenty-eight combinations - including the node that pushes
a frame and the node that pops one, where the stack changes shape:

* ``before_node``    - the process died having decided what to run and nothing else;
* ``after_node``     - the node ran, its effects are in memory, the checkpoint has not started;
* ``checkpoint_before_commit`` - inside the transaction, one instruction before ``COMMIT``;
* ``after_checkpoint`` - the commit returned and the loop had not yet moved on.

and one of them is repeated with a real ``os._exit`` in a separate OS process, so the claim does
not rest on an in-process model of dying.
"""

import json
import subprocess
import sys
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from support_core import load_pack
from support_core.engine import Executor, step_id
from support_core.graph.pack import Pack
from support_core.storage.config import test_database_url as database_url
from support_core.storage.repositories import RunUpdate, StepWrite, write_checkpoint
from support_core.storage.session import make_session_factory
from tests.engine_support import (
    DETERMINISTIC_PACK,
    Recorder,
    SimulatedCrash,
    outbound_texts,
    run_row,
    trace_rows,
)

CRASH_POINTS = ["before_node", "after_node", "checkpoint_before_commit", "after_checkpoint"]
REPO_ROOT = DETERMINISTIC_PACK.parent.parent.parent


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack(DETERMINISTIC_PACK)


async def _signature(engine: AsyncEngine, conversation_id: uuid.UUID) -> dict[str, Any]:
    """Everything about a finished conversation that a caller could observe."""
    row = await run_row(engine, conversation_id)
    steps = await trace_rows(engine, row["id"])
    return {
        "status": row["status"],
        "frames": row["frames"],
        "checkpoint_seq": row["checkpoint_seq"],
        "trace": [
            (step["seq"], step["node_id"], step["edge"], step["state_patch"], step["error"])
            for step in steps
        ],
        "step_suffixes": [step["step_id"].split(":", 1)[1] for step in steps],
        "outbound": await outbound_texts(engine, conversation_id),
    }


async def _uninterrupted(pack: Pack, engine: AsyncEngine) -> dict[str, Any]:
    executor = Executor(pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation(
        inputs={"amount": 250.0}, context={"customer": {"name": "Ada"}}
    )
    await executor.on_inbound(conversation_id, "hello")
    return await _signature(engine, conversation_id)


@pytest.mark.parametrize("point", CRASH_POINTS)
@pytest.mark.parametrize("crash_after", [0, 1, 2, 3, 4, 5, 6])
async def test_a_crash_anywhere_resumes_to_the_same_outcome(
    pack: Pack, engine: AsyncEngine, point: str, crash_after: int
) -> None:
    baseline = await _uninterrupted(pack, engine)

    dying = Recorder(crash_at=point, crash_after=crash_after)
    executor = Executor(pack, engine, hooks=dying.hooks())
    conversation_id = await executor.start_conversation(
        inputs={"amount": 250.0}, context={"customer": {"name": "Ada"}}
    )
    with pytest.raises(SimulatedCrash):
        await executor.on_inbound(conversation_id, "hello")

    # The process is gone: a new engine, a new session factory, a new executor, new hooks.
    # Only Postgres survives.
    reborn = create_async_engine(database_url(), poolclass=NullPool)
    try:
        survivor = Executor(pack, reborn, hooks=Recorder().hooks())
        outcome = await survivor.drain(conversation_id)
        assert outcome.status == "done"
        assert await _signature(reborn, conversation_id) == baseline
    finally:
        await reborn.dispose()


async def test_a_real_process_killed_mid_transaction_resumes_to_the_same_outcome(
    pack: Pack, engine: AsyncEngine
) -> None:
    """The same claim, without the in-process model of dying.

    The child calls ``os._exit`` inside the checkpoint transaction: no unwinding, no rollback,
    no released advisory lock. Postgres discovers the connection is gone on its own.
    """
    baseline = await _uninterrupted(pack, engine)

    executor = Executor(pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation(
        inputs={"amount": 250.0}, context={"customer": {"name": "Ada"}}
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.engine_child",
            json.dumps(
                {
                    "mode": "crash",
                    "pack": str(DETERMINISTIC_PACK),
                    "conversation": str(conversation_id),
                    "text": "hello",
                    "crash_at": "checkpoint_before_commit",
                    "crash_after": 3,
                }
            ),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 9, completed.stderr.decode(errors="replace")

    mid = await run_row(engine, conversation_id)
    assert mid["status"] == "running", "the dead process left the run marked in flight"
    assert mid["checkpoint_seq"] == 3, "the transaction it died inside committed nothing"

    survivor = Executor(pack, engine, hooks=Recorder().hooks())
    outcome = await survivor.drain(conversation_id)

    assert outcome.status == "done"
    assert await _signature(engine, conversation_id) == baseline


async def test_the_retried_step_keeps_its_step_id(pack: Pack, engine: AsyncEngine) -> None:
    """DESIGN.md section 7.1's step id is what phase 4 uses as a tool idempotency key.

    A node re-executed after a crash must compute the *same* id, or the retry would look like a
    new call and a non-idempotent tool would run twice.
    """
    dying = Recorder(crash_at="after_node", crash_after=2)
    executor = Executor(pack, engine, hooks=dying.hooks())
    conversation_id = await executor.start_conversation(inputs={"amount": 250.0})
    with pytest.raises(SimulatedCrash):
        await executor.on_inbound(conversation_id, "hello")

    row = await run_row(engine, conversation_id)
    frames = row["frames"]
    expected = step_id(row["id"], frames[-1]["frame_seq"], frames[-1]["node_id"], 0)

    survivor = Executor(pack, engine, hooks=Recorder().hooks())
    await survivor.drain(conversation_id)

    steps = [step["step_id"] for step in await trace_rows(engine, row["id"])]
    assert expected in steps
    assert len(steps) == len(set(steps)), "a retry must not add a second row for the same step"


async def test_the_frame_stack_and_the_trace_step_commit_together(
    pack: Pack, engine: AsyncEngine
) -> None:
    """DESIGN.md section 7.1: "one transaction". Killed inside it, neither half is there."""
    dying = Recorder(crash_at="checkpoint_before_commit", crash_after=1)
    executor = Executor(pack, engine, hooks=dying.hooks())
    conversation_id = await executor.start_conversation(inputs={"amount": 250.0})
    with pytest.raises(SimulatedCrash):
        await executor.on_inbound(conversation_id, "hello")

    row = await run_row(engine, conversation_id)
    steps = await trace_rows(engine, row["id"])
    assert row["checkpoint_seq"] == len(steps) == 1
    assert row["frames"][-1]["node_id"] == "classify", "the stack stopped where the trace did"
    assert [step["node_id"] for step in steps] == ["greet"]


async def test_two_checkpoints_of_one_step_id_are_refused_by_the_database(
    engine: AsyncEngine,
) -> None:
    """The last line of defence, if the executor ever lost track of an attempt number."""
    from sqlalchemy.exc import IntegrityError

    from support_core.storage import repositories as repo

    sessions = make_session_factory(engine)
    async with sessions() as session, session.begin():
        conversation = await repo.create_conversation(session, channel="web_chat")
        run = await repo.create_run(
            session,
            conversation_id=conversation.id,
            pack_version="1.0.0",
            pack_fingerprint="x",
        )
        conversation_id, run_id = conversation.id, run.id

    recorder = Recorder()
    now = recorder.now

    def write(seq: int) -> tuple[RunUpdate, StepWrite]:
        return (
            RunUpdate(
                run_id=run_id,
                status="running",
                frames=[],
                checkpoint_seq=seq,
                next_frame_seq=1,
                turn_nodes=seq,
                updated_at=now,
            ),
            StepWrite(
                run_id=run_id,
                step_id=f"{run_id}:0:greet:0",
                seq=seq,
                node_id="greet",
                started_at=now,
                ended_at=now,
            ),
        )

    async with sessions() as session:
        run_update, step = write(1)
        await write_checkpoint(session, run=run_update, step=step, conversation_id=conversation_id)
    with pytest.raises(IntegrityError, match="uq_trace_step_step_id"):
        async with sessions() as session:
            run_update, step = write(2)
            await write_checkpoint(
                session, run=run_update, step=step, conversation_id=conversation_id
            )


async def test_replay_order_does_not_depend_on_timestamps(pack: Pack, engine: AsyncEngine) -> None:
    """Phase 0 finding F5: ``started_at`` cannot order a run, ``seq`` can.

    The engine writes both timestamps from its own clock, so they are distinct here, but the
    guarantee replay relies on is the unique ``(run_id, seq)``.
    """
    executor = Executor(pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation(inputs={"amount": 250.0})
    outcome = await executor.on_inbound(conversation_id, "hello")
    assert outcome.run_id is not None

    steps = await trace_rows(engine, outcome.run_id)
    assert [step["seq"] for step in steps] == list(range(1, len(steps) + 1))
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT count(*) FROM trace_step WHERE run_id = :r AND started_at IS NULL"),
            {"r": outcome.run_id},
        )
        assert result.scalar_one() == 0
    with pytest.raises(Exception, match="uq_trace_step_run_seq"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO trace_step (run_id, step_id, seq, node_id, started_at) "
                    "VALUES (:r, 'other', 1, 'greet', now())"
                ),
                {"r": outcome.run_id},
            )


async def test_a_crash_between_claiming_a_message_and_delivering_it_keeps_the_message(
    engine: AsyncEngine,
) -> None:
    """The gap the ``message`` table alone cannot cover.

    Claiming marks the row ``received``, so after a crash the queue no longer offers it, and the
    frame stack alone cannot say what the suspended node was waiting for. The run row carries
    the event until the node consumes it, which is what makes this recoverable.
    """
    from tests.engine_support import ENGINE_PACK, messages

    pack = load_pack(ENGINE_PACK)
    executor = Executor(pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation(
        context={"customer": {"identity_verified": True}}
    )
    await executor.on_inbound(conversation_id, "hi")

    dying = Recorder(crash_at="before_node", crash_after=0)
    doomed = Executor(pack, engine, hooks=dying.hooks())
    with pytest.raises(SimulatedCrash):
        await doomed.on_inbound(conversation_id, "40")

    row = await run_row(engine, conversation_id)
    assert row["awaiting"]["turn_event"]["text"] == "40"

    survivor = Executor(pack, engine, hooks=Recorder().hooks())
    outcome = await survivor.drain(conversation_id)

    assert outcome.status == "done"
    assert (await outbound_texts(engine, conversation_id))[-1] == "Settled."
    steps = await trace_rows(engine, row["id"])
    resumed = next(step for step in steps if step["state_patch"].get("amount") == "40")
    assert resumed["node_id"] == "collect"
    inbound = [
        message
        for message in await messages(engine, conversation_id)
        if message["direction"] == "inbound"
    ]
    assert [message["status"] for message in inbound] == ["received", "received"]


async def test_a_run_whose_graph_changed_underneath_it_hands_off(
    engine: AsyncEngine, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """DESIGN.md section 6.7: "if none exists and the shapes differ, the conversation is handed
    off". Phase 9 owns the migration hook; phase 2 owes the safe default."""
    import shutil

    from tests.engine_support import ENGINE_PACK

    original = tmp_path_factory.mktemp("packs") / "before"
    shutil.copytree(ENGINE_PACK, original)
    executor = Executor(load_pack(original), engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation(inputs={"amount": 250.0})
    await executor.on_inbound(conversation_id, "hi")
    assert (await run_row(engine, conversation_id))["frames"][-1]["state"] == {"amount": 250.0}

    # The pack is edited and redeployed with the state field renamed. The new pack is perfectly
    # valid; it is the frame already on disk that no longer fits it.
    changed = tmp_path_factory.mktemp("packs") / "after"
    shutil.copytree(ENGINE_PACK, changed)
    root = changed / "graphs" / "root.yaml"
    root.write_text(
        root.read_text(encoding="utf-8")
        .replace("state.amount", "state.charge_amount")
        .replace("  amount: float | None", "  charge_amount: float | None")
        .replace("slots: [amount]", "slots: [charge_amount]"),
        encoding="utf-8",
    )
    recorder = Recorder()
    upgraded = Executor(load_pack(changed), engine, hooks=recorder.hooks())
    outcome = await upgraded.on_inbound(conversation_id, "40")

    assert outcome.status == "waiting_human"
    assert [request.reason for request in recorder.handoffs] == ["pack_incompatible"]
    assert "state shape" in (recorder.handoffs[0].detail or "")


async def test_a_conversation_nobody_touches_again_is_still_recovered(
    pack: Pack, engine: AsyncEngine
) -> None:
    """A crashed turn leaves the run ``running`` and no deadline for the timeout sweep to find.

    Without a recovery sweep it would sit there until the customer wrote again, which for an
    email conversation could be never.
    """
    baseline = await _uninterrupted(pack, engine)

    dying = Recorder(crash_at="after_node", crash_after=1)
    executor = Executor(pack, engine, hooks=dying.hooks())
    conversation_id = await executor.start_conversation(
        inputs={"amount": 250.0}, context={"customer": {"name": "Ada"}}
    )
    with pytest.raises(SimulatedCrash):
        await executor.on_inbound(conversation_id, "hello")

    stalled = await run_row(engine, conversation_id)
    assert stalled["status"] == "running"
    assert stalled["timeout_at"] is None, "a run in flight has no deadline to sweep"

    survivor = Executor(pack, engine, hooks=Recorder().hooks())
    assert await survivor.recover_stalled(older_than=timedelta(days=1)) == []
    outcomes = await survivor.recover_stalled(older_than=timedelta(seconds=-1))

    assert [outcome.conversation_id for outcome in outcomes] == [conversation_id]
    assert await _signature(engine, conversation_id) == baseline
