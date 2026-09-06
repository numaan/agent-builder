"""The order the pending inbound queue is drained in. Phase W review finding W4.

    Inbound messages that arrive while locked are stored in ``message`` with
    ``status = pending`` and processed in order when the lock frees. - DESIGN.md section 17

*In order* used to mean ``ORDER BY created_at, id``. ``created_at`` defaults to ``now()``, which
in Postgres is the **transaction start** timestamp, so two callers who arrive together share it -
and the tie then fell to a random UUID. The reviewer posted two messages at once in a known
order, got them back in the other one, and watched the second message run against a conversation
the first had not started: it dead-ended into an ``llm_unavailable`` handoff, the run parked
``waiting_human``, and the first message is still ``pending`` and always will be.

Phase 2's review found two message-loss bugs of this family and fixed both in durable state
rather than in timing. This is the same kind of fix: ``message.queue_seq`` is a number claimed
from ``conversation.inbound_seq`` under the conversation's row lock, so concurrent callers
serialise at the counter and leave with distinct, increasing numbers, and the queue is drained in
the order the rows were made durable rather than in the order two transactions happened to start.

The first test here fails against the reviewed code deterministically.
"""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from support_core.storage import repositories as repo
from support_core.storage.session import make_session_factory
from tests.app_support import TEST_PACKS, build_app, serving

QUEUE_PACK = TEST_PACKS / "queue_pack"

EARLY_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
LATE_ID = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
"""Two ids chosen so that sorting by ``id`` puts them in the wrong order.

Not a contrivance: ``gen_random_uuid()`` produces one of these orders half the time, so under the
old tie-break the reviewer's reordering was a coin toss on every simultaneous pair.
"""


async def _a_conversation(session: AsyncSession, key: str) -> uuid.UUID:
    conversation = await repo.create_conversation(
        session, channel="web_chat", channel_key=key, context={}
    )
    await repo.create_run(
        session,
        conversation_id=conversation.id,
        pack_version="1",
        pack_fingerprint="test",
    )
    return conversation.id


async def test_two_messages_written_at_the_same_instant_keep_their_order(
    engine: AsyncEngine,
) -> None:
    """The regression test for W4, made deterministic.

    Both rows are written in one transaction, so they share ``created_at`` to the microsecond -
    which is exactly what two simultaneous callers used to get - and their ids are then forced
    into the wrong sort order. Under ``ORDER BY created_at, id`` the queue hands back the second
    message first, which is the reviewer's reordering with the coin toss removed. Under
    ``queue_seq`` the order is the order they were enqueued in, because it was decided when they
    were enqueued instead of being reconstructed from a clock afterwards.
    """
    sessions = make_session_factory(engine)
    async with sessions() as session, session.begin():
        conversation_id = await _a_conversation(session, "order-at-one-instant")
        first = await repo.enqueue_inbound(session, conversation_id=conversation_id, text_="first")
        second = await repo.enqueue_inbound(
            session, conversation_id=conversation_id, text_="second"
        )
        first_id, second_id = first.id, second.id
        assert (first.queue_seq, second.queue_seq) == (1, 2)

    async with engine.begin() as connection:
        # A stable, adversarial tie-break, in place of the random one the old ordering used.
        await connection.execute(
            sql_text("UPDATE message SET id = :new WHERE id = :old"),
            {"new": LATE_ID, "old": first_id},
        )
        await connection.execute(
            sql_text("UPDATE message SET id = :new WHERE id = :old"),
            {"new": EARLY_ID, "old": second_id},
        )
        stamps = list(
            await connection.scalars(
                sql_text("SELECT DISTINCT created_at FROM message WHERE direction = 'inbound'")
            )
        )

    assert len(stamps) == 1, "two rows written together share a transaction timestamp"

    async with sessions() as session, session.begin():
        next_up = await repo.peek_next_pending(session, conversation_id)
        queued = await repo.pending_inbound(session, conversation_id)

    assert next_up is not None
    assert next_up.text == "first", "the queue is drained in the order it was written"
    assert [message.text for message in queued] == ["first", "second"]


async def test_concurrent_callers_are_given_distinct_places_in_the_queue(
    engine: AsyncEngine,
) -> None:
    """Twelve at once on one conversation: one queue, twelve places, nothing lost.

    The reviewer's concurrency attempt C1, asked about ordering rather than about latency. What
    the row lock on ``conversation.inbound_seq`` buys is that no two of these can be given the
    same number and none can be given none: there is a single decided order, and it is the order
    the drain follows.
    """
    sessions = make_session_factory(engine)
    async with sessions() as session, session.begin():
        conversation_id = await _a_conversation(session, "order-under-load")

    async def enqueue(text: str) -> None:
        async with sessions() as session, session.begin():
            await repo.enqueue_inbound(session, conversation_id=conversation_id, text_=text)

    await asyncio.gather(*(enqueue(f"message {index}") for index in range(12)))

    async with sessions() as session, session.begin():
        queued = await repo.pending_inbound(session, conversation_id, limit=50)
        conversation = await repo.get_conversation(session, conversation_id)

    assert conversation is not None
    assert conversation.inbound_seq == 12
    assert [message.queue_seq for message in queued] == list(range(1, 13))
    assert len({message.text for message in queued}) == 12


@pytest.mark.parametrize("burst", [6])
async def test_a_burst_through_the_webhook_is_drained_in_the_order_it_was_queued(
    engine: AsyncEngine, burst: int
) -> None:
    """End to end, over real HTTP: the queue's order is the order the turns ran in.

    Which order a set of simultaneous requests *should* have is not a question the database can
    answer - they were simultaneous. What it can answer, and what the reviewer found it could
    not, is that there is exactly one order, that every caller has a place in it, and that the
    conversation is driven in that order rather than in some other one. The sample graph asks a
    question and echoes the answer, so an echo names the message that filled the slot, and
    reading the echoes back against ``queue_seq`` is reading the drain order out of the customer's
    own transcript rather than out of timing.
    """
    app = build_app(QUEUE_PACK, engine)
    session_key = "order-through-the-webhook"
    async with serving(app) as host, httpx.AsyncClient(base_url=f"http://{host}") as client:

        async def post(index: int) -> None:
            await client.post(
                "/channels/web_chat/messages",
                json={"session": session_key, "text": f"note {index}"},
                timeout=90.0,
            )

        await asyncio.gather(*(post(index) for index in range(burst)))
        runtime = app.state.runtime
        assert await runtime.drainer.wait_until_idle(timeout=90.0)

        async with runtime.executor.sessions() as session, session.begin():
            conversation = await repo.conversation_by_channel_key(
                session, channel="web_chat", channel_key=session_key
            )
            assert conversation is not None
            rows = await repo.transcript(session, conversation.id, 200)
            still_pending = await repo.pending_inbound(session, conversation.id, limit=50)

    inbound = sorted(
        (row for row in rows if row.direction == "inbound"), key=lambda row: row.queue_seq or 0
    )
    echoes = [row.text for row in rows if row.text.startswith("You said ")]

    assert not still_pending, "every queued message was drained"
    assert [row.queue_seq for row in inbound] == list(range(1, burst + 1))
    assert len({row.text for row in inbound}) == burst, "nothing lost, nothing duplicated"
    # The graph consumes a message with `ask` and echoes the next one, so every second message
    # in the queue's own order is the one that gets echoed - in that order. Sorting by
    # `queue_seq` above is the point: `created_at` puts these same six rows in a *different*
    # order, which is the ordering the queue used to be drained by.
    assert echoes == [f"You said {row.text}." for row in inbound[1::2]]
