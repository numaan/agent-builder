"""The AG-UI transport over the web chat channel: the SSE endpoint and its event mapping.

This mirrors the exit criterion of :mod:`tests.test_web_chat_socket` over the AG-UI protocol
instead of the WebSocket - the same recorded refund conversation, asserted as AG-UI events. The
identity gate is a ``STATE_SNAPSHOT`` with a ``question``; the refund proposal is both a
``STATE_SNAPSHOT`` with a ``confirm`` and a ``TOOL_CALL`` naming ``issue_refund``; and underneath
the events, exactly one refund moves in the billing system. Implements
:mod:`support_core.channels.ag_ui`.

Everything runs through the real executor and real Postgres, with the recorded (``replay``)
provider, so a change to prompt assembly fails loudly rather than quietly re-deciding the
conversation - the same discipline as the socket and refund-flow tests.
"""

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api import AppConfig
from support_core.channels.ag_ui import RunTranslator
from tests.app_support import (
    ACME,
    CASSETTES,
    DEMO_CONFIG,
    TEST_PACKS,
    build_app,
    reset_acme_backend,
    serving,
    sync_acme_knowledge,
)
from tests.cassettes.scenarios import ACME_REFUND

QUEUE_PACK = TEST_PACKS / "queue_pack"
REFUND_ASK, OTP_REPLY, APPROVE, FINISH = ACME_REFUND.turns


def acme_config() -> AppConfig:
    """The demo's own configuration, so the tests and the demo cannot drift apart."""
    config = AppConfig.from_file(DEMO_CONFIG)
    return config.model_copy(update={"pack": ACME, "cassette_dir": CASSETTES})


async def run_agui(
    client: httpx.AsyncClient,
    text: str,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """One AG-UI run: POST the message, collect the SSE events until the stream closes."""
    body: dict[str, Any] = {"messages": [{"id": uuid.uuid4().hex, "role": "user", "content": text}]}
    if thread_id is not None:
        body["threadId"] = thread_id
    if run_id is not None:
        body["runId"] = run_id
    events: list[dict[str, Any]] = []
    # A turn is seconds and runs several recorded model calls; well past httpx's 5s default.
    async with client.stream("POST", "/channels/ag_ui", json=body, timeout=60.0) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def said(events: list[dict[str, Any]]) -> str:
    """Everything the assistant said this run, joined."""
    return " ".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")


def snapshots(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e["snapshot"] for e in events if e["type"] == "STATE_SNAPSHOT"]


def tools_called(events: list[dict[str, Any]]) -> list[str]:
    return [e["toolCallName"] for e in events if e["type"] == "TOOL_CALL_START"]


# -- the info endpoint ---------------------------------------------------------------------


async def test_the_info_endpoint_names_the_provider_and_the_suggestions(
    engine: AsyncEngine,
) -> None:
    """What an AG-UI client reads before its first run - the same static bits the socket's
    ``ready`` frame carries, and nothing a conversation could leak."""
    app = build_app(ACME, engine, config=acme_config())
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        response = await client.get("/channels/ag_ui")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "replay"
    assert body["pack"] == "acme-billing"
    assert body["suggestions"][0] == REFUND_ASK


async def test_the_ag_ui_client_page_is_served(engine: AsyncEngine) -> None:
    app = build_app(ACME, engine, config=acme_config())
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        page = await client.get("/agui")
        script = await client.get("/static/agui.js")

    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert script.status_code == 200
    # No network: the demo works on a laptop with no route out (as for the socket page).
    for body in (page.text, script.text):
        assert "https://" not in body
        assert "http://" not in body


# -- the exit criterion, as AG-UI events ---------------------------------------------------


async def test_the_refund_conversation_runs_as_ag_ui_events(engine: AsyncEngine) -> None:
    """The refund conversation of the socket exit criterion, over the AG-UI SSE endpoint.

    Every assertion is about an AG-UI event a front end would render: the identity check as a
    ``question`` state, the proposal as a ``confirm`` state *and* a ``TOOL_CALL`` naming the tool,
    the refund reported, and - underneath - exactly one refund authorised by one approval.
    """
    reset_acme_backend()
    await sync_acme_knowledge(engine)
    app = build_app(ACME, engine, config=acme_config())
    thread = "agui-refund-0001"
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        asked = await run_agui(client, REFUND_ASK, thread_id=thread)
        assert kinds(asked)[0] == "RUN_STARTED"
        assert kinds(asked)[-1] == "RUN_FINISHED"
        assert asked[0]["threadId"] == thread
        assert "six-digit code" in said(asked)
        assert snapshots(asked)[-1]["awaiting"] == {
            "kind": "question",
            "node": "ask_code",
            "tool": None,
        }
        assert tools_called(asked) == []

        proposed = await run_agui(client, OTP_REPLY, thread_id=thread)
        assert "made a note" not in said(proposed)  # the simple flow, no interrupt
        assert "29.00" in said(proposed)
        assert "Shall I go ahead?" in said(proposed)
        confirm = snapshots(proposed)[-1]["awaiting"]
        assert confirm == {"kind": "confirm", "node": "confirm_refund", "tool": "issue_refund"}
        assert tools_called(proposed) == ["issue_refund"]
        # The tool-call args carry what the customer was shown, so a front end can render the
        # approval - and never the argument hash (see AwaitingSummary).
        args = "".join(e["delta"] for e in proposed if e["type"] == "TOOL_CALL_ARGS")
        assert "29.00" in json.loads(args)["proposal"]

        refunded = await run_agui(client, APPROVE, thread_id=thread)
        assert "refunded" in said(refunded)
        assert snapshots(refunded)[-1]["awaiting"]["kind"] == "question"
        assert tools_called(refunded) == []

        finished = await run_agui(client, FINISH, thread_id=thread)
        assert snapshots(finished)[-1]["status"] == "done"
        assert snapshots(finished)[-1]["awaiting"] is None
        assert tools_called(finished) == []

    from support_core.tools.loading import import_pack_tools

    billing = import_pack_tools(ACME).BILLING
    assert billing.executed == ["issue_refund:ch_1002:29.0"], "one refund, not none and not two"
    assert app.state.runtime.send_failures == []


async def test_a_run_without_a_thread_id_is_given_one(engine: AsyncEngine) -> None:
    """A client that brings no thread id is told the server's, in ``RUN_STARTED`` - the AG-UI
    equivalent of the socket being handed a session key (finding W9 keeps it out of the URL)."""
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        events = await run_agui(client, "hello")

    started = events[0]
    assert started["type"] == "RUN_STARTED"
    assert isinstance(started["threadId"], str) and len(started["threadId"]) >= 8
    assert kinds(events)[-1] == "RUN_FINISHED"


async def test_a_thread_id_continues_one_conversation(engine: AsyncEngine) -> None:
    """Two runs on one thread are one conversation: the second turn sees the first (the queue
    pack echoes only after it has been told to say something)."""
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        first = await run_agui(client, "hello", thread_id="agui-thread-0001")
        second = await run_agui(client, "something worth echoing", thread_id="agui-thread-0001")

    assert "Say something." in said(first)
    assert "You said something worth echoing." in said(second)


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"messages": []}, "no user message"),
        ({"messages": [{"role": "assistant", "content": "hi"}]}, "no user-authored message"),
        (
            {"threadId": "short", "messages": [{"role": "user", "content": "hi"}]},
            "a thread id that is not a valid session key",
        ),
    ],
)
async def test_a_run_that_cannot_start_is_refused(
    engine: AsyncEngine, payload: dict[str, Any], why: str
) -> None:
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        response = await client.post("/channels/ag_ui", json=payload)

    assert response.status_code == 400, why
    assert "error" in response.json()


async def test_a_body_that_is_not_json_is_refused(engine: AsyncEngine) -> None:
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        response = await client.post(
            "/channels/ag_ui",
            content=b"this is not json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert "error" in response.json()


# -- the translator, in isolation ----------------------------------------------------------


def test_the_translator_maps_a_confirm_gate_to_a_tool_call() -> None:
    """A ``confirm`` turn becomes a state snapshot and a tool call naming the proposed tool, with
    the shown proposal as its args - the AG-UI human-in-the-loop shape."""
    translator = RunTranslator("thread-x", "run-x")
    message = translator.translate(
        {"type": "message", "author": "agent", "text": "I can refund 29.00 USD. Shall I go ahead?"}
    )
    assert [e["type"] for e in message] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]

    turn = translator.translate(
        {
            "type": "turn",
            "status": "waiting_customer",
            "awaiting": {"kind": "confirm", "node": "confirm_refund", "tool": "issue_refund"},
        }
    )
    assert [e["type"] for e in turn] == [
        "STATE_SNAPSHOT",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
    ]
    assert turn[1]["toolCallName"] == "issue_refund"
    assert json.loads(turn[2]["delta"])["proposal"] == "I can refund 29.00 USD. Shall I go ahead?"
    assert translator.done is True


def test_the_translator_maps_a_question_gate_to_state_only() -> None:
    """A ``question`` turn is a state snapshot and nothing else: there is no action to approve."""
    translator = RunTranslator("thread-x", "run-x")
    turn = translator.translate(
        {
            "type": "turn",
            "status": "waiting_customer",
            "awaiting": {"kind": "question", "node": "ask_code", "tool": None},
        }
    )
    assert [e["type"] for e in turn] == ["STATE_SNAPSHOT"]
    assert turn[0]["snapshot"]["awaiting"]["kind"] == "question"
    assert translator.done is True
