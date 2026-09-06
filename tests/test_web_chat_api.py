"""The HTTP half of the web chat channel: health, the client page, and the webhook that must
never hold a connection while somebody else holds the conversation lock.

The webhook is where phase 2's review finding R7 is settled for the customer path:

    A burst on one conversation stalls a connection per waiter... Add a queue-and-return mode
    (``lock_wait_seconds=0`` plus a poller calling ``drain``) and make it the default for
    channel webhooks. - reviews/phase-2.md
"""

import asyncio
import time
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api import AppConfig
from support_core.engine.hooks import EngineHooks
from tests.app_support import TEST_PACKS, build_app, serving

QUEUE_PACK = TEST_PACKS / "queue_pack"

SLOW_TURN = 1.5
"""How long a deliberately slow turn holds the conversation lock. Long enough that a caller
that waited for the lock could not possibly look prompt."""


def slow_hooks(seconds: float, started: asyncio.Event | None = None) -> EngineHooks:
    """Hooks whose checkpoints take their time, so a turn holds the lock for a while.

    This is what phase 3 made ordinary - a turn with model calls in it is seconds long - without
    needing a model here.
    """

    async def probe(point: str, detail: dict[str, Any]) -> None:
        if point == "after_checkpoint":
            if started is not None:
                started.set()
            await asyncio.sleep(seconds)

    return EngineHooks(probe=probe)


async def test_the_health_endpoint_names_the_pack_and_the_provider(engine: AsyncEngine) -> None:
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["pack"]["id"] == "queue-pack"
    assert body["pack"]["fingerprint"]
    assert body["provider"] == "none"
    assert body["channels"] == ["web_chat"]
    assert body["database"] == "ok"


async def test_the_client_page_is_served_and_needs_no_network(engine: AsyncEngine) -> None:
    """Plain HTML, CSS and JavaScript from this service and nowhere else: the demo has to work
    on a laptop with no route to the internet."""
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        page = await client.get("/")
        script = await client.get("/static/app.js")
        style = await client.get("/static/app.css")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    for asset in (script, style):
        assert asset.status_code == 200
    body = page.text
    assert "//fonts." not in body
    assert "https://" not in body
    assert "http://" not in body


async def test_a_message_posted_to_the_webhook_runs_a_turn(engine: AsyncEngine) -> None:
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        first = await client.post(
            "/channels/web_chat/messages", json={"session": "webhook-0001", "text": "hello"}
        )
        second = await client.post(
            "/channels/web_chat/messages",
            json={"session": "webhook-0001", "text": "something worth echoing"},
        )

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert first.json()["status"] == "waiting_customer"
    assert second.json()["created"] is False
    assert second.json()["status"] == "done"
    assert second.json()["conversation_id"] == first.json()["conversation_id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "no session"},
        {"session": "webhook-0001"},
        {"session": "short", "text": "hi"},
        {"session": "webhook-0001", "text": "hi", "customer_ref": "cus_someone_else"},
    ],
)
async def test_a_payload_that_is_not_a_message_is_refused(
    engine: AsyncEngine, payload: dict[str, Any]
) -> None:
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        response = await client.post("/channels/web_chat/messages", json=payload)

    assert response.status_code == 400
    assert "error" in response.json()


async def test_a_second_message_returns_promptly_while_a_turn_holds_the_lock(
    engine: AsyncEngine,
) -> None:
    """Finding R7, as a measurement.

    The first request is inside a turn that holds the conversation's advisory lock for
    ``SLOW_TURN`` seconds. The second request arrives on a different connection, for the *same*
    conversation. Before queue-and-return it would have blocked on the lock for the whole turn,
    holding its connection; now it is answered at once with ``queued``, and the drain worker
    runs its message afterwards.
    """
    started = asyncio.Event()
    app = build_app(QUEUE_PACK, engine, hooks=slow_hooks(SLOW_TURN, started))
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        slow = asyncio.create_task(
            client.post(
                "/channels/web_chat/messages",
                json={"session": "contended-01", "text": "first"},
                timeout=60.0,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=10.0)

        began = time.perf_counter()
        second = await client.post(
            "/channels/web_chat/messages",
            json={"session": "contended-01", "text": "second"},
            timeout=60.0,
        )
        waited = time.perf_counter() - began

        assert second.status_code == 202
        assert second.json()["queued"] is True
        assert waited < SLOW_TURN / 2, f"the handler waited {waited:.2f}s for the lock"

        first = await slow
        assert first.status_code == 200

        runtime = app.state.runtime
        assert await runtime.drainer.wait_until_idle(timeout=60.0)
        state = await runtime.state(second.json()["conversation_id"])

    # Nothing was dropped by returning early: the queued message was processed, in order, by
    # the drain worker.
    said = [entry["text"] for entry in state.history if entry["author"] == "customer"]
    assert said == ["first", "second"]
    assert runtime.drainer.stats.drained >= 1


async def test_concurrent_clients_on_different_conversations_do_not_serialise(
    engine: AsyncEngine,
) -> None:
    """The lock is per conversation (DESIGN.md section 17), and the HTTP layer must not put a
    narrower one in front of it."""
    clients = 5
    app = build_app(QUEUE_PACK, engine, hooks=slow_hooks(SLOW_TURN))
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        began = time.perf_counter()
        responses = await asyncio.gather(
            *(
                client.post(
                    "/channels/web_chat/messages",
                    json={"session": f"parallel-{index:04d}", "text": "hello"},
                    timeout=120.0,
                )
                for index in range(clients)
            )
        )
        elapsed = time.perf_counter() - began

    assert [response.status_code for response in responses] == [200] * clients
    assert len({response.json()["conversation_id"] for response in responses}) == clients
    assert all(response.json()["queued"] is False for response in responses)
    # Serialised, this would be at least clients * SLOW_TURN.
    assert elapsed < SLOW_TURN * (clients - 1), f"{clients} conversations took {elapsed:.2f}s"


async def test_the_client_page_can_be_turned_off(engine: AsyncEngine) -> None:
    """A deployment with its own front end serves the channel and not the demo."""
    config = AppConfig(pack=QUEUE_PACK, provider="none", serve_client=False)
    app = build_app(QUEUE_PACK, engine, config=config)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        page = await client.get("/")
        health = await client.get("/healthz")

    assert page.status_code == 404
    assert health.status_code == 200
