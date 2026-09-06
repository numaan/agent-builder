"""Re-run of the phase-2 durability and concurrency proofs after the resolution.

Not collected by ``pytest`` (the file name does not match ``test_*.py``); run explicitly::

    python -m pytest tests/verify_phase_2_resolution.py -q

It reproduces the scenarios from the independent review's "Crash and concurrency attempts" log
that the repository's own suite does not carry, so that the fixes for R1 to R6 can be shown not
to have weakened an invariant that already held. The numbering follows the review's tables.
"""

import asyncio
import json
import subprocess
import sys
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from support_core import load_pack
from support_core.engine import Executor, conversation_lock
from support_core.graph.pack import Pack
from support_core.storage.config import test_database_url as database_url
from tests.engine_support import (
    ENGINE_PACK,
    PACKS,
    REPO_ROOT,
    Recorder,
    SimulatedCrash,
    messages,
    outbound_texts,
    run_row,
    trace_rows,
)

CRASH_POINTS = ["before_node", "after_node", "checkpoint_before_commit", "after_checkpoint"]
QUEUE_PACK = PACKS / "queue_pack"


def _echoes(outbound: list[str]) -> list[str]:
    return [line for line in outbound if line.startswith("You said")]


def _expected_echoes(inbound: list[dict[str, Any]]) -> list[str]:
    """``queue_pack`` echoes every second message, in queue order.

    Its root graph is ask-then-echo, so a message that arrives with the run idle or done starts
    a fresh root frame and is consumed by the ``ask``; the next one resumes it and is echoed.

    *Queue order* is read from ``message.queue_seq``, which is the number the row claimed from
    ``conversation.inbound_seq`` when it was written. It used to be read from ``created_at``,
    because that was also what the pending queue was ordered by - and phase W's review broke
    exactly that (finding W4): ``created_at`` is the transaction *start* timestamp, so two
    callers who arrive together share it and the tie fell to a random UUID, and the reviewer
    reversed a pair and dead-ended a conversation. The property this file asserts is unchanged -
    every message answered exactly once, in the order the queue decided, nothing lost - and it is
    now read from the column that decides it rather than from a clock that approximated it.
    """
    ordered = sorted(inbound, key=lambda row: row["queue_seq"] or 0)
    return [f"You said {row['text']}." for row in ordered[1::2]]


@pytest.fixture(scope="module")
def pack() -> Pack:
    return load_pack(ENGINE_PACK)


@pytest.fixture(scope="module")
def queue_pack() -> Pack:
    return load_pack(QUEUE_PACK)


async def _signature(engine: AsyncEngine, conversation_id: uuid.UUID) -> dict[str, Any]:
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
        "inbound": [
            (message["text"], message["status"])
            for message in await messages(engine, conversation_id)
            if message["direction"] == "inbound"
        ],
    }


async def _conversation(pack: Pack, engine: AsyncEngine, recorder: Recorder) -> uuid.UUID:
    executor = Executor(pack, engine, hooks=recorder.hooks())
    return await executor.start_conversation(context={"customer": {"identity_verified": True}})


# -- #4: the whole suspend-and-resume conversation, killed at every point ------------------


@pytest.mark.parametrize("point", CRASH_POINTS)
@pytest.mark.parametrize("crash_after", [0, 1, 2, 3, 4, 5, 6])
async def test_suspend_and_resume_crash_matrix(
    pack: Pack, engine: AsyncEngine, point: str, crash_after: int
) -> None:
    """Review scenario #4: a conversation that suspends, waits, and is resumed.

    Seven probe occurrences per point across the two turns (say, ask/suspend, ask/resume, gate,
    router, say, end), so 28 combinations, each compared against the uninterrupted run.
    """
    control = Recorder()
    baseline_conversation = await _conversation(pack, engine, control)
    baseline_executor = Executor(pack, engine, hooks=control.hooks())
    await baseline_executor.on_inbound(baseline_conversation, "hi")
    await baseline_executor.on_inbound(baseline_conversation, "40")
    baseline = await _signature(engine, baseline_conversation)

    dying = Recorder(crash_at=point, crash_after=crash_after)
    conversation_id = await _conversation(pack, engine, dying)
    doomed = Executor(pack, engine, hooks=dying.hooks())
    crashed_on: str | None = None
    for body in ("hi", "40"):
        try:
            await doomed.on_inbound(conversation_id, body)
        except SimulatedCrash:
            crashed_on = body
            break
    assert crashed_on, f"{point} never happened {crash_after + 1} times: the case would be vacuous"

    reborn = create_async_engine(database_url(), poolclass=NullPool)
    try:
        survivor = Executor(pack, reborn, hooks=Recorder().hooks())
        await survivor.drain(conversation_id)
        if crashed_on == "hi":
            # The reply was never sent, because the process died during the first turn.
            await survivor.on_inbound(conversation_id, "40")
        assert await _signature(reborn, conversation_id) == baseline
    finally:
        await reborn.dispose()


# -- #9: dying again during recovery, and again during the second recovery -----------------


async def test_three_successive_deaths_still_converge(pack: Pack, engine: AsyncEngine) -> None:
    control = Recorder()
    baseline_conversation = await _conversation(pack, engine, control)
    baseline_executor = Executor(pack, engine, hooks=control.hooks())
    await baseline_executor.on_inbound(baseline_conversation, "hi")
    await baseline_executor.on_inbound(baseline_conversation, "40")
    baseline = await _signature(engine, baseline_conversation)

    first = Recorder(crash_at="after_node", crash_after=1)
    conversation_id = await _conversation(pack, engine, first)
    with pytest.raises(SimulatedCrash):
        await Executor(pack, engine, hooks=first.hooks()).on_inbound(conversation_id, "hi")

    # The first turn's message is already consumed, so recovery re-enters the turn rather than
    # claiming anything: the second and third deaths land inside that re-entry.
    second = Recorder(crash_at="before_node", crash_after=0)
    with pytest.raises(SimulatedCrash):
        await Executor(pack, engine, hooks=second.hooks()).drain(conversation_id)

    third = Recorder(crash_at="checkpoint_before_commit", crash_after=0)
    with pytest.raises(SimulatedCrash):
        await Executor(pack, engine, hooks=third.hooks()).drain(conversation_id)

    survivor = Executor(pack, engine, hooks=Recorder().hooks())
    await survivor.drain(conversation_id)
    await survivor.on_inbound(conversation_id, "40")
    assert await _signature(engine, conversation_id) == baseline


# -- #14, #15, #18, #19, #21: concurrency ---------------------------------------------------


async def _wait_for(
    engine: AsyncEngine, conversation_id: uuid.UUID, body: str, statuses: set[str]
) -> None:
    """Block until another process's message row has reached one of ``statuses``."""
    for _ in range(600):
        rows = await messages(engine, conversation_id)
        if any(row["text"] == body and row["status"] in statuses for row in rows):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{body!r} never reached {sorted(statuses)}")


def _child(conversation_id: uuid.UUID, body: str, **options: Any) -> subprocess.Popen[bytes]:
    payload = {
        "pack": str(QUEUE_PACK),
        "conversation": str(conversation_id),
        "text": body,
        **options,
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.engine_child", json.dumps(payload)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


async def test_five_processes_on_one_conversation(queue_pack: Pack, engine: AsyncEngine) -> None:
    """Review scenario #14: one slow holder, four arrivals, nothing lost or reordered."""
    executor = Executor(queue_pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "start")

    # Arrival order has to be established by the database, not by sleeping: under load a
    # subprocess can take longer to start than any pause worth writing.
    slow = _child(conversation_id, "one", sleep_at="after_node", sleep_seconds=3.0)
    children = [slow]
    await _wait_for(engine, conversation_id, "one", {"received"})
    for body in ("two", "three", "four", "five"):
        children.append(_child(conversation_id, body))
        await _wait_for(engine, conversation_id, body, {"pending", "received"})
    for child in children:
        _, err = child.communicate(timeout=180)
        assert child.returncode == 0, err.decode(errors="replace")

    await executor.drain(conversation_id)
    inbound = [
        row for row in await messages(engine, conversation_id) if row["direction"] == "inbound"
    ]
    assert [row["text"] for row in inbound] == ["start", "one", "two", "three", "four", "five"]
    assert [row["status"] for row in inbound] == ["received"] * 6
    assert _echoes(await outbound_texts(engine, conversation_id)) == _expected_echoes(inbound)
    run = await run_row(engine, conversation_id)
    steps = [step["step_id"] for step in await trace_rows(engine, run["id"])]
    assert len(steps) == len(set(steps))
    assert [step["seq"] for step in await trace_rows(engine, run["id"])] == list(
        range(1, len(steps) + 1)
    )


async def test_ten_concurrent_inbound_coroutines(queue_pack: Pack, engine: AsyncEngine) -> None:
    """Review scenario #15: ten engines, no pause, every message answered exactly once."""
    starter = Executor(queue_pack, engine, hooks=Recorder().hooks())
    conversation_id = await starter.start_conversation()

    engines = [create_async_engine(database_url(), poolclass=NullPool) for _ in range(10)]
    try:
        await asyncio.gather(
            *(
                Executor(queue_pack, each, hooks=Recorder().hooks()).on_inbound(
                    conversation_id, f"m{index}"
                )
                for index, each in enumerate(engines)
            )
        )
    finally:
        for each in engines:
            await each.dispose()

    await starter.drain(conversation_id)
    inbound = [
        row for row in await messages(engine, conversation_id) if row["direction"] == "inbound"
    ]
    assert [row["status"] for row in inbound] == ["received"] * 10
    assert sorted(row["text"] for row in inbound) == sorted(f"m{index}" for index in range(10))
    assert _echoes(await outbound_texts(engine, conversation_id)) == _expected_echoes(inbound), (
        "answered out of the order the enqueue transactions committed"
    )


async def test_a_killed_lock_holder_with_a_waiter_behind_it(
    queue_pack: Pack, engine: AsyncEngine
) -> None:
    """Review scenario #16, with the waiter already queued when the holder dies."""
    executor = Executor(queue_pack, engine, hooks=Recorder().hooks())
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "start")

    doomed = _child(conversation_id, "one", crash_at="after_checkpoint", crash_after=0)
    await _wait_for(engine, conversation_id, "one", {"received"})
    waiter = _child(conversation_id, "two", lock_wait_seconds=60.0)
    await _wait_for(engine, conversation_id, "two", {"pending", "received"})
    assert doomed.wait(timeout=90) == 9
    _, err = waiter.communicate(timeout=180)
    assert waiter.returncode == 0, err.decode(errors="replace")

    await executor.drain(conversation_id)
    inbound = [
        row for row in await messages(engine, conversation_id) if row["direction"] == "inbound"
    ]
    assert [row["text"] for row in inbound] == ["start", "one", "two"]
    assert [row["status"] for row in inbound] == ["received"] * 3
    assert _echoes(await outbound_texts(engine, conversation_id)) == _expected_echoes(inbound)


async def test_a_resume_racing_an_inbound_message(engine: AsyncEngine) -> None:
    """Review scenario #18: ``resume_human`` and ``on_inbound`` at once, two engines."""
    from tests.engine_support import custom_node_types

    with custom_node_types():
        pack = load_pack(PACKS / "custom_pack")
        first = Executor(pack, engine, hooks=Recorder().hooks())
        conversation_id = await first.start_conversation(
            context={"customer": {"identity_verified": False, "attributes": {"mode": "human"}}}
        )
        outcome = await first.on_inbound(conversation_id, "go")
        assert outcome.status == "waiting_human"

        other = create_async_engine(database_url(), poolclass=NullPool)
        try:
            second = Executor(pack, other, hooks=Recorder().hooks())
            await asyncio.gather(
                first.resume_human(conversation_id, patch={"detail": "seen"}),
                second.on_inbound(conversation_id, "again"),
            )
        finally:
            await other.dispose()

    # Whichever of the two won, the run is in a legal state and both messages are accounted
    # for: "again" either drove a turn of its own or is still queued for one. (A second message
    # after the resume finishes the run starts a fresh root frame, which reaches the same
    # ``wait`` node again - that is the pack, not a race.)
    row = await run_row(engine, conversation_id)
    assert row["status"] in {"done", "idle", "waiting_customer", "waiting_human"}
    inbound = [
        message
        for message in await messages(engine, conversation_id)
        if message["direction"] == "inbound"
    ]
    assert [message["text"] for message in inbound] == ["go", "again"]
    assert all(message["status"] in {"received", "pending"} for message in inbound)


async def test_requeue_keeps_its_place_in_the_queue(engine: AsyncEngine) -> None:
    """Review scenario #21: a gate fires, the reply goes back, a newer message is behind it."""
    from tests.engine_support import custom_node_types, set_context

    with custom_node_types():
        pack = load_pack(PACKS / "custom_pack")
        context = {"customer": {"identity_verified": True, "attributes": {"mode": "gate"}}}
        executor = Executor(pack, engine, hooks=Recorder().hooks())
        conversation_id = await executor.start_conversation(context=context)
        await executor.on_inbound(conversation_id, "go")
        lapsed = {"customer": {"identity_verified": False, "attributes": {"mode": "gate"}}}
        await set_context(engine, conversation_id, lapsed)
        await executor.on_inbound(conversation_id, "42")
        await executor.on_inbound(conversation_id, "later")

        inbound = [
            message
            for message in await messages(engine, conversation_id)
            if message["direction"] == "inbound"
        ]
        assert [(m["text"], m["status"]) for m in inbound] == [
            ("go", "received"),
            ("42", "pending"),
            ("later", "pending"),
        ], "the requeued reply must still be ahead of the message that arrived after it"

        await set_context(engine, conversation_id, context)
        await executor.resume_human(conversation_id)
        await executor.drain(conversation_id)

    assert "Protected: 42." in await outbound_texts(engine, conversation_id)


# -- #22, #25: step ids ---------------------------------------------------------------------


async def test_one_graph_invoked_twice_gets_disjoint_step_ids(engine: AsyncEngine) -> None:
    """Review scenario #22 and #25 together: two invocations, then repeated human retries."""
    from tests.engine_support import custom_node_types, set_context

    with custom_node_types():
        pack = load_pack(PACKS / "custom_pack")
        context = {"customer": {"identity_verified": True, "attributes": {"mode": "gate"}}}
        executor = Executor(pack, engine, hooks=Recorder().hooks())
        conversation_id = await executor.start_conversation(context=context)
        outcome = await executor.on_inbound(conversation_id, "go")
        run_id = outcome.run_id
        assert run_id is not None
        lapsed = {"customer": {"identity_verified": False, "attributes": {"mode": "gate"}}}
        await set_context(engine, conversation_id, lapsed)
        await executor.on_inbound(conversation_id, "42")
        await executor.resume_human(conversation_id)
        await executor.drain(conversation_id)
        await executor.resume_human(conversation_id)

    steps = [step["step_id"] for step in await trace_rows(engine, run_id)]
    assert len(steps) == len(set(steps)), "a step id was reused"
    verify_frames = {
        int(step.split(":")[-3]) for step in steps if step.split(":")[-2] in {"tell", "hold"}
    }
    assert len(verify_frames) >= 2, "each redirect push must be its own frame"


# -- #27 to #29: does the suite still have teeth? -------------------------------------------


async def test_mutation_the_checkpoint_is_split_into_two_transactions(
    pack: Pack, engine: AsyncEngine
) -> None:
    """Review scenario #27, re-run against the resolved code."""
    from support_core.storage import repositories as repo
    from support_core.storage.models import TraceStep

    original = repo.write_checkpoint

    async def split(session: Any, **kwargs: Any) -> None:
        step = kwargs["step"]
        async with session.begin():
            session.add(
                TraceStep(
                    run_id=step.run_id,
                    step_id=step.step_id,
                    seq=step.seq,
                    node_id=step.node_id,
                    edge=step.edge,
                    state_patch=step.state_patch,
                    started_at=step.started_at,
                    ended_at=step.ended_at,
                )
            )
        raise SimulatedCrash("the run row never followed the trace step")

    repo.write_checkpoint = split
    try:
        recorder = Recorder()
        conversation_id = await _conversation(pack, engine, recorder)
        with pytest.raises(SimulatedCrash):
            await Executor(pack, engine, hooks=recorder.hooks()).on_inbound(conversation_id, "hi")
    finally:
        repo.write_checkpoint = original

    survivor = Executor(pack, engine, hooks=Recorder().hooks())
    with pytest.raises(Exception, match="uq_trace_step_step_id"):
        await survivor.drain(conversation_id)


async def test_mutation_the_turn_event_is_forgotten(pack: Pack, engine: AsyncEngine) -> None:
    """Review scenario #28: dropping the durable turn event must still be caught."""
    from support_core.engine import executor as executor_module

    recorder = Recorder()
    conversation_id = await _conversation(pack, engine, recorder)
    executor = Executor(pack, engine, hooks=recorder.hooks())
    await executor.on_inbound(conversation_id, "hi")

    original = executor_module.Executor._with_turn_event

    def forget(self: Any, turn: Any, awaiting: Any) -> Any:
        return awaiting

    executor_module.Executor._with_turn_event = forget  # type: ignore[method-assign]
    try:
        dying = Recorder(crash_at="after_claim", crash_after=0)
        with pytest.raises(SimulatedCrash):
            await Executor(pack, engine, hooks=dying.hooks()).on_inbound(conversation_id, "40")
    finally:
        executor_module.Executor._with_turn_event = original  # type: ignore[method-assign]

    survivor = Executor(pack, engine, hooks=Recorder().hooks())
    await survivor.drain(conversation_id)
    assert (await outbound_texts(engine, conversation_id))[-1] != "Settled.", (
        "losing the turn event must change the outcome, or nothing is testing it"
    )


async def test_mutation_the_conversation_lock_is_a_no_op(
    queue_pack: Pack, engine: AsyncEngine
) -> None:
    """Review scenario #29, and the one place the resolution changed its answer.

    The review saw three of four concurrent turns die with ``IntegrityError`` when the lock was
    replaced by a no-op. They no longer do, because R1's fix added a *second* serialisation
    point below the lock: the claim is one transaction whose ``WHERE status = 'pending'`` makes
    two callers contend for the row rather than both proceeding. That is a strengthening, not a
    weakening - but it means this shape no longer demonstrates the lock's necessity, so what is
    asserted here is what the mutation still costs: the callers that lose the claim go home
    having done nothing, leaving messages queued with nobody coming back for them, which is
    exactly the lost wakeup ``conversation_lock``'s waiting behaviour exists to prevent.

    The lock is still load-bearing and still tested: with ``conversation_lock`` neutered in
    source, four of the five tests in ``tests/test_engine_concurrency.py`` fail, including
    ``test_two_processes_send_at_once_and_the_turns_run_in_order``, which is two real OS
    processes and the assertion the review's mutation was aimed at.
    """
    from contextlib import asynccontextmanager

    from support_core.engine import executor as executor_module

    @asynccontextmanager
    async def no_lock(*args: Any, **kwargs: Any) -> Any:
        yield True

    starter = Executor(queue_pack, engine, hooks=Recorder().hooks())
    conversation_id = await starter.start_conversation()

    original = executor_module.conversation_lock  # type: ignore[attr-defined]
    executor_module.conversation_lock = no_lock  # type: ignore[attr-defined]
    engines = [create_async_engine(database_url(), poolclass=NullPool) for _ in range(4)]
    try:
        results = await asyncio.gather(
            *(
                Executor(queue_pack, each, hooks=Recorder().hooks()).on_inbound(
                    conversation_id, f"m{index}"
                )
                for index, each in enumerate(engines)
            ),
            return_exceptions=True,
        )
    finally:
        executor_module.conversation_lock = original  # type: ignore[attr-defined]
        for each in engines:
            await each.dispose()

    assert not any(isinstance(result, BaseException) for result in results), (
        f"the atomic claim should keep concurrent callers off each other: {results}"
    )
    assert not any(outcome.queued for outcome in results), (  # type: ignore[union-attr]
        "a no-op lock cannot report a message as queued, which is how the concurrency suite "
        "catches this mutation"
    )
    inbound = [
        row for row in await messages(engine, conversation_id) if row["direction"] == "inbound"
    ]
    assert [row["status"] for row in inbound] == ["received"] * 4
    run = await run_row(engine, conversation_id)
    steps = await trace_rows(engine, run["id"])
    assert len({step["step_id"] for step in steps}) == len(steps), "a step id was written twice"
    assert [step["seq"] for step in steps] == list(range(1, len(steps) + 1))
    assert _echoes(await outbound_texts(engine, conversation_id)) == _expected_echoes(inbound)


async def test_the_lock_still_serialises_two_conversations_independently(
    queue_pack: Pack, engine: AsyncEngine
) -> None:
    """Review scenario #20: the lock is per conversation, so one slow turn is not global."""
    executor = Executor(queue_pack, engine, hooks=Recorder().hooks())
    first = await executor.start_conversation()
    second = await executor.start_conversation()

    async with conversation_lock(engine, first, wait_seconds=0) as held:
        assert held
        loop = asyncio.get_running_loop()
        started = loop.time()
        await Executor(queue_pack, engine, hooks=Recorder().hooks()).on_inbound(second, "hello")
        elapsed = loop.time() - started
    assert elapsed < 5.0, f"a locked conversation blocked an unrelated one for {elapsed:.2f}s"
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT count(*) FROM message WHERE conversation_id = :c"), {"c": second}
        )
        assert rows.scalar_one() >= 2
