"""The tool runtime. DESIGN.md sections 8.1 and 8.2, and 7.3's crash semantics.

These tests drive :class:`~support_core.tools.runtime.ToolRuntime` directly, against real
Postgres, because what is being asked is what the *runtime* does - not what a graph makes it do.
A test that could only reach a refusal through a graph the validator accepts would be testing the
validator; the graph-level tests are in ``tests/test_adversarial_approvals.py``.
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.storage.session import make_session_factory
from support_core.tools import Risk, ToolFailed, ToolRefused
from support_core.tools.approval import approval_hash, canonical_args, canonical_json
from support_core.tools.runtime import ToolCallResult, ToolRuntime
from tests.tool_support import (
    CHARGE,
    LEDGER,
    approve,
    conversation_and_run,
    runtime,
    site,
)


@pytest.fixture(autouse=True)
def ledger() -> None:
    LEDGER.reset()


# -- the risk policy (DESIGN.md section 8.2's table) -------------------------------------


async def test_a_read_tool_needs_no_approval(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    result = await runtime(engine).invoke(
        tool_name="peek",
        args={"amount": 1.0},
        site=site(conversation_id, run_id),
        caller="tool_node",
    )
    assert result.output_json["receipt"] == "peeked"
    assert result.approval_id is None


async def test_a_write_tool_without_an_approval_is_refused(engine: AsyncEngine) -> None:
    """The rule PLAN.md says reviewers check on every phase, at the point that enforces it."""
    conversation_id, run_id = await conversation_and_run(engine)
    with pytest.raises(ToolRefused, match="no live approval"):
        await runtime(engine).invoke(
            tool_name="nudge",
            args={"amount": 1.0},
            site=site(conversation_id, run_id),
            caller="tool_node",
            requires_approval="confirm_it",
        )
    assert LEDGER.executed == []


async def test_a_high_tool_that_names_no_confirm_node_is_refused(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args={"amount": 29.0, "label": None},
    )
    with pytest.raises(ToolRefused, match="names no confirm node"):
        await runtime(engine).invoke(
            tool_name="charge",
            args={"amount": 29.0},
            site=site(conversation_id, run_id),
            caller="tool_node",
        )
    assert LEDGER.executed == []


async def test_a_confirm_exempt_write_tool_runs_without_one(engine: AsyncEngine) -> None:
    """DESIGN.md section 8.2's named exception, and only for the tool that declares it."""
    conversation_id, run_id = await conversation_and_run(engine)
    result = await runtime(engine).invoke(
        tool_name="ping",
        args={},
        site=site(conversation_id, run_id),
        caller="tool_node",
    )
    assert result.approval_id is None
    assert LEDGER.executed == [("ping", 0.0)]


async def test_a_write_tool_is_refused_from_a_model_loop_even_with_an_approval(
    engine: AsyncEngine,
) -> None:
    """The first row of DESIGN.md 8.2's table is unconditional: WRITE from a model loop, never.

    Even holding a valid, unconsumed approval for exactly these arguments, the runtime refuses,
    because the caller is the wrong kind of caller. This is the second lock behind the read-only
    gateway, and it is the one that would still hold if the gateway had a bug.
    """
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    with pytest.raises(ToolRefused, match="cannot be called from a model tool loop"):
        await runtime(engine).invoke(
            tool_name="charge",
            args={"amount": 29.0},
            site=site(conversation_id, run_id),
            caller="model_loop",
            requires_approval="confirm_it",
        )
    assert LEDGER.executed == []


async def test_a_tool_a_node_did_not_declare_is_refused(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    with pytest.raises(ToolRefused, match="not a tool this step may call"):
        await runtime(engine).invoke(
            tool_name="peek",
            args={"amount": 1.0},
            site=site(conversation_id, run_id),
            caller="tool_node",
            allowed=("nudge",),
        )


async def test_a_tool_the_pack_does_not_export_is_refused(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    with pytest.raises(ToolRefused, match="no tool named 'wire_money'"):
        await runtime(engine).invoke(
            tool_name="wire_money",
            args={},
            site=site(conversation_id, run_id),
            caller="tool_node",
        )


async def test_a_tool_needing_human_approval_is_refused_without_one(engine: AsyncEngine) -> None:
    """Phase 6 owns the desk that records a human approval; until then the tool cannot run."""
    conversation_id, run_id = await conversation_and_run(engine)
    args = canonical_args(CHARGE, {"amount": 5.0})
    await approve(
        engine, conversation_id=conversation_id, run_id=run_id, tool="human_only", args=args
    )
    with pytest.raises(ToolRefused, match="no human has approved"):
        await runtime(engine).invoke(
            tool_name="human_only",
            args={"amount": 5.0},
            site=site(conversation_id, run_id),
            caller="tool_node",
            requires_approval="confirm_it",
        )
    assert LEDGER.executed == []
    # And the customer's approval was not spent by the attempt.
    async with engine.connect() as connection:
        unconsumed = await connection.execute(
            text("SELECT count(*) FROM action_approval WHERE consumed_at IS NULL")
        )
        assert unconsumed.scalar_one() == 1


# -- the approval binding (DESIGN.md section 8.2) -----------------------------------------


async def test_an_approval_for_different_arguments_does_not_authorise_the_call(
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
    with pytest.raises(ToolRefused, match="no live approval"):
        await runtime(engine).invoke(
            tool_name="charge",
            args={"amount": 2900.0},
            site=site(conversation_id, run_id),
            caller="tool_node",
            requires_approval="confirm_it",
        )
    assert LEDGER.executed == []


async def test_an_approval_for_a_different_tool_does_not_authorise_the_call(
    engine: AsyncEngine,
) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="nudge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
    )
    with pytest.raises(ToolRefused, match="no live approval"):
        await runtime(engine).invoke(
            tool_name="charge",
            args={"amount": 29.0},
            site=site(conversation_id, run_id),
            caller="tool_node",
            requires_approval="confirm_it",
        )


async def test_an_approval_from_another_confirm_node_does_not_authorise_the_call(
    engine: AsyncEngine,
) -> None:
    """``requires_approval`` names one confirm node, and the runtime checks that it is that one.

    Otherwise a graph with two confirmations for two actions could spend either approval on
    either call whenever the arguments happened to match.
    """
    conversation_id, run_id = await conversation_and_run(engine)
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=canonical_args(CHARGE, {"amount": 29.0}),
        node_id="confirm_something_else",
    )
    with pytest.raises(ToolRefused, match="no live approval"):
        await runtime(engine).invoke(
            tool_name="charge",
            args={"amount": 29.0},
            site=site(conversation_id, run_id),
            caller="tool_node",
            requires_approval="confirm_it",
        )


async def test_an_approval_from_an_earlier_frame_does_not_authorise_a_later_one(
    engine: AsyncEngine,
) -> None:
    """A workflow entered twice is two invocations, and one approval belongs to one of them.

    ``frame_seq`` is monotonic and never reused (DESIGN.md section 7.1), so the same graph
    reached again gets a new one, and last time's unspent approval does not travel with it.
    """
    conversation_id, run_id = await conversation_and_run(engine)
    args = canonical_args(CHARGE, {"amount": 29.0})
    await approve(
        engine,
        conversation_id=conversation_id,
        run_id=run_id,
        tool="charge",
        args=args,
        frame_seq=1,
    )
    with pytest.raises(ToolRefused, match="in this frame"):
        await runtime(engine).invoke(
            tool_name="charge",
            args={"amount": 29.0},
            site=site(conversation_id, run_id, frame_seq=7),
            caller="tool_node",
            requires_approval="confirm_it",
        )
    assert LEDGER.executed == []


async def test_an_approval_is_single_use(engine: AsyncEngine) -> None:
    """Phase-0 review finding N1: one approval, one call, whatever the arguments say."""
    conversation_id, run_id = await conversation_and_run(engine)
    args = canonical_args(CHARGE, {"amount": 29.0})
    await approve(engine, conversation_id=conversation_id, run_id=run_id, tool="charge", args=args)
    tools = runtime(engine)
    first = await tools.invoke(
        tool_name="charge",
        args={"amount": 29.0},
        site=site(conversation_id, run_id, attempt=0),
        caller="tool_node",
        requires_approval="confirm_it",
    )
    assert first.approval_id is not None
    with pytest.raises(ToolRefused, match="no live approval"):
        await tools.invoke(
            tool_name="charge",
            args={"amount": 29.0},
            # A different step, so idempotency does not answer for the approval.
            site=site(conversation_id, run_id, node_id="do_it_again"),
            caller="tool_node",
            requires_approval="confirm_it",
        )
    assert LEDGER.executed == [("charge", 29.0)]


async def test_two_racing_callers_cannot_both_spend_one_approval(engine: AsyncEngine) -> None:
    """The consume is atomic in the database, not in the conversation lock.

    The advisory lock already serialises two turns of one conversation, so this races the
    approval *underneath* that: two callers, two connections, two different idempotency keys,
    one approval. Exactly one may win, and the loser must not have moved money.
    """
    conversation_id, run_id = await conversation_and_run(engine)
    args = canonical_args(CHARGE, {"amount": 29.0})
    await approve(engine, conversation_id=conversation_id, run_id=run_id, tool="charge", args=args)

    async def attempt(node_id: str) -> str:
        try:
            await runtime(engine).invoke(
                tool_name="charge",
                args={"amount": 29.0},
                site=site(conversation_id, run_id, node_id=node_id),
                caller="tool_node",
                requires_approval="confirm_it",
            )
        except ToolRefused:
            return "refused"
        return "ran"

    outcomes = await asyncio.gather(attempt("do_it"), attempt("do_it_too"))
    assert sorted(outcomes) == ["ran", "refused"]
    assert LEDGER.executed == [("charge", 29.0)]


# -- idempotency (DESIGN.md sections 7.3, 8.1) --------------------------------------------


async def test_the_same_step_replays_a_completed_call_without_running_it_again(
    engine: AsyncEngine,
) -> None:
    """DESIGN.md 7.3: "the step is re-executed. Tool idempotency keys make re-execution safe."""
    conversation_id, run_id = await conversation_and_run(engine)
    tools = runtime(engine)
    where = site(conversation_id, run_id)
    first = await tools.invoke(
        tool_name="peek", args={"amount": 3.0}, site=where, caller="tool_node"
    )
    second = await tools.invoke(
        tool_name="peek", args={"amount": 3.0}, site=where, caller="tool_node"
    )
    assert LEDGER.executed == [("peek", 3.0)]
    assert second.replayed and not first.replayed
    assert second.output_json == first.output_json
    assert second.tool_call_id == first.tool_call_id

    async with engine.connect() as connection:
        attempts = await connection.execute(text("SELECT attempts FROM tool_call"))
        assert attempts.scalar_one() == 2


async def test_a_non_idempotent_call_whose_outcome_is_unknown_is_never_repeated(
    engine: AsyncEngine,
) -> None:
    """The crash this phase exists to survive: killed *after* the tool ran, before it recorded.

    The row is claimed before the call, so what a dead process leaves behind is a ``running``
    row - a call that may or may not have happened. For a non-idempotent tool the answer is a
    refusal, which the graph routes to ``on_error`` and, in the sample pack, to a human.
    """
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    sessions = make_session_factory(engine)
    args = canonical_args(CHARGE, {"amount": 29.0})
    await approve(engine, conversation_id=conversation_id, run_id=run_id, tool="charge", args=args)

    class Killed(ToolRuntime):
        """Dies between the side effect and the record, as a process would."""

        async def _execute(self, **kwargs: Any) -> ToolCallResult:
            raise SystemExit(9)

    killed = Killed(runtime(engine).registry, sessions)
    with pytest.raises(SystemExit):
        await killed.invoke(
            tool_name="charge",
            args={"amount": 29.0},
            site=where,
            caller="tool_node",
            requires_approval="confirm_it",
        )
    # Pretend the tool did run before the process died: this is the ambiguous state.
    LEDGER.executed.append(("charge", 29.0))

    async with engine.connect() as connection:
        status = await connection.execute(text("SELECT status FROM tool_call"))
        assert status.scalar_one() == "running"

    with pytest.raises(ToolRefused, match="at-most-once"):
        await runtime(engine).invoke(
            tool_name="charge",
            args={"amount": 29.0},
            site=where,
            caller="tool_node",
            requires_approval="confirm_it",
        )
    assert LEDGER.executed == [("charge", 29.0)], "exactly once, whatever the crash did"

    async with engine.connect() as connection:
        after = await connection.execute(text("SELECT status, attempts FROM tool_call"))
        row = after.one()
        assert row.status == "indeterminate"
        assert row.attempts == 2


async def test_an_idempotent_call_whose_outcome_is_unknown_is_repeated(
    engine: AsyncEngine,
) -> None:
    """The other half of the same rule: ``idempotent: true`` is a promise, and it is used."""
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    sessions = make_session_factory(engine)
    async with sessions() as session, session.begin():
        from datetime import UTC, datetime

        from support_core.storage import repositories as repo

        await repo.start_tool_call(
            session,
            repo.ToolCallStart(
                idempotency_key=where.step_id,
                run_id=run_id,
                step_id=where.step_id,
                node_id=where.node_id,
                tool="charge_again",
                risk="high",
                args={"amount": 29.0, "label": None},
                created_at=datetime.now(UTC),
            ),
        )
    result = await runtime(engine).invoke(
        tool_name="charge_again", args={"amount": 29.0}, site=where, caller="tool_node"
    )
    assert not result.replayed
    assert LEDGER.executed == [("charge", 29.0)]


async def test_a_second_call_under_one_key_with_different_arguments_is_refused(
    engine: AsyncEngine,
) -> None:
    """The idempotency key belongs to the call it was claimed for.

    A step re-executed after a crash computes the same arguments, because the frame state comes
    from the same checkpoint. If it ever did not - a pack changed under a suspended run, a bug -
    the alternatives are executing new arguments under the approval that authorised the old ones,
    or handing back the old result as the answer to a new question. Both are worse than a
    refusal that a human reads.
    """
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    tools = runtime(engine)
    await tools.invoke(tool_name="peek", args={"amount": 1.0}, site=where, caller="tool_node")
    with pytest.raises(ToolRefused, match="with different arguments"):
        await tools.invoke(tool_name="peek", args={"amount": 2.0}, site=where, caller="tool_node")
    assert LEDGER.executed == [("peek", 1.0)]


async def test_a_recorded_failure_is_replayed_rather_than_retried(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    tools = runtime(engine)
    with pytest.raises(ToolFailed, match="the world said no"):
        await tools.invoke(tool_name="boom", args={}, site=where, caller="tool_node")
    with pytest.raises(ToolFailed, match="already failed under this step"):
        await tools.invoke(tool_name="boom", args={}, site=where, caller="tool_node")
    assert LEDGER.executed == [("boom", 0.0)]


async def test_a_model_loop_call_gets_its_own_key_per_call(engine: AsyncEngine) -> None:
    """One ``llm`` node's step can make several READ calls, so the key needs a suffix."""
    from support_core.tools.runtime import RegistryToolRunner

    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id, node_id="find_it")
    runner = RegistryToolRunner(runtime(engine), where)
    first = await runner.invoke("peek", {"amount": 1.0}, call_id="a", step_id=where.step_id)
    second = await runner.invoke("peek", {"amount": 2.0}, call_id="b", step_id=where.step_id)
    assert not first.is_error and not second.is_error
    assert LEDGER.executed == [("peek", 1.0), ("peek", 2.0)]
    async with engine.connect() as connection:
        keys = await connection.execute(text("SELECT idempotency_key FROM tool_call ORDER BY 1"))
        assert [row[0] for row in keys] == [f"{where.step_id}#tool1", f"{where.step_id}#tool2"]


# -- failure, timeouts and the context patch ----------------------------------------------


async def test_a_tool_that_raises_records_the_failure(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    with pytest.raises(ToolFailed, match="the world said no"):
        await runtime(engine).invoke(
            tool_name="boom", args={}, site=site(conversation_id, run_id), caller="tool_node"
        )
    async with engine.connect() as connection:
        row = (await connection.execute(text("SELECT status, error FROM tool_call"))).one()
        assert row.status == "failed"
        assert "the world said no" in row.error


async def test_a_tool_that_overruns_its_timeout_fails(engine: AsyncEngine) -> None:
    from support_core.tools import FunctionTool, ToolRegistry

    async def _slow(payload: object, ctx: object) -> object:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    from tests.tool_support import Empty, Flag

    slow = FunctionTool(
        name="slow",
        description="Never finishes in time.",
        input_model=Empty,
        output_model=Flag,
        risk=Risk.READ,
        timeout_s=0.05,
        handler=_slow,
    )
    conversation_id, run_id = await conversation_and_run(engine)
    tools = ToolRuntime(ToolRegistry([slow]), make_session_factory(engine))
    with pytest.raises(ToolFailed, match="timed out"):
        await tools.invoke(
            tool_name="slow", args={}, site=site(conversation_id, run_id), caller="tool_node"
        )


async def test_a_read_tool_may_not_change_the_conversation_context(engine: AsyncEngine) -> None:
    """READ means "no side effects" (DESIGN.md section 8.1), and identity is a side effect."""
    conversation_id, run_id = await conversation_and_run(engine)
    with pytest.raises(ToolFailed, match="read-tier"):
        await runtime(engine).invoke(
            tool_name="sneak",
            args={"amount": 1.0},
            site=site(conversation_id, run_id),
            caller="tool_node",
        )


async def test_a_write_tool_may_change_the_customer_context(engine: AsyncEngine) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    result = await runtime(engine).invoke(
        tool_name="verify",
        args={},
        site=site(conversation_id, run_id, verified=False),
        caller="tool_node",
    )
    assert result.customer_patch is not None
    assert result.customer_patch["identity_verified"] is True
    assert result.customer_patch["ref"] == "cus_1", "the patch is a whole customer, merged"


# -- async tools (DESIGN.md section 7.2) --------------------------------------------------


async def test_an_async_tool_is_dispatched_and_completed_by_its_callback(
    engine: AsyncEngine,
) -> None:
    from support_core.tools import FunctionTool, ToolRegistry
    from tests.tool_support import Amount, Flag, _dispatch

    dispatcher = FunctionTool(
        name="dispatch",
        description="A long-running write.",
        input_model=Amount,
        output_model=Flag,
        risk=Risk.WRITE,
        confirm_exempt=True,
        confirm_exempt_reason="dispatching is not the act",
        async_=True,
        handler=_dispatch,
    )
    conversation_id, run_id = await conversation_and_run(engine)
    where = site(conversation_id, run_id)
    tools = ToolRuntime(ToolRegistry([dispatcher]), make_session_factory(engine))

    started = await tools.invoke(
        tool_name="dispatch", args={"amount": 4.0}, site=where, caller="tool_node"
    )
    assert started.pending
    async with engine.connect() as connection:
        status = await connection.execute(text("SELECT status FROM tool_call"))
        assert status.scalar_one() == "awaiting_callback"

    finished = await tools.complete_async(tool_name="dispatch", site=where, payload={"ok": True})
    assert not finished.pending
    assert finished.output_json == {"ok": True}
    async with engine.connect() as connection:
        status = await connection.execute(text("SELECT status FROM tool_call"))
        assert status.scalar_one() == "succeeded"


async def test_a_callback_for_a_call_that_was_never_dispatched_is_refused(
    engine: AsyncEngine,
) -> None:
    conversation_id, run_id = await conversation_and_run(engine)
    with pytest.raises(ToolRefused, match="to complete"):
        await runtime(engine).complete_async(
            tool_name="ping", site=site(conversation_id, run_id), payload={"ok": True}
        )


# -- the hash itself (DESIGN.md section 8.2) ----------------------------------------------


def test_the_hash_is_exactly_what_the_design_says() -> None:
    args = {"amount": 29.0, "label": None}
    assert approval_hash("charge", args) == _sha256("charge" + canonical_json(args))


def test_key_order_and_whitespace_do_not_change_the_hash() -> None:
    assert approval_hash("charge", {"amount": 29.0, "label": "x"}) == approval_hash(
        "charge", {"label": "x", "amount": 29.0}
    )


def test_arguments_are_canonicalised_through_the_tools_own_input_model() -> None:
    """``29`` and ``29.0`` are the same call when the input is a float, and a different call
    when the value is different. The model is what decides, not the YAML spelling."""
    assert canonical_args(CHARGE, {"amount": 29}) == canonical_args(CHARGE, {"amount": 29.0})
    assert approval_hash("charge", canonical_args(CHARGE, {"amount": 29})) == approval_hash(
        "charge", canonical_args(CHARGE, {"amount": 29.0})
    )
    assert canonical_args(CHARGE, {"amount": 29}) != canonical_args(CHARGE, {"amount": 29.5})


def test_an_argument_the_tool_does_not_declare_is_refused_rather_than_hashed() -> None:
    """An argument the input model does not declare never reaches the hash *or* the tool.

    This tool's model forbids extras, so the call is refused outright, which is the strongest
    answer. A tool whose model *allowed* extras would have them dropped by ``model_validate``
    before the hash is taken - so the hash still covers exactly what the tool receives, which is
    the property DESIGN.md section 8.2 needs. Neither shape lets an undeclared argument ride
    along unhashed.
    """
    with pytest.raises(ToolRefused, match="do not fit the input"):
        canonical_args(CHARGE, {"amount": 1.0, "sneaky": True})


def test_arguments_that_do_not_fit_the_input_model_are_refused_not_hashed() -> None:
    with pytest.raises(ToolRefused, match="do not fit the input"):
        canonical_args(CHARGE, {"amount": "several"})


def test_a_value_json_cannot_represent_is_refused() -> None:
    with pytest.raises(ToolRefused, match="cannot be canonicalised"):
        canonical_json({"amount": float("nan")})


def _sha256(text_: str) -> str:
    import hashlib

    return hashlib.sha256(text_.encode("utf-8")).hexdigest()


def test_the_step_id_is_the_idempotency_key(engine: AsyncEngine) -> None:
    """DESIGN.md section 7.1 names the key, and phase 2 proved the id is stable across a crash."""
    from support_core.engine.types import step_id

    run = uuid.uuid4()
    assert step_id(run, 3, "issue_refund", 1) == f"{run}:3:issue_refund:1"
