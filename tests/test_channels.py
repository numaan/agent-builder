"""The channel adapter protocol and the web chat adapter (DESIGN.md section 12).

The protocol is the part with a future in it: phase 7's email adapter has to implement it
unchanged, so the tests here are as much about what the protocol *is not* - no connection, no
session, nothing a browser told us about the customer - as about what web chat does with it.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.channels import (
    AwaitingSummary,
    ChannelAdapter,
    ChannelHub,
    ConnectionRegistry,
    ConversationRef,
    InboundMessage,
    InboundRejected,
    UnknownChannelError,
    WebChatAdapter,
)
from support_core.engine import Executor, OutboundMessage
from support_core.storage import repositories as repo

TEST_PACKS = Path(__file__).resolve().parent / "packs"
QUEUE_PACK = TEST_PACKS / "queue_pack"


class Recorder:
    """A connection that keeps what it was pushed."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def push(self, event: Any) -> None:
        self.events.append(dict(event))


class Broken:
    """A connection whose socket has gone."""

    async def push(self, event: Any) -> None:
        raise ConnectionResetError("the tab is closed")


def executor_for(engine: AsyncEngine) -> Executor:
    return Executor(load_pack(QUEUE_PACK), engine, lock_wait_seconds=0.0)


# -- the protocol -------------------------------------------------------------------------


def test_the_protocol_is_exactly_the_three_methods_the_design_names() -> None:
    """DESIGN.md section 12 gives ``parse_inbound``, ``send`` and ``conversation_key``.

    Pinned, because phase 7's email adapter has to implement this protocol without changing it.
    A fourth method added for the convenience of one transport is how a protocol stops being
    one; if a later phase needs one, this test is where the decision gets made deliberately.
    """
    attrs: set[str] = ChannelAdapter.__protocol_attrs__  # type: ignore[attr-defined]
    assert attrs == {
        "channel",
        "conversation_key",
        "parse_inbound",
        "send",
    }


def test_the_web_chat_adapter_satisfies_the_protocol() -> None:
    assert isinstance(WebChatAdapter(), ChannelAdapter)


def test_an_inbound_message_says_nothing_about_a_connection() -> None:
    """What crosses into the engine is a message, a key and who the *transport* knows this is.

    An email adapter fills ``customer_ref`` from the From address; a browser cannot know
    anything of the sort. Neither carries a socket, and the engine could not use one if it did.
    """
    fields = set(InboundMessage.model_fields)
    assert fields == {
        "channel",
        "conversation_key",
        "text",
        "customer_ref",
        "external_id",
        "metadata",
    }


# -- what a browser may say ---------------------------------------------------------------


async def test_a_plain_message_parses() -> None:
    adapter = WebChatAdapter()
    inbound = await adapter.parse_inbound({"type": "message", "session": "s" * 12, "text": " hi "})
    assert inbound.channel == "web_chat"
    assert inbound.conversation_key == "s" * 12
    assert inbound.text == "hi"
    assert inbound.customer_ref is None


@pytest.mark.parametrize(
    "payload",
    [
        {"session": "s" * 12, "text": "hi", "customer": {"ref": "cus_someone_else"}},
        {"session": "s" * 12, "text": "hi", "customer_ref": "cus_someone_else"},
        {"session": "s" * 12, "text": "hi", "context": {"customer": {"identity_verified": True}}},
        {"session": "s" * 12, "text": "hi", "channel": "email"},
    ],
)
async def test_a_browser_may_not_describe_the_customer(payload: dict[str, Any]) -> None:
    """Identity spoofing with a friendly interface, refused at the transport.

    ``identity_verified`` is set by the pack's verification workflow and by nothing else
    (DESIGN.md section 10); ``customer_ref`` decides whose charges the refund graph reads. A
    page that could post either would be the whole security model, undone by a text field.
    """
    with pytest.raises(InboundRejected):
        await WebChatAdapter().parse_inbound(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "hi"},
        {"session": "short", "text": "hi"},
        {"session": "with spaces!!", "text": "hi"},
        {"session": "s" * 12},
        {"session": "s" * 12, "text": "   "},
        {"session": "s" * 12, "text": "x" * 4001},
        "not an object",
        None,
    ],
)
async def test_a_payload_that_is_not_a_message_is_refused(payload: Any) -> None:
    with pytest.raises(InboundRejected):
        await WebChatAdapter().parse_inbound(payload)


def test_the_conversation_key_is_read_without_a_message() -> None:
    """A transport often has to resolve the conversation before it has any text: a socket
    opening, or (phase 7) a delivery receipt with no body."""
    assert WebChatAdapter().conversation_key({"session": "abcdefgh"}) == "abcdefgh"
    with pytest.raises(InboundRejected):
        WebChatAdapter().conversation_key({"session": ""})


# -- delivery -----------------------------------------------------------------------------


async def test_an_outbound_message_reaches_every_live_connection() -> None:
    connections = ConnectionRegistry()
    adapter = WebChatAdapter(connections)
    conversation = ConversationRef(id=uuid.uuid4(), channel="web_chat", key="s" * 12)
    first, second = Recorder(), Recorder()
    connections.add(conversation.id, first)
    connections.add(conversation.id, second)

    await adapter.send(conversation, OutboundMessage(text="your refund is on its way"))

    assert first.events == second.events
    assert first.events == [
        {"type": "message", "author": "agent", "text": "your refund is on its way"}
    ]


async def test_a_message_with_nobody_watching_is_not_a_failure() -> None:
    """The customer closed the tab. The row is durable and the next connection gets it in the
    transcript; raising here would abort the turn that produced it."""
    adapter = WebChatAdapter()
    await adapter.send(
        ConversationRef(id=uuid.uuid4(), channel="web_chat", key="s" * 12),
        OutboundMessage(text="anyone there?"),
    )


async def test_a_broken_connection_is_dropped_rather_than_retried() -> None:
    connections = ConnectionRegistry()
    conversation_id = uuid.uuid4()
    broken, live = Broken(), Recorder()
    connections.add(conversation_id, broken)
    connections.add(conversation_id, live)

    delivered = await connections.broadcast(conversation_id, {"type": "message", "text": "hi"})

    assert delivered == 1
    assert connections.count(conversation_id) == 1
    assert live.events


async def test_the_hub_does_not_let_a_transport_failure_abort_a_turn(engine: AsyncEngine) -> None:
    """Phase 2's self-critique, fragility item 1: ``hooks.send`` runs inside the transaction
    that marks the rows sent, so an exception there rolls it back and takes the turn with it."""
    executor = executor_for(engine)
    conversation_id = await executor.start_conversation(channel="web_chat", channel_key="k" * 12)
    connections = ConnectionRegistry()
    connections.add(conversation_id, Broken())
    failures: list[Exception] = []
    hub = ChannelHub(
        executor,
        [WebChatAdapter(connections)],
        on_send_error=lambda c, m, e: failures.append(e),
    )

    await hub.deliver(conversation_id, [OutboundMessage(text="hello")])

    # The registry drops a broken connection before the adapter sees the failure, so the hub's
    # handler is not called here; what matters is that nothing escaped.
    assert failures == []
    assert connections.count(conversation_id) == 0


async def test_a_delivery_failure_in_an_adapter_is_reported_not_raised(
    engine: AsyncEngine,
) -> None:
    class Exploding:
        channel = "web_chat"

        def conversation_key(self, raw: Any) -> str:  # pragma: no cover - unused here
            raise NotImplementedError

        async def parse_inbound(self, raw: Any) -> InboundMessage:  # pragma: no cover
            raise NotImplementedError

        async def send(self, conversation: ConversationRef, msg: OutboundMessage) -> None:
            raise RuntimeError("the mail provider said no")

    executor = executor_for(engine)
    conversation_id = await executor.start_conversation(channel="web_chat", channel_key="j" * 12)
    seen: list[Exception] = []
    hub = ChannelHub(executor, [Exploding()], on_send_error=lambda c, m, e: seen.append(e))

    await hub.deliver(conversation_id, [OutboundMessage(text="hello")])

    assert [str(exc) for exc in seen] == ["the mail provider said no"]


async def test_a_conversation_on_a_channel_with_no_adapter_is_reported(
    engine: AsyncEngine,
) -> None:
    executor = executor_for(engine)
    conversation_id = await executor.start_conversation(channel="email", channel_key="t" * 12)
    seen: list[Exception] = []
    hub = ChannelHub(executor, [WebChatAdapter()], on_send_error=lambda c, m, e: seen.append(e))

    await hub.deliver(conversation_id, [OutboundMessage(text="hello")])

    assert len(seen) == 1
    assert isinstance(seen[0], UnknownChannelError)


# -- the durable key ----------------------------------------------------------------------


async def test_the_same_session_key_finds_the_same_conversation(engine: AsyncEngine) -> None:
    """The identity DESIGN.md section 12 gives a channel: "thread id, session id"."""
    hub = ChannelHub(executor_for(engine), [WebChatAdapter()])
    inbound = InboundMessage(channel="web_chat", conversation_key="s" * 12, text="hello")

    first, created = await hub.conversation_for(inbound)
    assert created
    again, created_again = await hub.conversation_for(inbound)

    assert again.id == first.id
    assert not created_again


async def test_two_callers_racing_on_one_key_get_one_conversation(engine: AsyncEngine) -> None:
    """Two tabs opened at once, or a webhook delivered twice. The unique index decides."""
    hub = ChannelHub(executor_for(engine), [WebChatAdapter()])
    inbound = InboundMessage(channel="web_chat", conversation_key="r" * 12, text="hello")

    results = await asyncio.gather(
        *(hub.conversation_for(inbound) for _ in range(5)), return_exceptions=True
    )

    conversations = [result for result in results if not isinstance(result, BaseException)]
    assert len(conversations) == 5, [r for r in results if isinstance(r, BaseException)]
    assert len({ref.id for ref, _ in conversations}) == 1


async def test_a_key_belongs_to_its_channel(engine: AsyncEngine) -> None:
    """The same string on two channels is two conversations: a mail thread id and a chat
    session are not in the same namespace."""
    executor = executor_for(engine)
    chat = await executor.start_conversation(channel="web_chat", channel_key="same-key-1234")
    mail = await executor.start_conversation(channel="email", channel_key="same-key-1234")
    assert chat != mail

    async with executor.sessions() as session, session.begin():
        found = await repo.conversation_by_channel_key(
            session, channel="web_chat", channel_key="same-key-1234"
        )
    assert found is not None
    assert found.id == chat


async def test_a_conversation_without_a_key_is_allowed(engine: AsyncEngine) -> None:
    """The engine's own callers - and phase 7's desk - open conversations no channel named.
    The unique index is partial so any number of those can exist."""
    executor = executor_for(engine)
    first = await executor.start_conversation(channel="web_chat")
    second = await executor.start_conversation(channel="web_chat")
    assert first != second


# -- what the client is told --------------------------------------------------------------


def test_a_waiting_confirm_is_visible_to_the_client_without_its_hash() -> None:
    """The confirmation is the moment DESIGN.md section 8.2 exists for, so the client is told
    it is happening. The argument hash is the engine's binding and stays there."""
    summary = AwaitingSummary.of(
        {
            "kind": "node",
            "node": "confirm_refund",
            "frame_seq": 2,
            "detail": {
                "node": "confirm_refund",
                "kind": "confirm",
                "tool": "issue_refund",
                "args_hash": "0" * 64,
                "prompt": "I can refund 29.00 USD ... Shall I go ahead?",
            },
        }
    )
    assert summary is not None
    assert summary.kind == "confirm"
    assert summary.tool == "issue_refund"
    assert "0" * 64 not in summary.model_dump_json()


def test_an_ordinary_question_is_not_reported_as_a_confirmation() -> None:
    summary = AwaitingSummary.of({"kind": "node", "node": "ask_code", "detail": {}})
    assert summary is not None
    assert summary.kind == "question"
    assert summary.tool is None
    assert AwaitingSummary.of(None) is None

    parked = AwaitingSummary.of({"kind": "handoff", "reason": "engine_error"})
    assert parked is not None
    assert parked.kind == "handoff"
