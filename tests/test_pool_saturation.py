"""What a flood of concurrent customers does to one replica (security review finding S2).

    Ten concurrent anonymous requests deny service to the whole deployment... roughly seven
    concurrent turns saturate the pool. Past that, every request blocks for the full 30 s and
    then raises ``QueuePool limit ... timed out`` as an unhandled exception - a 500 with a full
    traceback, not a routed failure or a 503. The desk and ``/healthz`` are on the same pool and
    stall with it. - reviews/security-review-2026-09-07.md, S2

Three properties, one per test, and they are the three the finding took away:

* a flood is answered, not dropped - no 5xx, and every message either ran or came back
  ``queued`` with the drain worker holding it;
* ``/healthz`` keeps answering while the flood is in flight, because an orchestrator reads a
  slow health check as "restart this instance", which is the worst possible response to load;
* nothing is left ``pending`` afterwards, because an inbound row is durable *before* the turn is
  attempted and a customer whose message survived the crash of the turn is still owed an answer.

These tests build their own **pooled** engine. The ``engine`` fixture is ``NullPool`` - a fresh
connection per checkout, no pool to exhaust - which is right for the rest of the suite and would
make this file assert nothing.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api import AppConfig
from support_core.api.app import OVERLOADED_DETAIL, RETRY_AFTER_SECONDS
from support_core.engine.executor import TurnSlots
from support_core.engine.hooks import EngineHooks
from support_core.storage import config as db_config
from support_core.storage.models import Message
from support_core.storage.session import make_engine, make_session_factory
from tests.app_support import TEST_PACKS, build_app, serving

QUEUE_PACK = TEST_PACKS / "queue_pack"

FLOOD = 16
"""The reviewer's largest measurement: 15 of 16 failed."""

SLOW_TURN = 1.0
"""How long each turn holds its conversation lock and its connections. Long enough that the
whole flood is in flight at once, which is the condition the finding is about."""

HEALTH_BUDGET = 2.0
"""The slowest ``/healthz`` this test will accept under load. The finding measured 28.5 s
against a 30 s pool checkout timeout; anything in seconds is already a restart signal."""


def slow_hooks(seconds: float) -> EngineHooks:
    """Hooks whose checkpoints take their time, so a turn holds its connections for a while.

    The same device ``tests/test_web_chat_api.py`` uses, and the same thing phase 3 made
    ordinary: a turn with model calls in it is seconds long.
    """

    async def probe(point: str, detail: dict[str, Any]) -> None:
        if point == "after_checkpoint":
            await asyncio.sleep(seconds)

    return EngineHooks(probe=probe)


@pytest.fixture
async def pooled_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """The engine a deployment actually gets, pool and all.

    Depends on ``engine`` for the truncation it does, and then ignores it: what is under test is
    the pool :func:`~support_core.storage.session.make_engine` configures, so the test has to use
    that function rather than the fixture's ``NullPool``.
    """
    pooled = make_engine(db_config.test_database_url())
    try:
        yield pooled
    finally:
        await pooled.dispose()


async def pending_inbound(engine: AsyncEngine) -> int:
    """Customer messages nobody has processed."""
    async with make_session_factory(engine)() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.direction == "inbound", Message.status == "pending")
        )
        return int(total or 0)


async def flood(client: httpx.AsyncClient, count: int) -> list[httpx.Response]:
    """``count`` messages, each on its own conversation, all issued together."""
    return list(
        await asyncio.gather(
            *(
                client.post(
                    "/channels/web_chat/messages",
                    json={"session": f"flood-{index:04d}", "text": "hello"},
                    timeout=120.0,
                )
                for index in range(count)
            )
        )
    )


async def test_a_flood_of_concurrent_customers_is_answered_rather_than_refused(
    pooled_engine: AsyncEngine,
) -> None:
    """No 5xx, whatever the pool is doing.

    A caller over the bound gets the answer the system already had for a conversation it could
    not lock: ``202 queued``, with the message durable and the drain worker holding it. A caller
    that cannot be served at all would get a 503; what it must never get is a pool timeout
    rendered as an unhandled exception.
    """
    app = build_app(QUEUE_PACK, pooled_engine, hooks=slow_hooks(SLOW_TURN))
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        responses = await flood(client, FLOOD)

    codes = [response.status_code for response in responses]
    assert not [code for code in codes if code >= 500], f"{codes.count(500)} of {FLOOD} failed"
    assert set(codes) <= {200, 202, 503}
    assert codes.count(200) + codes.count(202) == FLOOD, codes


async def test_healthz_keeps_answering_while_the_flood_is_in_flight(
    pooled_engine: AsyncEngine,
) -> None:
    """A health endpoint that queues behind the turns is a restart signal, not a health check."""
    app = build_app(QUEUE_PACK, pooled_engine, hooks=slow_hooks(SLOW_TURN))
    latencies: list[float] = []
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        posts = asyncio.create_task(flood(client, FLOOD))
        while not posts.done():
            began = time.perf_counter()
            health = await client.get("/healthz", timeout=120.0)
            latencies.append(time.perf_counter() - began)
            assert health.status_code < 500 or health.status_code == 503
            await asyncio.sleep(0.05)
        await posts

    assert latencies
    worst = max(latencies)
    assert worst < HEALTH_BUDGET, f"/healthz took {worst:.2f}s while the flood was in flight"


async def test_no_customer_message_is_left_pending_after_a_flood(
    pooled_engine: AsyncEngine,
) -> None:
    """The row is written before the lock is tried, so a refused turn still owes an answer."""
    app = build_app(QUEUE_PACK, pooled_engine, hooks=slow_hooks(SLOW_TURN))
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        await flood(client, FLOOD)
        runtime = app.state.runtime
        assert await runtime.drainer.wait_until_idle(timeout=180.0)

    assert await pending_inbound(pooled_engine) == 0


async def test_over_the_bound_a_message_is_queued_and_still_answered(
    pooled_engine: AsyncEngine,
) -> None:
    """The bound itself, at a size small enough to be certain it is the thing being measured.

    Two turns at a time, ten callers. Eight of them are over the bound and must come back
    ``202 queued`` at once rather than waiting for a connection - and every one of their messages
    must have been processed by the time the drain worker goes idle.
    """
    config = AppConfig(pack=QUEUE_PACK, provider="none", max_concurrent_turns=2)
    app = build_app(QUEUE_PACK, pooled_engine, config=config, hooks=slow_hooks(SLOW_TURN))
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        began = time.perf_counter()
        responses = await flood(client, 10)
        elapsed = time.perf_counter() - began
        runtime = app.state.runtime
        assert await runtime.drainer.wait_until_idle(timeout=180.0)

    codes = [response.status_code for response in responses]
    assert not [code for code in codes if code >= 500], codes
    assert codes.count(202) >= 8, codes
    # Nobody waited for a slot: the eight over the bound were answered while two turns ran.
    assert elapsed < SLOW_TURN * 3, f"the flood took {elapsed:.2f}s"
    assert await pending_inbound(pooled_engine) == 0


# -- the bound itself, without a database ----------------------------------------------------


async def test_a_turn_slot_is_never_a_wait() -> None:
    """Over the bound, ``hold`` says no at once. Waiting is the thing being removed."""
    slots = TurnSlots(limit=2)
    async with slots.hold() as first, slots.hold() as second:
        assert (first, second) == (True, True)
        began = time.perf_counter()
        async with slots.hold() as third:
            assert third is False
        assert time.perf_counter() - began < 0.1
        assert slots.refused == 1
    assert slots.in_use == 0


async def test_a_slot_is_released_even_when_the_turn_raises() -> None:
    """A turn that fails must not consume a slot for the life of the process."""
    slots = TurnSlots(limit=1)
    with pytest.raises(RuntimeError):
        async with slots.hold() as taken:
            assert taken
            raise RuntimeError("the node fell over")
    assert slots.in_use == 0
    async with slots.hold() as again:
        assert again


async def test_no_bound_is_a_real_configuration() -> None:
    """The engine's own tests run without one, and a caller may supply its own strategy."""
    slots = TurnSlots(limit=0)
    async with slots.hold() as a, slots.hold() as b, slots.hold() as c:
        assert (a, b, c) == (True, True, True)


# -- the boundary: a pool timeout is an answer, not a traceback ------------------------------


async def test_a_pool_timeout_is_a_503_with_a_retry_after(engine: AsyncEngine) -> None:
    """Finding S2's worst symptom: an unhandled ``sqlalchemy.exc.TimeoutError`` reaching an
    anonymous caller as a 500 with a full traceback, thirty seconds after they asked."""
    app = build_app(QUEUE_PACK, engine)

    async def no_connection(*args: Any, **kwargs: Any) -> None:
        raise SQLTimeoutError("QueuePool limit of size 5 overflow 10 reached")

    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        app.state.runtime.executor.on_inbound = no_connection  # type: ignore[method-assign]
        response = await client.post(
            "/channels/web_chat/messages", json={"session": "refused-01", "text": "hello"}
        )

    # Every other route is covered by the same answer, registered on the application rather
    # than repeated in each handler, so the desk and anything phase 7 mounts get it too.
    assert SQLTimeoutError in app.exception_handlers
    assert response.status_code == 503
    assert response.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    body = response.json()
    assert body["error"] == OVERLOADED_DETAIL
    # Nothing internal in it: no exception name, no pool, no SQL.
    for leak in ("QueuePool", "TimeoutError", "sqlalchemy", "Traceback"):
        assert leak not in response.text
