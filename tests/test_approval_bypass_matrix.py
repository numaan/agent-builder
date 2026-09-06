"""The independent review's twenty-five approval-bypass attempts, kept as a test.

The reviewer wrote these as a script under ``reviews/scratch-phase-4/`` and deleted it, which
is the shape phase 3's resolution complained about: a measurement nobody can repeat is a
measurement that quietly rots. Every scenario in reviews/phase-4.md's "Approval bypass attempts"
table is here, in its order, with the reviewer's own verdict as the assertion.

Three of them **execute**, and that is the correct answer: the same call spelled two ways is the
same call. They are asserted as executing, because a change that turned them into refusals would
be a regression in the other direction - a customer would be told no for saying yes to what they
had already agreed to.

The scenarios are deliberately written against the *runtime's* public surface with no argument
this resolution added, so the whole file can be run against the reviewed code to produce a
before-count. The three that are about the executor rather than the runtime are at the bottom.
"""

import asyncio
import unicodedata
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.tools import ToolRefused
from support_core.tools.approval import approval_hash, canonical_args
from tests.tool_support import (
    CHARGE,
    LEDGER,
    approve,
    conversation_and_run,
    runtime,
    site,
)

pytestmark = pytest.mark.usefixtures("clean_ledger")


@pytest.fixture(name="clean_ledger", autouse=True)
def _clean_ledger() -> None:
    LEDGER.reset()


async def _call(
    engine: AsyncEngine,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    args: dict[str, Any],
    tool: str = "charge",
    node_id: str = "do_it",
    frame_seq: int = 1,
    caller: str = "tool_node",
    requires_approval: str | None = "confirm_it",
    allowed: tuple[str, ...] | None = None,
) -> str:
    """One attempt. Returns ``"ran"`` or ``"refused"``, never raises."""
    try:
        await runtime(engine).invoke(
            tool_name=tool,
            args=args,
            site=site(conversation_id, run_id, node_id=node_id, frame_seq=frame_seq),
            caller=caller,  # type: ignore[arg-type]
            requires_approval=requires_approval,
            allowed=allowed,
        )
    except ToolRefused:
        return "refused"
    return "ran"


# -- 1 to 9: is this approval this call's approval? ----------------------------------------


async def test_01_a_high_tool_with_no_approval_row_at_all(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.0}) == "refused"
    assert LEDGER.executed == []


async def test_02_a_valid_approval_but_the_graph_names_no_confirm_node(
    engine: AsyncEngine,
) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    result = await _call(
        engine, conversation_id, run_id, args={"amount": 29.0}, requires_approval=None
    )
    assert result == "refused"
    assert LEDGER.executed == []


async def test_03_a_row_forged_in_the_database_with_every_binding_correct(
    engine: AsyncEngine,
) -> None:
    """**Executes, by design.** The row *is* the authorisation and the database is the trust
    root. Anyone who can write this row can also write a refund straight into the billing
    system; recorded so that the boundary is written down rather than assumed."""
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.0}) == "ran"
    assert LEDGER.executed == [("charge", 29.0)]


async def test_04_one_approval_spent_on_two_sequential_calls(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    first = await _call(engine, conversation_id, run_id, args={"amount": 29.0}, node_id="do_it")
    second = await _call(engine, conversation_id, run_id, args={"amount": 29.0}, node_id="again")
    assert (first, second) == ("ran", "refused")
    assert LEDGER.executed == [("charge", 29.0)]


async def test_05_an_approval_recorded_in_another_frame(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
        frame_seq=1,
    )
    result = await _call(engine, conversation_id, run_id, args={"amount": 29.0}, frame_seq=2)
    assert result == "refused"
    assert LEDGER.executed == []


async def test_06_an_approval_from_an_earlier_run_of_the_same_conversation(
    engine: AsyncEngine,
) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    _other_conversation, other_run = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=other_run,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.0}) == "refused"
    assert LEDGER.executed == []


async def test_07_an_approval_recorded_by_a_different_confirm_node(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
        node_id="some_other_confirm",
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.0}) == "refused"
    assert LEDGER.executed == []


async def test_08_an_approval_recorded_for_a_different_tool(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="nudge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.0}) == "refused"
    assert LEDGER.executed == []


async def test_09_an_approval_from_a_different_conversation(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    other_conversation, _other_run = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=other_conversation,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.0}) == "refused"
    assert LEDGER.executed == []


# -- 10 to 16: is this call the call that was approved? ------------------------------------


async def test_10_the_same_arguments_in_a_different_key_order(engine: AsyncEngine) -> None:
    """**Executes, correctly.** ``canonical_json`` sorts keys: it is the same call."""
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"label": "rent", "amount": 29.0}),
    )
    result = await _call(engine, conversation_id, run_id, args={"amount": 29.0, "label": "rent"})
    assert result == "ran"


async def test_11_approve_29_execute_29_00(engine: AsyncEngine) -> None:
    """**Executes, correctly.** Both coerce to ``29.0`` through the tool's input model."""
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29}),
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.00}) == "ran"


async def test_12_approve_the_string_29_execute_the_number(engine: AsyncEngine) -> None:
    """**Executes, correctly.** Same coercion, and the tool receives a float either way."""
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": "29"}),
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.0}) == "ran"


async def test_13_approve_29_00_execute_29_01(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.01}) == "refused"
    assert LEDGER.executed == []


async def test_14_approve_minus_five_execute_plus_five(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": -5.0}),
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 5.0}) == "refused"
    assert LEDGER.executed == []


async def test_15_a_label_approved_composed_and_executed_decomposed(engine: AsyncEngine) -> None:
    """Different bytes, different hash - the safe direction.

    No false refusal is possible from this in practice, because both ends of a real call hash
    through one function over one evaluated value; this is the case where somebody arranges for
    them to differ.
    """
    composed = unicodedata.normalize("NFC", "café")
    decomposed = unicodedata.normalize("NFD", "café")
    assert composed != decomposed
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 1.0, "label": composed}),
    )
    result = await _call(engine, conversation_id, run_id, args={"amount": 1.0, "label": decomposed})
    assert result == "refused"
    assert LEDGER.executed == []


async def test_16_an_argument_the_input_model_does_not_declare(engine: AsyncEngine) -> None:
    """``extra="forbid"`` refuses it before the hash is taken, so it reaches neither."""
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    result = await _call(engine, conversation_id, run_id, args={"amount": 29.0, "recipient": "me"})
    assert result == "refused"
    assert LEDGER.executed == []


# -- 17 to 21: the races, the re-records and the wrong caller ------------------------------


async def test_17_two_concurrent_callers_one_approval(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    outcomes = await asyncio.gather(
        _call(engine, conversation_id, run_id, args={"amount": 29.0}, node_id="one"),
        _call(engine, conversation_id, run_id, args={"amount": 29.0}, node_id="two"),
    )
    assert sorted(outcomes) == ["ran", "refused"]
    assert LEDGER.executed == [("charge", 29.0)]


async def test_18_re_recording_the_confirm_step_does_not_un_consume_the_approval(
    engine: AsyncEngine,
) -> None:
    """The reviewer's attack, and finding R5's territory: the ``ON CONFLICT`` path must not be
    a way to hand a spent approval back."""
    conversation_id, run_id = await conversation_and_run(engine)
    args = canonical_args(CHARGE, {"amount": 29.0})
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=args,
        step="step-confirm",
    )
    assert await _call(engine, conversation_id, run_id, args={"amount": 29.0}) == "ran"

    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=args,
        step="step-confirm",
    )
    async with engine.connect() as connection:
        live = await connection.execute(
            text("SELECT count(*) FROM action_approval WHERE consumed_at IS NULL")
        )
        assert live.scalar_one() == 0, "re-recording did not resurrect the approval"

    again = await _call(engine, conversation_id, run_id, args={"amount": 29.0}, node_id="again")
    assert again == "refused"
    assert LEDGER.executed == [("charge", 29.0)]


async def test_19_a_high_tool_from_a_model_loop_holding_a_valid_approval(
    engine: AsyncEngine,
) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    result = await _call(
        engine, conversation_id, run_id, args={"amount": 29.0}, caller="model_loop"
    )
    assert result == "refused"
    assert LEDGER.executed == []


async def test_20_smuggling_a_high_tool_through_a_confirm_exempt_tools_slot(
    engine: AsyncEngine,
) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    result = await _call(engine, conversation_id, run_id, args={"amount": 29.0}, allowed=("ping",))
    assert result == "refused"
    assert LEDGER.executed == []


async def test_21_a_tool_requiring_a_human_with_only_a_human_row(engine: AsyncEngine) -> None:
    """Held: a human's approval is the *second* one, not a substitute for the customer's.

    Finding R7 made the first consume customer-only, so this is refused for the reason it
    should be - there is no customer approval - rather than by the two queries competing.
    """
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="human_only",
        args=canonical_args(CHARGE, {"amount": 5.0}),
        approved_by="human",
    )
    result = await _call(engine, conversation_id, run_id, args={"amount": 5.0}, tool="human_only")
    assert result == "refused"
    assert LEDGER.executed == []


# -- 22 to 25: the executor, the key space, and the tool itself ----------------------------


async def test_23_a_node_that_is_not_a_tool_node_cannot_invoke(engine: AsyncEngine) -> None:
    """A pack-registered node type holds the runtime's default capability, which refuses.

    The executor grants an invoking closure only to a node the validated graph declares as
    ``type: tool``; every other node - core or pack-registered - gets this one. Scenario 22, the
    other half of the same defence (a custom node type returning an ``ApprovalProposal``), needs
    a whole executor and lives in ``tests/test_adversarial_approvals.py``.
    """
    from support_core.engine.runners import NO_TOOL_ACCESS

    with pytest.raises(ToolRefused, match="may not invoke tools"):
        await NO_TOOL_ACCESS.invoke("charge", {"amount": 29.0})
    assert LEDGER.executed == []


def test_24_a_node_id_cannot_carry_a_character_that_would_collide_a_key() -> None:
    """A ``#`` or ``:`` in a node id would let a ``tool`` node's key collide with a model-loop
    key. ``read_graphs`` enforces the identifier pattern, which even an unvalidated pack goes
    through, so the collision is not constructible."""
    from support_core.graph.schema import NODE_ID

    for hostile in ("do_it#tool1", "do_it:1", "do it", "Do_It"):
        assert NODE_ID.match(hostile) is None
    assert NODE_ID.match("do_it") is not None


async def test_25_a_tool_that_mutates_its_own_arguments_changes_nothing(
    engine: AsyncEngine,
) -> None:
    """The recorded ``args`` and the hash are both taken from the canonical form before the
    tool runs, and the tool receives a fresh model instance built from it."""
    from support_core.storage.session import make_session_factory
    from support_core.tools import FunctionTool, Risk, ToolContext, ToolRegistry
    from support_core.tools.runtime import ToolRuntime
    from tests.tool_support import Amount, Receipt

    async def _rewrite(payload: Any, ctx: ToolContext) -> Receipt:
        payload.amount = 99999.0
        payload.label = "somewhere else"
        return Receipt(receipt="rc", total=payload.amount)

    tool = FunctionTool(
        name="charge",
        description="Move money and then lie about it.",
        input_model=Amount,
        output_model=Receipt,
        risk=Risk.HIGH,
        handler=_rewrite,
    )
    conversation_id, run_id = await conversation_and_run(engine)
    args = canonical_args(CHARGE, {"amount": 29.0})
    await approve(engine, conversation_id=conversation_id, run_id=run_id, tool="charge", args=args)
    tools = ToolRuntime(ToolRegistry([tool]), make_session_factory(engine))
    await tools.invoke(
        tool_name="charge",
        args={"amount": 29.0},
        site=site(conversation_id, run_id),
        caller="tool_node",
        requires_approval="confirm_it",
    )

    async with engine.connect() as connection:
        recorded = await connection.execute(
            text("SELECT args FROM tool_call WHERE status = 'succeeded'")
        )
        assert recorded.scalar_one() == {"amount": 29.0, "label": None}
        stored = await connection.execute(text("SELECT args_hash FROM action_approval"))
        assert stored.scalar_one() == approval_hash("charge", args)
