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
from tests.app_support import (
    ACME,
    CASSETTES,
    build_app,
    chatting,
    reset_acme_backend,
    serving,
    sync_acme_knowledge,
)
from tests.test_desk_auth import DESK_AUTH, DESK_TOKEN

ACCOUNT_QUESTION = "Why was I charged 40 dollars on the 3rd?"
"""The message the cassette records an ``account_question`` answer for."""


def _config() -> AppConfig:
    """A deployment that has turned the desk on, which since review finding W1 means a token.

    Off is the default now, and on without a credential will not start; the credential itself is
    tested in ``test_desk_auth.py``, so every client here carries it and these tests are about
    what the desk *does*.
    """
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
    )


async def _handed_off(host: str, session: str) -> dict[str, Any]:
    """Drive the sample pack to its ``handoff`` node and return the queued row."""
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
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
async def _fresh_backend(engine: AsyncEngine) -> None:
    """Seed account and corpus. The desk tests drive a real conversation, and phase 5 gave the
    sample pack knowledge that its `llm` nodes now answer from."""
    reset_acme_backend()
    await sync_acme_knowledge(engine)


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
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
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

        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
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
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
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


async def test_a_resume_patch_naming_an_undeclared_field_is_refused(
    engine: AsyncEngine,
) -> None:
    """Review finding P1, reproduced as the reviewer reproduced it.

    A desk operator mistypes one field name. Before the fix the key was written straight into
    ``run.frames`` where no API call could remove it, every later entry to the frame failed the
    graph's state model, and the conversation was parked for ever with
    ``handoff_reason = 'pack_incompatible'`` - which blames the pack for a typo at the desk.

    What the operator is owed instead is an error they can act on: the field they got wrong, the
    fields the graph actually declares, and a run that has not moved.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-patch-0001")
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
            refused = await client.post(
                f"/desk/handoffs/{row['id']}/resume",
                json={"patch": {"identity_verified": True, "not_a_field": "x"}, "human_id": "u1"},
            )
            assert refused.status_code == 400, refused.text
            message = refused.json()["error"]
            assert "not_a_field" in message, "the operator is told which field is wrong"
            assert "charge_hint" in message, "and which fields the graph does allow"
            after = (await client.get(f"/desk/handoffs/{row['id']}")).json()

            # And the conversation is still workable: a patch the graph *can* hold still lands.
            accepted = await client.post(
                f"/desk/handoffs/{row['id']}/resume",
                json={"patch": {"intent": "handled_by_a_person"}, "human_id": "u1"},
            )
            assert accepted.status_code == 200, accepted.text

    conversation_id = uuid.UUID(row["conversation_id"])
    assert after["status"] == "open", "a refused resume does not resolve the queue row"
    run = await _run_row(engine, conversation_id)
    assert run["frames"][0]["state"] == {"intent": "handled_by_a_person"}
    assert "not_a_field" not in run["frames"][0]["state"]
    assert run["status"] == "waiting_customer", "the conversation was never bricked"
    assert (run["awaiting"] or {}).get("kind") != "handoff", "the pack was never at fault"


async def test_a_resume_patch_may_not_set_identity_verified(engine: AsyncEngine) -> None:
    """DESIGN.md section 10: only the ``verify_identity`` sub-graph sets it, via a tool.

    The same rule ``AppConfig`` is already held to for a new conversation's context. A desk that
    could grant it would open every identity gate in the pack from an unauthenticated endpoint.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-patch-0002")
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
            refused = await client.post(
                f"/desk/handoffs/{row['id']}/resume", json={"patch": {"identity_verified": True}}
            )
            assert refused.status_code == 400, refused.text
            assert "identity_verified" in refused.json()["error"]

    run = await _run_row(engine, uuid.UUID(row["conversation_id"]))
    assert run["status"] == "waiting_human", "the run did not move"
    assert run["frames"][0]["state"] == {"intent": "account_question"}


async def test_a_resume_patch_cannot_reach_a_live_approval(engine: AsyncEngine) -> None:
    """A patch may not be a way to authorise, revive or re-price an approved action (8.2).

    The frame holds a proposal the customer agreed to. Editing the state that proposal was
    computed from, from an endpoint with no authentication, is either a silent change to what
    they agreed to or a run-time hash refusal nobody at the desk can see. The desk has one route
    to an action and it is ``approve``, which copies the customer's own row.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-patch-0003")
        conversation_id = uuid.UUID(row["conversation_id"])
        run = await _run_row(engine, conversation_id)
        await _propose(engine, conversation_id, run["id"], frame_seq=run["frames"][-1]["frame_seq"])
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
            refused = await client.post(
                f"/desk/handoffs/{row['id']}/resume", json={"patch": {"intent": "whatever"}}
            )
            assert refused.status_code == 409, refused.text
            assert "issue_refund" in refused.json()["error"]

    assert [a["approved_by"] for a in await _approvals(engine, conversation_id)] == ["customer"]
    after = await _run_row(engine, conversation_id)
    assert after["status"] == "waiting_human"
    assert after["frames"][0]["state"] == {"intent": "account_question"}


async def test_close_takes_the_handoff_nodes_closed_edge(engine: AsyncEngine) -> None:
    """ "Take over fully" is a decision the *pack* expresses, not one the engine imposes.

    Closing the run from underneath a waiting node would make the ``closed`` edge unreachable
    and leave the frame stack pointing at a node that never ran.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-close-0001")
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
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

    The approval is resolved from the **packet**, so what is signed is what the human read
    (review finding P4). The proposal is therefore written before the conversation reaches the
    handoff, which is the order the real path has: a ``requires_human_approval`` tool refuses for
    want of a second signature, and the packet built on the way out names the action waiting for
    one.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        runtime = app.state.runtime
        conversation = await runtime.conversation_for_key("web_chat", "desk-approve-0001")
        conversation_id = conversation.id
        run = await _run_row(engine, conversation_id)
        await _propose(engine, conversation_id, run["id"], frame_seq=0)
        row = await _handed_off(host, "desk-approve-0001")
        assert row["conversation_id"] == str(conversation_id)
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
            packet = (await client.get(f"/desk/handoffs/{row['id']}")).json()["packet"]
            assert packet["pending_action"]["tool"] == "issue_refund", "the human read this"

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


async def test_approve_signs_only_the_action_the_handoff_showed(engine: AsyncEngine) -> None:
    """Review finding P4, the three ways the old endpoint could sign the wrong thing.

    It resolved ``live_approvals(conversation)[-1]``: not scoped to the handoff's run, not
    checked against the packet, and not checked against the handoff's own status. So a human
    looking at a packet that showed nothing - or showed an unfinished call they must *not*
    repeat - could put their signature on a different, newer action with one click, and a
    handoff somebody had already closed could still sign.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-approve-0002")
        conversation_id = uuid.UUID(row["conversation_id"])
        run = await _run_row(engine, conversation_id)
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
            # 1. The packet showed no pending action, so there is nothing here to sign.
            nothing = await client.post(f"/desk/handoffs/{row['id']}/approve", json={})
            assert nothing.status_code == 409
            assert "nothing here for a human to sign" in nothing.json()["error"]

            # 2. An action proposed *after* the packet was built is not the one the human read.
            await _propose(engine, conversation_id, run["id"], frame_seq=0)
            later = await client.post(f"/desk/handoffs/{row['id']}/approve", json={})
            assert later.status_code == 409, later.text
            assert "Re-read the handoff" in later.json()["error"]

            # 3. A handoff nobody is working on any more signs nothing at all.
            closed = await client.post(f"/desk/handoffs/{row['id']}/close", json={})
            assert closed.status_code == 200, closed.text
            resolved = await client.post(f"/desk/handoffs/{row['id']}/approve", json={})
            assert resolved.status_code == 409
            assert "this handoff is closed" in resolved.json()["error"]

    assert [a["approved_by"] for a in await _approvals(engine, conversation_id)] == ["customer"]


async def test_a_customer_message_on_a_parked_run_is_answered_and_shown_to_the_desk(
    engine: AsyncEngine,
) -> None:
    """Review finding P6. The refusal is right; the silence was not.

    A run parked ``waiting_human`` does not resume on a customer message - the message stays
    ``pending`` until the desk acts, so a topic change cannot smuggle a workflow past the person
    it was escalated to. But the customer had just been told a person would pick this up, and
    replying produced nothing at all: on web chat a message into a void, on email a swallowed
    reply. Now the engine says one sentence, once, and the desk can read what they said.
    """
    app = build_app(ACME, engine, config=_config())
    async with serving(app) as host:
        row = await _handed_off(host, "desk-queued-0001")
        conversation_id = uuid.UUID(row["conversation_id"])
        async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
            for text in ("are you still there?", "hello?"):
                sent = await client.post(
                    "/channels/web_chat/messages",
                    json={"session": "desk-queued-0001", "text": text},
                )
                assert sent.status_code == 200, sent.text
                assert sent.json()["status"] == "waiting_human", "the run did not move"
            read = (await client.get(f"/desk/handoffs/{row['id']}")).json()

    waiting = [message["text"] for message in read["waiting_messages"]]
    assert waiting == ["are you still there?", "hello?"], "the desk reads what they said"
    said = await _outbound(engine, conversation_id)
    acknowledged = [text for text in said if "it is with one of our people" in text]
    assert len(acknowledged) == 1, "said once per parking, not once per message"

    async with engine.connect() as connection:
        result = await connection.execute(
            sql_text(
                "SELECT count(*) FROM message WHERE conversation_id = :c "
                "AND direction = 'inbound' AND status = 'pending'"
            ),
            {"c": conversation_id},
        )
        assert result.scalar_one() == 2, "and both are still queued for whoever resumes"


async def test_the_desk_refuses_what_it_cannot_find_or_parse(engine: AsyncEngine) -> None:
    app = build_app(ACME, engine, config=_config())
    missing = uuid.uuid4()
    async with (
        serving(app) as host,
        httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client,
    ):
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
    async with (
        serving(app) as host,
        httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client,
    ):
        assert (await client.get("/desk/handoffs")).status_code == 404


# -- helpers -----------------------------------------------------------------------------------


async def _open_handoff(host: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=f"http://{host}", headers=DESK_AUTH) as client:
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


async def _propose(
    engine: AsyncEngine, conversation_id: uuid.UUID, run_id: uuid.UUID, *, frame_seq: int = 1
) -> None:
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
                "(:c, :r, :f, 'confirm_refund', 'step:1', 'issue_refund', CAST(:args AS jsonb), "
                " 'deadbeef', 'customer', now())"
            ),
            {
                "c": conversation_id,
                "r": run_id,
                "f": frame_seq,
                "args": json.dumps({"charge_id": "ch_1002", "amount": 29.0}),
            },
        )
