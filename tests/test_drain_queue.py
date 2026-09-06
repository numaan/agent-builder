"""The drain worker: the caller that makes a non-blocking inbound handler safe.

Phase 2 left ``lock_wait_seconds=0`` and ``Executor.drain`` in place with nothing calling them
(finding R7). These tests are about the worker's own behaviour - what it does with a lock it
cannot take, with a conversation that keeps failing, and with ten messages for one conversation -
against a stand-in executor, so that the behaviour is visible rather than inferred from timings
of a real turn.
"""

import asyncio
import uuid
from typing import Any

from support_core.api.drain import DrainQueue
from support_core.engine.types import TurnOutcome


class FakeExecutor:
    """Answers ``drain`` from a script. Not an :class:`~support_core.engine.Executor`."""

    def __init__(self, queued_times: int = 0, fail_times: int = 0) -> None:
        self.queued_times = queued_times
        self.fail_times = fail_times
        self.calls: list[uuid.UUID] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = False

    async def drain(self, conversation_id: uuid.UUID) -> TurnOutcome:
        self.calls.append(conversation_id)
        self.started.set()
        if self.hold:
            await self.release.wait()
        if self.fail_times > 0:
            self.fail_times -= 1
            msg = "the database went away"
            raise RuntimeError(msg)
        if self.queued_times > 0:
            self.queued_times -= 1
            return TurnOutcome(conversation_id=conversation_id, queued=True)
        return TurnOutcome(conversation_id=conversation_id, status="done")


def queue_for(executor: Any, **kwargs: Any) -> DrainQueue:
    return DrainQueue(executor, workers=2, first_delay=0.001, max_delay=0.01, **kwargs)


async def test_a_submitted_conversation_is_drained() -> None:
    executor = FakeExecutor()
    queue = queue_for(executor)
    await queue.start()
    try:
        conversation_id = uuid.uuid4()
        queue.submit(conversation_id)
        assert await queue.wait_until_idle(timeout=10.0)
    finally:
        await queue.aclose()

    assert executor.calls == [conversation_id]
    assert queue.stats.drained == 1


async def test_a_lock_held_elsewhere_is_retried_until_it_is_free() -> None:
    executor = FakeExecutor(queued_times=3)
    queue = queue_for(executor)
    await queue.start()
    try:
        queue.submit(uuid.uuid4())
        assert await queue.wait_until_idle(timeout=10.0)
    finally:
        await queue.aclose()

    assert len(executor.calls) == 4
    assert queue.stats.retries == 3
    assert queue.stats.drained == 1


async def test_a_lock_nobody_ever_releases_is_given_up_on_rather_than_retried_for_ever() -> None:
    """Giving up is safe - the message is durable and ``pending``, and whoever holds the lock
    drains the queue in order - and retrying for ever is not."""
    executor = FakeExecutor(queued_times=10_000)
    queue = queue_for(executor, budget_seconds=0.05)
    await queue.start()
    try:
        queue.submit(uuid.uuid4())
        assert await queue.wait_until_idle(timeout=10.0)
    finally:
        await queue.aclose()

    assert queue.stats.gave_up == 1
    assert queue.stats.drained == 0


async def test_ten_messages_for_one_conversation_are_one_piece_of_work() -> None:
    """``drain`` processes the whole pending queue in order, so a burst is one thing to do.

    The submission that arrives while the drain is running is remembered, though: a row that
    commits after the last claim found nothing would otherwise wait for the next message.
    """
    executor = FakeExecutor()
    executor.hold = True
    queue = queue_for(executor)
    await queue.start()
    try:
        conversation_id = uuid.uuid4()
        queue.submit(conversation_id)
        await asyncio.wait_for(executor.started.wait(), timeout=10.0)
        for _ in range(9):
            queue.submit(conversation_id)
        executor.hold = False
        executor.release.set()
        assert await queue.wait_until_idle(timeout=10.0)
    finally:
        await queue.aclose()

    assert queue.stats.submitted == 10
    assert queue.stats.coalesced == 9
    assert queue.stats.requeued == 1
    assert len(executor.calls) == 2, "the drain that ran, and one more for what arrived during it"


async def test_a_conversation_that_raises_does_not_take_the_worker_down() -> None:
    """The same rule phase 2's finding R3 settled for the recovery sweep: one poisoned
    conversation must not stop the ones behind it."""
    executor = FakeExecutor(fail_times=1)
    seen: list[str] = []
    queue = queue_for(executor, on_error=lambda cid, exc: seen.append(str(exc)))
    await queue.start()
    try:
        first, second = uuid.uuid4(), uuid.uuid4()
        queue.submit(first)
        queue.submit(second)
        assert await queue.wait_until_idle(timeout=10.0)
    finally:
        await queue.aclose()

    assert seen == ["the database went away"]
    assert queue.stats.failed == 1
    assert queue.stats.drained == 1


async def test_a_drained_conversation_is_announced() -> None:
    """A turn run by the worker is a turn nobody's connection ran, so whoever is watching has to
    be told what the conversation looks like now - otherwise a browser never learns that the
    turn ended waiting for a confirmation."""
    executor = FakeExecutor()
    told: list[uuid.UUID] = []

    async def announce(conversation_id: uuid.UUID) -> None:
        told.append(conversation_id)

    queue = queue_for(executor, on_drained=announce)
    await queue.start()
    try:
        conversation_id = uuid.uuid4()
        queue.submit(conversation_id)
        assert await queue.wait_until_idle(timeout=10.0)
    finally:
        await queue.aclose()

    assert told == [conversation_id]
