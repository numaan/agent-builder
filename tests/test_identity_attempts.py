"""The sample pack's passcode has a finite number of guesses (review finding R3).

``verify_identity`` is the graph that opens the refund gate, so its wrong-code edge - which
returns to ``send_code`` and asks again - is the pack's shortest path past everything phase 4
built. One customer message per guess is not a cost, and the fake's code is a pure function of
the address, so the loop was a six-digit code with unlimited attempts.

The limit lives in the tool, because the expression language has no arithmetic and a counter a
graph cannot increment is not a counter; the graph routes on it to the failure path it already
had. Both halves are tested here: the store's counting, and the graph actually stopping.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from support_core.graph.pack import Pack
from support_core.tools.loading import import_pack_tools
from tests.cassettes.scenarios import ACME
from tests.engine_support import Recorder, outbound_texts, path, run_row

CUSTOMER = {"ref": "cus_acme_1", "email": "me@example.com", "name": "Sam"}


@pytest.fixture
def identity_pack() -> Pack:
    """The sample pack, entered at ``verify_identity`` rather than at the root graph."""
    pack = load_pack(ACME)
    pack.manifest.entry_graph = "verify_identity"
    module = import_pack_tools(ACME)
    module.reset_backend()
    return pack


def test_the_store_stops_checking_after_three_wrong_guesses() -> None:
    from packs.acme_billing.tools.identity import MAX_OTP_ATTEMPTS, OtpStore

    otp = OtpStore()
    conversation = uuid.uuid4()
    code = otp.send(conversation, "me@example.com")

    for _ in range(MAX_OTP_ATTEMPTS):
        assert otp.check(conversation, "000000") is False
    assert otp.locked(conversation)
    assert otp.check(conversation, code) is False, "the right code no longer opens it either"


def test_a_re_send_does_not_hand_back_the_guesses_already_spent() -> None:
    """The half that matters: a limit the customer can reset by asking for another code is not
    a limit, and the graph's wrong-code edge goes back to ``send_code``."""
    from packs.acme_billing.tools.identity import MAX_OTP_ATTEMPTS, OtpStore

    otp = OtpStore()
    conversation = uuid.uuid4()
    for _ in range(MAX_OTP_ATTEMPTS):
        otp.send(conversation, "me@example.com")
        assert otp.check(conversation, "000000") is False
    otp.send(conversation, "me@example.com")
    assert otp.locked(conversation)


async def test_the_passcode_loop_ends_rather_than_asking_for_ever(
    engine: AsyncEngine, identity_pack: Pack
) -> None:
    """Three wrong codes and the graph is finished, rather than asking for a fourth."""
    from packs.acme_billing.tools.identity import MAX_OTP_ATTEMPTS

    recorder = Recorder()
    executor = Executor(identity_pack, engine, hooks=recorder.hooks())
    conversation_id = await executor.start_conversation(context={"customer": dict(CUSTOMER)})
    await executor.on_inbound(conversation_id, "I am here")

    for _ in range(MAX_OTP_ATTEMPTS):
        outcome = await executor.on_inbound(conversation_id, "000000")

    assert outcome.status == "done"
    row = await run_row(engine, conversation_id)
    walked = await path(engine, row["id"])
    assert "too_many_attempts" in walked
    assert walked[-1] == "not_verified"
    assert walked.count("send_code") == MAX_OTP_ATTEMPTS, "one send per guess, then no more"
    assert any("stop guessing" in text for text in await outbound_texts(engine, conversation_id))


async def test_the_right_code_still_verifies_within_the_limit(
    engine: AsyncEngine, identity_pack: Pack
) -> None:
    """The limit refuses guesses, not customers: two wrong and then the real code verifies."""
    from packs.acme_billing.tools.identity import OTP

    # ``code_for`` is a pure function of the address, so this is the code the *registry's* copy
    # of the store sent, even though the two module objects are different (see reset_backend).
    recorder = Recorder()
    executor = Executor(identity_pack, engine, hooks=recorder.hooks())
    conversation_id = await executor.start_conversation(context={"customer": dict(CUSTOMER)})
    await executor.on_inbound(conversation_id, "I am here")
    await executor.on_inbound(conversation_id, "000000")
    await executor.on_inbound(conversation_id, "111111")

    outcome = await executor.on_inbound(conversation_id, OTP.code_for(CUSTOMER["email"]))

    assert outcome.status == "done"
    row = await run_row(engine, conversation_id)
    walked = await path(engine, row["id"])
    assert walked[-1] == "verified"
    assert "too_many_attempts" not in walked
