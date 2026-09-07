"""The WebSocket half of the web chat channel, and the phase W exit criterion.

    ``create_app(load_pack("packs/acme_billing"))`` starts, and a browser client completes the
    phase-4 refund flow end to end, including the confirmation step, with the recorded
    provider. - BACKLOG.md, phase W

These tests drive a real server over a real socket. The refund conversation is the one from
``tests/cassettes/scenarios.py``: the same four customer messages, the same recorded responses,
the same in-memory billing system - so what they prove is that the *channel* delivers the phase-4
conversation, not that some other conversation happens to work.
"""

import asyncio
import json
import uuid

import httpx
import pytest
import websockets.exceptions
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api import AppConfig
from support_core.engine.runners import DEFAULT_HANDOFF_MESSAGE
from support_core.storage import repositories as repo
from tests.app_support import (
    ACME,
    CASSETTES,
    DEMO_CONFIG,
    TEST_PACKS,
    build_app,
    chatting,
    reset_acme_backend,
    serving,
    sync_acme_knowledge,
)
from tests.cassettes.scenarios import ACME_REFUND, SCENARIOS

QUEUE_PACK = TEST_PACKS / "queue_pack"

REFUND_ASK, OTP_REPLY, APPROVE, FINISH = ACME_REFUND.turns


def acme_config() -> AppConfig:
    """The demo's own configuration, so the tests and the demo cannot drift apart."""
    config = AppConfig.from_file(DEMO_CONFIG)
    return config.model_copy(update={"pack": ACME, "cassette_dir": CASSETTES})


# -- the plain round trip ------------------------------------------------------------------


async def test_a_conversation_runs_over_a_websocket(engine: AsyncEngine) -> None:
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, chatting(host, "socket-round-trip") as chat:
        ready = await chat.ready()
        assert ready["session"] == "socket-round-trip"
        assert ready["history"] == []
        assert ready["status"] == "idle"

        first = await chat.say("hello")
        assert chat.messages == ["Say something."]
        assert first["status"] == "waiting_customer"
        assert first["awaiting"]["kind"] == "question"

        second = await chat.say("a thing worth echoing")
        assert chat.messages[-1] == "You said a thing worth echoing."
        assert second["status"] == "done"


async def test_a_client_that_brings_no_session_is_given_one(engine: AsyncEngine) -> None:
    """And connecting alone creates nothing (review finding W8).

    A socket used to write a durable ``conversation`` and ``run`` before the customer had said
    anything, from an unauthenticated endpoint with no rate limit, so a loop of connects filled
    two tables. A connect now resolves a key and reports an empty conversation; the first
    message is what creates one.
    """
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host:
        async with chatting(host) as chat:
            ready = await chat.ready()
        async with engine.connect() as connection:
            after_connecting = await connection.scalar(
                sql_text("SELECT count(*) FROM conversation")
            )

        async with chatting(host, ready["session"]) as again:
            await again.ready()
            await again.say("hello")
        async with engine.connect() as connection:
            after_speaking = await connection.scalar(sql_text("SELECT count(*) FROM conversation"))

    assert ready["session"]
    assert ready["conversation_id"] is None
    assert ready["history"] == []
    assert after_connecting == 0, "connecting is not a conversation"
    assert after_speaking == 1


async def test_a_frame_that_is_not_a_message_is_an_error_not_a_disconnect(
    engine: AsyncEngine,
) -> None:
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, chatting(host, "socket-bad-frames") as chat:
        await chat.ready()
        await chat.send_raw("{not json")
        assert (await chat.recv())["type"] == "error"
        await chat.send_raw(json.dumps({"type": "message", "text": "   "}))
        assert (await chat.recv())["type"] == "error"

        # The socket is still usable, which is the point.
        await chat.say("hello")
        assert chat.messages == ["Say something."]


async def test_a_binary_frame_is_refused_rather_than_crashing_the_handler(
    engine: AsyncEngine,
) -> None:
    """Phase W review finding W2, the reviewer's attempt A7, with their exact input.

    A WebSocket carries text frames and binary frames, and this endpoint speaks JSON text. A
    binary frame used to reach ``socket.receive_text()``, which raised ``KeyError: 'text'`` out
    of the handler: an unhandled exception on the phase's public entry point, an ASGI traceback
    in the log for every frame an anonymous client cared to send, and an abnormal close.

    A hostile client is refused, and told why, and the refusal is orderly: an ``error`` frame
    saying what was wrong, then close code 1003 - "unsupported data", which is what a WebSocket
    says when it will not take the kind of frame it was sent. The server is still serving
    afterwards, which the second connection proves.
    """
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host:
        async with chatting(host, "socket-binary-frame") as chat:
            await chat.ready()
            await chat.socket.send(b"\x00\x01\x02\x03")
            refusal = await chat.recv()
            with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                await asyncio.wait_for(chat.socket.recv(), timeout=30.0)

        # Whatever the last client did, the next one is served.
        async with chatting(host, "socket-after-binary") as after:
            await after.ready()
            await after.say("hello")
            assert after.messages == ["Say something."]

    assert refusal["type"] == "error"
    assert refusal["fatal"] is True
    assert "binary" in refusal["detail"]
    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 1003, closed.value.rcvd


async def test_a_turn_that_fails_still_says_something_to_the_customer(
    engine: AsyncEngine,
) -> None:
    """Phase W review finding W6, from the reviewer's attempt A8.

    Sending an unrecorded message to the sample pack is a cassette miss, which is a node error,
    which DESIGN.md section 7.3 routes to a handoff. The reviewer did that and got a status
    change on the socket and **no message at all**: the run was parked for a human and the
    customer had been told nothing. The next message was answered ("it is with one of our
    people", phase 6's finding P6), which made the silence look like a decision. It was not.

    The turn that raises the handoff now says the same sentence a ``handoff`` node says. What it
    must not say is what went wrong - the failure detail belongs in the packet a person reads,
    not in the customer's transcript.
    """
    reset_acme_backend()
    await sync_acme_knowledge(engine)
    app = build_app(ACME, engine, config=acme_config())
    async with serving(app) as host, chatting(host, "failing-turn-01") as chat:
        await chat.ready()
        turn = await chat.say("please recite the fourteenth verse of the shipping policy")

    assert turn["status"] == "waiting_human"
    assert chat.messages, "the failing turn said nothing at all"
    assert chat.messages[-1] == DEFAULT_HANDOFF_MESSAGE
    said = " ".join(chat.messages)
    assert "llm_unavailable" not in said
    assert "cassette" not in said.lower()
    assert "Traceback" not in said


async def test_a_socket_speaks_only_for_its_own_conversation(engine: AsyncEngine) -> None:
    """A frame that names another session is not a way into that conversation: the session is
    the connection's, fixed when the socket opened."""
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host:
        async with chatting(host, "socket-victim-01") as victim:
            await victim.ready()
            victim_turn = await victim.say("hello")
        async with chatting(host, "socket-attacker") as attacker:
            await attacker.ready()
            await attacker.socket.send(
                json.dumps({"type": "message", "text": "hi", "session": "socket-victim-01"})
            )
            turn = await attacker.recv()
            while turn["type"] != "turn":
                turn = await attacker.recv()

    assert turn["type"] == "turn"
    assert turn["conversation_id"] != victim_turn["conversation_id"]


# -- the exit criterion --------------------------------------------------------------------


async def test_the_refund_conversation_runs_through_the_socket_with_the_confirmation(
    engine: AsyncEngine,
) -> None:
    """The phase W exit criterion, over the real HTTP and WebSocket path.

    Every assertion is about something a person watching the browser would see: the identity
    check happening before anything else, the proposal being flagged as needing approval and
    naming the tool it would run, the refund being reported, and - underneath it - exactly one
    refund in the billing system, authorised by one approval.
    """
    reset_acme_backend()
    await sync_acme_knowledge(engine)
    app = build_app(ACME, engine, config=acme_config())
    async with serving(app) as host, chatting(host, "refund-demo-0001") as chat:
        ready = await chat.ready()
        assert ready["provider"] == "replay"
        assert ready["suggestions"][0] == REFUND_ASK

        asked = await chat.say(REFUND_ASK)
        assert asked["status"] == "waiting_customer"
        assert asked["awaiting"]["kind"] == "question"
        assert "six-digit code" in chat.messages[-1]

        proposed = await chat.say(OTP_REPLY)
        assert proposed["awaiting"]["kind"] == "confirm"
        assert proposed["awaiting"]["tool"] == "issue_refund"
        assert proposed["awaiting"]["node"] == "confirm_refund"
        assert "29.00" in chat.messages[-1]
        assert "Shall I go ahead?" in chat.messages[-1]

        refunded = await chat.say(APPROVE)
        assert refunded["awaiting"]["kind"] == "question"
        assert "refunded" in chat.messages[-2]

        finished = await chat.say(FINISH)
        assert finished["status"] == "done"
        assert finished["awaiting"] is None

        conversation_id = uuid.UUID(finished["conversation_id"])
        runtime = app.state.runtime
        async with runtime.executor.sessions() as session, session.begin():
            run = await repo.load_run(session, conversation_id)
            assert run is not None
            calls = await repo.tool_calls_for_run(session, run.id)
            approvals = await repo.live_approvals(session, run.id)

    from support_core.tools.loading import import_pack_tools

    billing = import_pack_tools(ACME).BILLING
    assert billing.executed == ["issue_refund:ch_1002:29.0"], "one refund, not none and not two"
    assert [call.tool for call in calls if call.tool == "issue_refund"] == ["issue_refund"]
    assert approvals == [], "the approval was consumed by the call it authorised"
    assert runtime.send_failures == []


async def test_a_suspended_conversation_resumes_on_a_second_connection(
    engine: AsyncEngine,
) -> None:
    """DESIGN.md section 7.2 through DESIGN.md section 12: the conversation is the durable key.

    The first socket asks for the refund and is closed while the run waits for the passcode -
    the browser tab was shut, the train went into a tunnel. A second socket, a different
    connection, opens on the same session key, is sent the transcript, and answers the question
    the *first* one was asked. The confirmation then arrives on the second connection.
    """
    reset_acme_backend()
    await sync_acme_knowledge(engine)
    app = build_app(ACME, engine, config=acme_config())
    async with serving(app) as host:
        async with chatting(host, "reconnect-demo-01") as first:
            await first.ready()
            opened = await first.say(REFUND_ASK)
            assert "six-digit code" in first.messages[-1]

        async with chatting(host, "reconnect-demo-01") as second:
            resumed = await second.ready()
            assert resumed["conversation_id"] == opened["conversation_id"]
            assert resumed["status"] == "waiting_customer"
            assert resumed["awaiting"]["node"] == "ask_code"
            assert [entry["author"] for entry in resumed["history"]] == ["customer", "agent"]
            assert resumed["history"][0]["text"] == REFUND_ASK

            proposed = await second.say(OTP_REPLY)
            assert proposed["awaiting"]["kind"] == "confirm"
            assert "Shall I go ahead?" in second.messages[-1]


async def test_two_connections_on_one_conversation_both_see_what_it_says(
    engine: AsyncEngine,
) -> None:
    """Two tabs, one conversation. Delivery is per conversation, not per socket."""
    app = build_app(QUEUE_PACK, engine)
    async with (
        serving(app) as host,
        chatting(host, "two-tabs-0001") as one,
        chatting(host, "two-tabs-0001") as two,
    ):
        first_ready = await one.ready()
        second_ready = await two.ready()
        assert first_ready["session"] == second_ready["session"]

        sender = await one.say("hello")
        watched = await asyncio.wait_for(two.recv(), timeout=30.0)
        # The `turn` frame - the only thing carrying `status` and `awaiting`, and therefore the
        # only thing that raises the approval panel - reaches the second tab too (finding W3).
        watcher = await asyncio.wait_for(two.recv(), timeout=30.0)

    assert one.messages == ["Say something."]
    assert watched["type"] == "message"
    assert watched["text"] == "Say something."
    assert watcher["type"] == "turn"
    assert watcher["status"] == sender["status"] == "waiting_customer"
    assert watcher["awaiting"] == sender["awaiting"]


async def test_a_message_posted_by_webhook_reaches_the_open_socket(engine: AsyncEngine) -> None:
    """The channel is the conversation, not the transport: a reply that arrives by ``POST``
    still speaks to the browser watching it, which is how phase 7's email adapter and a desk
    reply will reach a customer who has a tab open."""
    app = build_app(QUEUE_PACK, engine)
    async with serving(app) as host, chatting(host, "mixed-transport") as chat:
        await chat.ready()
        async with httpx.AsyncClient(base_url=f"http://{host}") as client:
            await client.post(
                "/channels/web_chat/messages",
                json={"session": "mixed-transport", "text": "hello"},
                timeout=60.0,
            )
        event = await chat.recv()

    assert event["type"] == "message"
    assert event["text"] == "Say something."


# -- the demo configuration is the recorded conversation -----------------------------------


def test_the_demo_configuration_matches_the_recorded_conversations() -> None:
    """The demo replays recordings, so its configuration is part of them.

    The customer the conversation starts as, and every phrase the page offers, have to be lines
    some cassette was recorded with: a different email address means a different passcode and a
    different prompt, and a different prompt is a cassette miss. If a future phase re-records a
    scenario, this test is what says the demo has to move with it.

    Phase 6 made the demo's suggestions span three conversations rather than one - the refund,
    the interrupt that defers an address change into it, and the account question that goes to a
    person - because those are the three things there are now to show.
    """
    config = AppConfig.from_file(DEMO_CONFIG)
    assert config.new_conversation_context == ACME_REFUND.context
    assert config.provider == "auto"
    recorded = {turn for scenario in SCENARIOS for turn in scenario.turns}
    unrecorded = [line for line in config.suggestions if line not in recorded]
    assert not unrecorded, f"the demo offers lines no cassette records: {unrecorded}"
    assert config.suggestions[0] == ACME_REFUND.turns[0]
