"""Who may reach the human desk. DESIGN.md sections 12 and 13, and phase W review finding W1.

    **Human desk**: not a customer channel but uses the same API surface to inject human
    replies and to resume or close. - DESIGN.md section 12

"Not a customer channel" is the whole of this file. The desk lists every conversation in the
deployment, reads any transcript, and writes into any of them; the customer chat is served by
the same application on the same port. Until the fix these tests pin, the two were the same
surface with no credential between them, so the browser that opened the demo page could read
somebody else's refund.

The attack here is the reviewer's own (reviews/phase-w.md, attempt A12), and every test in this
file failed against the code it was written against:

* ``/desk/handoffs`` enumerated every conversation in the database,
* ``/desk/conversations/{id}/transcript`` returned another customer's whole transcript,
* ``/desk/handoffs/{id}/reply`` wrote into another customer's conversation,

with no credential of any kind, from a deployment that had configured nothing.
"""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.routing import Router

from support_core.api import AppConfig
from support_core.api.config import ConfigError
from tests.app_support import (
    ACME,
    CASSETTES,
    build_app,
    reset_acme_backend,
    serving,
    sync_acme_knowledge,
)


def api_routes(app: FastAPI) -> list[APIRoute]:
    """Every HTTP route the application serves, including nested routers.

    FastAPI mounts an included router as a router rather than flattening its routes, so
    ``app.routes`` alone would report that this application has no desk endpoints whether or not
    it serves six of them - which is the opposite of what this file is for.
    """
    found: list[APIRoute] = []
    pending: list[Any] = list(app.routes)
    while pending:
        route = pending.pop()
        if isinstance(route, APIRoute):
            found.append(route)
        inner = getattr(route, "routes", None)
        if inner:
            pending.extend(inner)
        nested = getattr(route, "original_router", None) or getattr(route, "app", None)
        if isinstance(nested, Router):
            pending.extend(nested.routes)
    return found


ACCOUNT_QUESTION = "Why was I charged 40 dollars on the 3rd?"
"""The sample pack's question with no workflow, which is what raises a handoff."""

DESK_TOKEN = "desk-token-for-the-tests-0001"
WRONG_TOKEN = "desk-token-for-the-tests-0002"

DESK_AUTH = {"Authorization": f"Bearer {DESK_TOKEN}"}


def desk_config(**overrides: Any) -> AppConfig:
    """A deployment that has deliberately turned the desk on, with a credential."""
    return AppConfig(
        pack=ACME,
        provider="replay",
        cassette_dir=CASSETTES,
        serve_client=False,
        serve_desk=True,
        desk_token=DESK_TOKEN,
        new_conversation_context={
            "customer": {"ref": "cus_acme_1", "name": "Sam", "email": "me@example.com"}
        },
    ).model_copy(update=dict(overrides))


@pytest.fixture(autouse=True)
async def _fresh_backend(engine: AsyncEngine) -> None:
    """Seed account and corpus. The desk tests drive a real conversation, and phase 5 gave the
    sample pack knowledge that its `llm` nodes now answer from."""
    reset_acme_backend()
    await sync_acme_knowledge(engine)


async def _a_customer_conversation(host: str) -> tuple[str, str]:
    """Drive one customer to a handoff over the *customer* surface. Returns ids.

    No credential is used here and none should be needed: this is the channel, and the customer
    is talking about their own conversation.
    """
    async with httpx.AsyncClient(base_url=f"http://{host}") as client:
        posted = await client.post(
            "/channels/web_chat/messages",
            json={"session": "desk-auth-victim-01", "text": ACCOUNT_QUESTION},
            timeout=90.0,
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["status"] == "waiting_human"
        conversation_id = str(posted.json()["conversation_id"])
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as desk:
        listed = await desk.get("/desk/handoffs")
        assert listed.status_code == 200, listed.text
        handoffs = listed.json()["handoffs"]
        assert len(handoffs) == 1, handoffs
    return conversation_id, str(handoffs[0]["id"])


# -- a deployment that configures nothing --------------------------------------------------


async def test_a_deployment_that_configures_nothing_serves_no_desk(engine: AsyncEngine) -> None:
    """The default is off, so a deployment nobody configured cannot serve customer data.

    ``build_app`` here passes the same ``AppConfig`` an operator gets by writing nothing: the
    pack, and the provider. Under the reviewed code that application answered
    ``GET /desk/handoffs`` with 200 and the whole queue.
    """
    app = build_app(ACME, engine)
    routes = [route.path for route in api_routes(app)]
    assert "/healthz" in routes, routes
    assert not [path for path in routes if path.startswith("/desk")], routes
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        assert (await client.get("/desk/handoffs")).status_code == 404
        assert (await client.get(f"/desk/conversations/{uuid.uuid4()}/transcript")).status_code == (
            404
        )


def test_the_desk_cannot_be_turned_on_without_a_credential() -> None:
    """A missing credential is a refusal to start, not a desk anybody can read.

    The failure mode is the point of the finding: a deployment that turns the desk on and
    forgets the token must not get an open desk, and it must find out at startup rather than
    when somebody enumerates its customers.
    """
    with pytest.raises(ConfigError, match="desk_token"):
        AppConfig(pack=ACME, serve_desk=True).build_desk_credential()


def test_a_credential_too_short_to_be_one_is_refused() -> None:
    with pytest.raises(ConfigError, match="at least"):
        AppConfig(pack=ACME, serve_desk=True, desk_token="short").build_desk_credential()


# -- the reviewer's attack, A12 ------------------------------------------------------------


async def test_an_unauthenticated_client_cannot_enumerate_conversations(
    engine: AsyncEngine,
) -> None:
    """Reviewer attempt A12, first half: the queue is not a public list."""
    app = build_app(ACME, engine, config=desk_config())
    async with serving(app) as host:
        conversation_id, handoff_id = await _a_customer_conversation(host)
        async with httpx.AsyncClient(base_url=f"http://{host}") as attacker:
            listed = await attacker.get("/desk/handoffs")
            read = await attacker.get(f"/desk/handoffs/{handoff_id}")

    assert listed.status_code == 401, listed.text
    assert listed.headers.get("www-authenticate") == "Bearer"
    assert conversation_id not in listed.text
    assert handoff_id not in listed.text
    assert read.status_code == 401, read.text
    assert conversation_id not in read.text


async def test_an_unauthenticated_client_cannot_read_another_customers_transcript(
    engine: AsyncEngine,
) -> None:
    """Reviewer attempt A12, second half: the transcript endpoint is the desk's, not the web's.

    The attacker is given the conversation id outright, which is the strongest form of the
    attack: knowing which conversation to ask for must still not be enough.
    """
    app = build_app(ACME, engine, config=desk_config())
    async with serving(app) as host:
        conversation_id, _ = await _a_customer_conversation(host)
        async with httpx.AsyncClient(base_url=f"http://{host}") as attacker:
            transcript = await attacker.get(f"/desk/conversations/{conversation_id}/transcript")

    assert transcript.status_code == 401, transcript.text
    assert ACCOUNT_QUESTION not in transcript.text


async def test_an_unauthenticated_client_cannot_write_into_another_conversation(
    engine: AsyncEngine,
) -> None:
    """The write half of A12, which is worse than the read half.

    ``reply`` puts words into somebody else's transcript in the agent's voice, ``resume`` and
    ``close`` move their run, and ``approve`` is the *human* half of
    ``requires_human_approval`` - so an unauthenticated caller could countersign their own
    action, which is the one property a second signature exists to have.
    """
    app = build_app(ACME, engine, config=desk_config())
    async with serving(app) as host:
        conversation_id, handoff_id = await _a_customer_conversation(host)
        async with httpx.AsyncClient(base_url=f"http://{host}") as attacker:
            replied = await attacker.post(
                f"/desk/handoffs/{handoff_id}/reply", json={"text": "your refund is approved"}
            )
            resumed = await attacker.post(f"/desk/handoffs/{handoff_id}/resume", json={})
            closed = await attacker.post(f"/desk/handoffs/{handoff_id}/close", json={})
            approved = await attacker.post(f"/desk/handoffs/{handoff_id}/approve", json={})
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as desk:
            after = await desk.get(f"/desk/conversations/{conversation_id}/transcript")

    for response in (replied, resumed, closed, approved):
        assert response.status_code == 401, response.text
    assert "your refund is approved" not in after.text
    assert [row["author"] for row in after.json()["messages"]] == ["customer", "agent"]


async def test_a_wrong_or_malformed_credential_is_refused(engine: AsyncEngine) -> None:
    """Wrong, empty, the wrong scheme, and the token in the query string: all refusals."""
    app = build_app(ACME, engine, config=desk_config())
    attempts = [
        {"Authorization": f"Bearer {WRONG_TOKEN}"},
        {"Authorization": "Bearer"},
        {"Authorization": DESK_TOKEN},
        {"Authorization": f"Basic {DESK_TOKEN}"},
        {},
    ]
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        refused = [(await client.get("/desk/handoffs", headers=headers)) for headers in attempts]
        allowed = await client.get("/desk/handoffs", headers=DESK_AUTH)
        by_query = await client.get(f"/desk/handoffs?token={DESK_TOKEN}")

    assert [response.status_code for response in refused] == [401] * len(attempts)
    assert allowed.status_code == 200, allowed.text
    assert by_query.status_code == 401, "a credential in a URL is logged; it is not a credential"


async def test_every_desk_route_is_behind_the_credential(engine: AsyncEngine) -> None:
    """Enumerated from the router, so a route added later cannot arrive unguarded.

    This is the test that would have caught W1 when phase 6 mounted the desk: it does not name
    the six endpoints, it asks the application which ones it serves.
    """
    app = build_app(ACME, engine, config=desk_config())
    desk_routes = [
        (route.path, method)
        for route in api_routes(app)
        if route.path.startswith("/desk")
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"})
    ]
    assert len(desk_routes) >= 6, desk_routes

    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        results = {}
        for path, method in desk_routes:
            url = path.format(handoff_id=uuid.uuid4(), conversation_id=uuid.uuid4())
            results[f"{method} {path}"] = (await client.request(method, url, json={})).status_code

    assert set(results.values()) == {401}, results
