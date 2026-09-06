"""The human agent desk, over real HTTP. DESIGN.md section 13's desk API.

    The human desk API lets the human reply directly, take over fully (`close`), or hand back
    (`resume` with optional state patch ...). On `resume`, the graph continues from the handoff
    node's `resumed` edge.

Driven against a running server for the reason phase W gives for the web chat tests: the desk is
an HTTP surface and the interesting parts of it - the status codes, the refusals, the fact that a
reply reaches an *open socket* on the customer's side - are what a client actually meets.

The conversation these tests act on is the sample pack's own account question, which is the case
that made the desk necessary: a question the pack has no workflow for, which used to dead-end
with an honest "I cannot do that" and now goes to a person.
"""

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api import AppConfig
from tests.app_support import ACME, CASSETTES, build_app, chatting, reset_acme_backend, serving

ACCOUNT_QUESTION = "Why was I charged 40 dollars on the 3rd?"
"""The message the cassette records an ``account_question`` answer for."""


def _config() -> AppConfig:
    return AppConfig(
        pack=ACME,
        provider="replay",
        cassette_dir=CASSETTES,
        serve_client=False,
        new_conversation_context={
            "customer": {"ref": "cus_acme_1", "name": "Sam", "email": "me@example.com"}
        },
    )


async def _handed_off(host: str, session: str) -> dict[str, Any]:
    """Drive the sample pack to its ``handoff`` node and return the queued row."""
    async with httpx.AsyncClient(base_url=f"http://{host}") as client:
        posted = await client.post(
            "/channels/web_chat/messages", json={"session": session, "text": ACCOUNT_QUESTION}
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["status"] == "waiting_human"
        listed = await client.get("/desk/handoffs")
        assert listed.status_code == 200
        handoffs = listed.json()["handoffs"]
        assert len(handoffs) == 1, handoffs
        return dict(handoffs[0])


@pytest.fixture(autouse=True)
def _fresh_backend() -> None:
    reset_acme_backend()


async def test_the_queue_lists_what_is_waiting_and_why(engine: AsyncEngine) -> None:
    """A desk's first view: which conversations, from which queue, for what reason."""
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-list-0001")

    assert row["queue"] == "billing-tier-1"
    assert row["reason"] == "no_workflow"
    assert row["status"] == "open"
    assert row["workflow"] == "root"
    assert row["node"] == "no_workflow"
    assert row["sla_due_at"], "the pack names an sla_minutes, so the queue has a deadline"


async def test_reading_one_handoff_gives_the_whole_packet(engine: AsyncEngine) -> None:
    """DESIGN.md section 13's packet, as the person who has to act on it receives it."""
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-read-0001")
        async with httpx.AsyncClient(base_url=f"http://{host}") as client:
            response = await client.get(f"/desk/handoffs/{row['id']}")
            assert response.status_code == 200
            packet = response.json()["packet"]

            # And the link it carries is a real one, not a plausible address.
            transcript = await client.get(packet["transcript_url"])
            assert transcript.status_code == 200

    assert packet["reason"] == "no_workflow"
    assert packet["identity_verified"] is False
    assert packet["customer"]["ref"] == "cus_acme_1"
    assert packet["workflow"] == "root"
    assert packet["node"] == "no_workflow"
    assert packet["state_snapshot"]["intent"] == "account_question"
    assert packet["actions_taken"] == []
    assert packet["pending_action"] is None
    assert packet["suggested_next_steps"][0].startswith("Look up the charge")
    assert packet["summary"], "a packet without a summary is a transcript with extra steps"
    assert transcript.json()["messages"][0]["text"] == ACCOUNT_QUESTION


async def test_a_reply_reaches_the_customers_open_socket(engine: AsyncEngine) -> None:
    """The second half of what the pack's handoff message promises: "They will reply here."

    A human types, the customer sees it, and the run stays parked - answering a question is not
    the same act as giving the workflow back.
    """
    app = build_app(ACME, engine, config=_config())
    session = "desk-reply-0001"
    async with serving(app) as host, chatting(host, session) as chat:
        await chat.ready()
        await chat.say(ACCOUNT_QUESTION)
        row = await _open_handoff(host)

        async with httpx.AsyncClient(base_url=f"http://{host}") as client:
            replied = await client.post(
                f"/desk/handoffs/{row['id']}/reply",
                json={
                    "text": "It was the annual plan renewal. Shall I refund it?",
                    "human_id": "u1",
                },
            )
            assert replied.status_code == 200

        event = await chat.recv()
        assert event["type"] == "message"
        assert event["author"] == "human", "a customer is entitled to know a person wrote it"
        assert event["text"].startswith("It was the annual plan renewal")

    run = await _run_row(engine, uuid.UUID(row["conversation_id"]))
    assert run["status"] == "waiting_human", "a reply is not a resume"


async def test_resume_continues_from_the_handoff_nodes_resumed_edge(
    engine: AsyncEngine,
) -> None:
    """DESIGN.md section 13, verbatim, with the state patch it offers."""
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-resume-0001")
        async with httpx.AsyncClient(base_url=f"http://{host}") as client:
            response = await client.post(
                f"/desk/handoffs/{row['id']}/resume",
                json={
                    "text": "I have credited that charge. Handing you back.",
                    "patch": {"intent": "handled_by_a_person"},
                    "human_id": "u1",
                },
            )
            assert response.status_code == 200, response.text
            body = response.json()
            after = (await client.get(f"/desk/handoffs/{row['id']}")).json()

    conversation_id = uuid.UUID(row["conversation_id"])
    run = await _run_row(engine, conversation_id)
    # `no_workflow`'s `resumed` edge is `anything_else`, an ask node.
    assert body["status"] == "waiting_customer"
    assert run["frames"][0]["node_id"] == "anything_else"
    assert run["frames"][0]["state"]["intent"] == "handled_by_a_person", "the patch was applied"
    assert after["status"] == "resumed"
    assert after["human_id"] == "u1"
    texts = await _outbound(engine, conversation_id)
    assert "I have credited that charge. Handing you back." in texts
    assert texts[-1] == "Is there anything else I can help you with?"


async def test_close_takes_the_handoff_nodes_closed_edge(engine: AsyncEngine) -> None:
    """ "Take over fully" is a decision the *pack* expresses, not one the engine imposes.

    Closing the run from underneath a waiting node would make the ``closed`` edge unreachable
    and leave the frame stack pointing at a node that never ran.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-close-0001")
        async with httpx.AsyncClient(base_url=f"http://{host}") as client:
            response = await client.post(
                f"/desk/handoffs/{row['id']}/close",
                json={"text": "I will take this from here.", "human_id": "u2"},
            )
            assert response.status_code == 200, response.text
            after = (await client.get(f"/desk/handoffs/{row['id']}")).json()

    conversation_id = uuid.UUID(row["conversation_id"])
    run = await _run_row(engine, conversation_id)
    assert run["status"] == "done"
    assert after["status"] == "closed"
    assert (await _outbound(engine, conversation_id))[-1] == "I will take this from here."


async def test_approve_signs_the_action_the_customer_already_approved(
    engine: AsyncEngine,
) -> None:
    """What makes phase 4's ``requires_human_approval`` satisfiable at last.

    The row this writes is a copy of the customer's own: same tool, same arguments, same hash,
    same run, frame and confirm node, with ``approved_by = 'human'``. There is no way to name a
    different action, because the desk supplies no arguments - which is the whole point of a
    second signature.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-approve-0001")
        conversation_id = uuid.UUID(row["conversation_id"])
        run = await _run_row(engine, conversation_id)
        async with httpx.AsyncClient(base_url=f"http://{host}") as client:
            # Nothing is proposed, so there is nothing to sign.
            nothing = await client.post(f"/desk/handoffs/{row['id']}/approve", json={})
            assert nothing.status_code == 409
            assert "nothing for a human to sign" in nothing.json()["error"]

            await _propose(engine, conversation_id, run["id"])
            signed = await client.post(f"/desk/handoffs/{row['id']}/approve", json={})
            assert signed.status_code == 200, signed.text
            body = signed.json()

    assert body["tool"] == "issue_refund"
    assert body["args"] == {"charge_id": "ch_1002", "amount": 29.0}
    rows = await _approvals(engine, conversation_id)
    assert [row["approved_by"] for row in rows] == ["customer", "human"]
    customer, human = rows
    for field in ("tool", "args", "args_hash", "run_id", "frame_seq", "node_id"):
        assert customer[field] == human[field], f"the human signed a different {field}"
    assert human["step_id"] != customer["step_id"], "two rows, not one overwritten"


async def test_the_desk_refuses_what_it_cannot_find_or_parse(engine: AsyncEngine) -> None:
    app = build_app(ACME, engine, config=_config())
    missing = uuid.uuid4()
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        assert (await client.get(f"/desk/handoffs/{missing}")).status_code == 404
        assert (await client.post(f"/desk/handoffs/{missing}/reply", json={})).status_code == 400
        assert (await client.get(f"/desk/conversations/{missing}/transcript")).status_code == 404
        bad = await client.post(
            f"/desk/handoffs/{missing}/resume",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert bad.status_code == 400
        # A body the model refuses - an unknown key - is a refusal, not a silently ignored field.
        extra = await client.post(
            f"/desk/handoffs/{missing}/reply", json={"text": "hi", "resume": True}
        )
        assert extra.status_code == 400


async def test_the_desk_can_be_turned_off(engine: AsyncEngine) -> None:
    """A deployment that reads the ``handoff`` table with its own tooling serves no desk."""
    app = build_app(ACME, engine, config=_config().model_copy(update={"serve_desk": False}))
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:
        assert (await client.get("/desk/handoffs")).status_code == 404


# -- helpers -----------------------------------------------------------------------------------


async def _open_handoff(host: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=f"http://{host}") as client:
        listed = await client.get("/desk/handoffs")
        rows = listed.json()["handoffs"]
        assert rows, listed.text
        return dict(rows[0])


async def _run_row(engine: AsyncEngine, conversation_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as connection:
        result = await connection.execute(
            sql_text("SELECT * FROM run WHERE conversation_id = :c"), {"c": conversation_id}
        )
        return dict(result.mappings().one())


async def _outbound(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            sql_text(
                "SELECT text FROM message WHERE conversation_id = :c AND direction = 'outbound' "
                "ORDER BY created_at, ordinal, id"
            ),
            {"c": conversation_id},
        )
        return [row[0] for row in result]


async def _approvals(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(
            sql_text(
                "SELECT tool, args, args_hash, approved_by, run_id, frame_seq, node_id, step_id "
                "FROM action_approval WHERE conversation_id = :c ORDER BY approved_at, id"
            ),
            {"c": conversation_id},
        )
        return [dict(row) for row in result.mappings()]


async def _propose(engine: AsyncEngine, conversation_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Write the customer's approval of a refund, as a ``confirm`` node's checkpoint would.

    By hand, because the conversation under test is an account question rather than a refund:
    what is being tested is the desk's *second signature*, and driving a whole refund to get one
    would make the test about the refund.
    """
    async with engine.begin() as connection:
        await connection.execute(
            sql_text(
                "INSERT INTO action_approval (conversation_id, run_id, frame_seq, node_id, "
                "step_id, tool, args, args_hash, approved_by, approved_at) VALUES "
                "(:c, :r, 1, 'confirm_refund', 'step:1', 'issue_refund', CAST(:args AS jsonb), "
                " 'deadbeef', 'customer', now())"
            ),
            {
                "c": conversation_id,
                "r": run_id,
                "args": json.dumps({"charge_id": "ch_1002", "amount": 29.0}),
            },
        )
