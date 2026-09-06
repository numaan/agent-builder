"""The phase 4 exit criterion, and what it is supposed to prove.

    Adversarial approval tests pass and the refund graph runs with the fake provider through
    confirm and issue_refund. - BACKLOG.md, phase 4

``tests/test_golden_conversation.py`` runs the conversation and pins its path and transcript.
This file asks the questions that matter about the *money*: was there exactly one approval, was
it bound to the arguments the customer was shown, was it consumed by the call it authorised, and
did the billing system move one refund rather than none or two.

Everything runs through the real executor against real Postgres, with
:class:`~support_core.llm.fake.FakeProvider` replaying the committed cassette - so a change to a
prompt fails loudly rather than quietly re-deciding the conversation.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.llm.fake import FakeProvider
from support_core.llm.recording import Cassette
from support_core.tools.approval import approval_hash
from support_core.tools.loading import import_pack_tools
from tests.cassettes.scenarios import ACME, ACME_REFUND, play
from tests.engine_support import approvals, outbound_texts, path, run_row, tool_calls


@pytest.fixture
def billing() -> object:
    """The pack's fake billing system, through the module the registry imported."""
    module = import_pack_tools(ACME)
    module.reset_backend()
    return module.BILLING


async def _run(engine: AsyncEngine) -> uuid.UUID:
    provider = FakeProvider(Cassette.load(ACME_REFUND.cassette_path))
    conversation_id, _ = await play(ACME_REFUND, engine, provider)
    return conversation_id


async def test_the_refund_graph_runs_through_confirm_and_issue_refund(
    engine: AsyncEngine, billing: object
) -> None:
    conversation_id = await _run(engine)
    row = await run_row(engine, conversation_id)
    visited = await path(engine, row["id"])
    assert "confirm_refund" in visited
    assert "issue_refund" in visited
    assert visited[-1] == "done_finished"
    assert row["status"] == "done"

    spoken = await outbound_texts(engine, conversation_id)
    assert any("six-digit code" in text for text in spoken)
    assert any("Shall I go ahead?" in text for text in spoken)
    assert any("five to seven business days" in text for text in spoken)


async def test_the_money_moved_exactly_once(engine: AsyncEngine, billing: object) -> None:
    """The question a test of a refund flow exists to answer."""
    await _run(engine)
    assert billing.executed == ["issue_refund:ch_1002:29.0"]  # type: ignore[attr-defined]
    assert len(billing.refunds) == 1  # type: ignore[attr-defined]
    assert billing.charges["ch_1002"].refunded_by is not None  # type: ignore[attr-defined]
    assert billing.charges["ch_1001"].refunded_by is None  # type: ignore[attr-defined]


async def test_the_approval_is_bound_to_the_arguments_and_consumed_by_the_call(
    engine: AsyncEngine, billing: object
) -> None:
    """DESIGN.md section 8.2, and phase-0 review finding N1's single-use marker."""
    conversation_id = await _run(engine)
    given = await approvals(engine, conversation_id)
    assert len(given) == 1, "one confirmation, one approval"
    approval = given[0]
    assert approval["tool"] == "issue_refund"
    assert approval["approved_by"] == "customer"
    assert approval["node_id"] == "confirm_refund"
    assert approval["args"] == {"charge_id": "ch_1002", "amount": 29.0}
    # The hash is exactly DESIGN.md 8.2's, over the arguments the row records.
    assert approval["args_hash"] == approval_hash("issue_refund", approval["args"])
    # And it is spent: a second call with the same arguments has nothing to present.
    assert approval["consumed_at"] is not None
    assert approval["consumed_by_tool_call_id"] is not None

    calls = await tool_calls(engine, conversation_id)
    refund = next(call for call in calls if call["tool"] == "issue_refund")
    # The two rows point at each other: this approval authorised this call and no other.
    assert approval["consumed_by_tool_call_id"] == refund["id"]
    assert refund["approval_id"] is not None
    assert refund["status"] == "succeeded"
    assert refund["risk"] == "high"
    assert refund["args"] == approval["args"]


async def test_every_tool_call_is_recorded_against_the_run(
    engine: AsyncEngine, billing: object
) -> None:
    """Phase-0 deferred finding F6: the join exists, and it is complete."""
    conversation_id = await _run(engine)
    calls = await tool_calls(engine, conversation_id)
    assert [call["tool"] for call in calls] == [
        "send_otp",
        "verify_otp",
        "list_recent_charges",  # the model's own READ call, inside find_charge's loop
        "get_charge",
        "check_refund_eligibility",
        "issue_refund",
    ]
    assert all(call["status"] == "succeeded" for call in calls)
    assert all(call["attempts"] == 1 for call in calls)
    # The step id is the idempotency key, and a model-loop call adds a suffix so one step can
    # make several (DESIGN.md section 7.1).
    keys = [call["idempotency_key"] for call in calls]
    assert len(set(keys)) == len(keys)
    model_loop = next(call for call in calls if call["tool"] == "list_recent_charges")
    assert model_loop["idempotency_key"].startswith(model_loop["step_id"])
    assert model_loop["idempotency_key"].endswith("#tool1")
    assert model_loop["approval_id"] is None, "a READ tool needs no approval"

    # Only the two exempt WRITE tools ran without one, and only the HIGH tool has one.
    with_approval = [call["tool"] for call in calls if call["approval_id"] is not None]
    assert with_approval == ["issue_refund"]


async def test_verify_otp_is_what_sets_identity_verified(
    engine: AsyncEngine, billing: object
) -> None:
    """DESIGN.md section 19 step 9, and section 6.1's "ctx is read-only to all nodes".

    The gate is passed because a *tool* changed the context, not because a node wrote it: the
    patch is on the ``verify_otp`` call's row, and the conversation carries it afterwards.
    """
    conversation_id = await _run(engine)
    calls = await tool_calls(engine, conversation_id)
    verify = next(call for call in calls if call["tool"] == "verify_otp")
    assert verify["context_patch"] == {
        "customer": {
            "ref": "cus_acme_1",
            "identity_verified": True,
            "name": "Sam",
            "email": "me@example.com",
            "locale": "en",
            "attributes": {},
        }
    }
    send = next(call for call in calls if call["tool"] == "send_otp")
    assert send["context_patch"] is None, "sending a code establishes nothing"

    from sqlalchemy import text as sql

    async with engine.connect() as connection:
        stored = await connection.execute(
            sql("SELECT context FROM conversation WHERE id = :c"), {"c": conversation_id}
        )
        context = stored.scalar_one()
    assert context["customer"]["identity_verified"] is True
