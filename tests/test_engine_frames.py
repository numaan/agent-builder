"""The frame stack and its gates. Implements DESIGN.md sections 6.1, 6.2 (``gate``) and 6.6.

The property under test is the last sentence of section 6.6: "Gates fire on every entry to a
frame, so an interrupt cannot be used to reach an unverified action." A gate that only fired
when its own node was reached would be trivial to walk around: suspend after it, let the
precondition lapse, come back, and the protected node is next.

The frame stack is also checked for the shape phase 6 needs. ``Frame.kind`` distinguishes a
sub-graph call from a gate redirect from an interrupt, and frames are pushed through one
method, so phase 6 adds interrupt *behaviour* rather than a second kind of stack.
"""

import uuid
from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor, Frame, GraphInvocation, TurnOutcome
from support_core.graph.pack import Pack
from tests.engine_support import (
    PACKS,
    Recorder,
    custom_node_types,
    outbound_texts,
    path,
    run_row,
    set_context,
    trace_rows,
)

CUSTOM_PACK = PACKS / "custom_pack"


@pytest.fixture
def custom_types() -> Iterator[None]:
    with custom_node_types():
        yield None


@pytest.fixture
def pack(custom_types: None) -> Pack:
    return load_pack(CUSTOM_PACK)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def executor(pack: Pack, engine: AsyncEngine, recorder: Recorder) -> Executor:
    return Executor(pack, engine, hooks=recorder.hooks())


def _context(*, verified: bool) -> dict[str, object]:
    return {"customer": {"identity_verified": verified, "attributes": {"mode": "gate"}}}


async def _frames(engine: AsyncEngine, conversation_id: uuid.UUID) -> list[Frame]:
    row = await run_row(engine, conversation_id)
    return [Frame.model_validate(frame) for frame in row["frames"]]


async def test_a_satisfied_gate_is_recorded_on_the_frame(
    executor: Executor, engine: AsyncEngine
) -> None:
    conversation_id = await executor.start_conversation(context=_context(verified=True))
    await executor.on_inbound(conversation_id, "go")

    frames = await _frames(engine, conversation_id)
    assert [frame.kind for frame in frames] == ["root", "subgraph"]
    assert frames[-1].graph_id == "gated"
    assert frames[-1].passed_gates == ["guard"]
    assert frames[-1].node_id == "collect"


async def test_a_gate_cannot_be_walked_around_by_suspending(
    executor: Executor, engine: AsyncEngine
) -> None:
    """The whole of DESIGN.md section 6.6's last sentence, in one test.

    The gate passes, the frame suspends after it, the precondition lapses while the customer is
    away, and the reply must not reach the protected node.
    """
    conversation_id = await executor.start_conversation(context=_context(verified=True))
    outcome = await executor.on_inbound(conversation_id, "go")
    assert outcome.status == "waiting_customer"
    run_id = outcome.run_id
    assert run_id is not None

    await set_context(engine, conversation_id, _context(verified=False))
    await executor.on_inbound(conversation_id, "42")

    visited = await path(engine, run_id)
    assert "reveal" not in visited, "the protected node was reached with the gate unsatisfied"
    assert visited[-3:] == ["guard", "tell", "hold"]
    guard_steps = [step for step in await trace_rows(engine, run_id) if step["node_id"] == "guard"]
    assert [step["edge"] for step in guard_steps] == ["next", "redirect"]
    frames = await _frames(engine, conversation_id)
    assert [frame.kind for frame in frames] == ["root", "subgraph", "gate_redirect"]
    assert frames[-1].return_node == "collect", "the redirect returns to where the frame was"


async def test_the_customer_message_a_firing_gate_swallowed_is_not_lost(
    executor: Executor, engine: AsyncEngine
) -> None:
    """The reply that triggered the re-check is queued again, not dropped."""
    conversation_id = await executor.start_conversation(context=_context(verified=True))
    await executor.on_inbound(conversation_id, "go")
    await set_context(engine, conversation_id, _context(verified=False))
    await executor.on_inbound(conversation_id, "42")

    from tests.engine_support import messages

    inbound = [
        row for row in await messages(engine, conversation_id) if row["direction"] == "inbound"
    ]
    assert [(row["text"], row["status"]) for row in inbound] == [
        ("go", "received"),
        ("42", "pending"),
    ]


async def test_the_protected_node_runs_once_the_gate_is_satisfied_again(
    executor: Executor, engine: AsyncEngine
) -> None:
    conversation_id = await executor.start_conversation(context=_context(verified=True))
    outcome = await executor.on_inbound(conversation_id, "go")
    run_id = outcome.run_id
    assert run_id is not None
    await set_context(engine, conversation_id, _context(verified=False))
    await executor.on_inbound(conversation_id, "42")

    # A human verifies the customer, then hands control back.
    await set_context(engine, conversation_id, _context(verified=True))
    await executor.resume_human(conversation_id)
    # The reply that was put back on the queue is now delivered.
    final = await executor.drain(conversation_id)

    assert final.status == "done"
    assert "reveal" in await path(engine, run_id)
    assert "Protected: 42." in await outbound_texts(engine, conversation_id)
    frames = await _frames(engine, conversation_id)
    assert frames == []


async def test_a_gate_that_never_clears_ends_in_a_handoff_not_in_the_protected_node(
    executor: Executor, engine: AsyncEngine, recorder: Recorder
) -> None:
    """A redirect that cannot satisfy its gate re-pushes until the per-turn limit, then hands off.

    That is the safe direction: the failure mode of an unsatisfiable gate is a human, never a
    walk past the gate. It is also what stops the re-check being an infinite loop.
    """
    conversation_id = await executor.start_conversation(
        context={"customer": {"identity_verified": False, "attributes": {"mode": "gate_loop"}}}
    )
    outcome = await executor.on_inbound(conversation_id, "go")
    run_id = outcome.run_id
    assert run_id is not None

    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_human"
    assert row["awaiting"]["reason"] == "limit_exceeded"
    assert [request.reason for request in recorder.handoffs] == ["limit_exceeded"]
    visited = await path(engine, run_id)
    assert "reveal" not in visited
    assert visited.count("guard") > 1, "the gate really did re-push its redirect"


async def test_frames_are_pushed_by_one_method_whatever_the_reason(
    executor: Executor, engine: AsyncEngine
) -> None:
    """Phase 6 pushes an ``interrupt`` frame; the stack already carries the shape for it.

    Not a test of interrupt *behaviour* (DESIGN.md section 6.6, phase 6): a test that the stored
    frame stack does not assume every pushed frame is a sub-graph call, so phase 6 changes the
    executor and not the schema.
    """
    conversation_id = await executor.start_conversation(context=_context(verified=True))
    await executor.on_inbound(conversation_id, "go")

    outcome = TurnOutcome(conversation_id=conversation_id)
    turn = executor._turn_state(await executor._run_for(conversation_id), outcome)
    before = len(turn.frames)
    executor._push(
        turn,
        GraphInvocation(graph="verify", kind="interrupt", return_node=turn.frame.node_id),
    )

    assert len(turn.frames) == before + 1
    pushed = turn.frames[-1]
    assert pushed.kind == "interrupt"
    assert pushed.frame_seq not in {frame.frame_seq for frame in turn.frames[:-1]}
    assert pushed.return_node == "collect"
    # Round-trips through JSONB exactly as a sub-graph frame does.
    assert Frame.model_validate(pushed.model_dump(mode="json")) == pushed


async def test_frame_sequence_numbers_are_never_reused(
    executor: Executor, engine: AsyncEngine
) -> None:
    """Two invocations of one graph must not share a ``frame_seq``: step ids would collide."""
    conversation_id = await executor.start_conversation(context=_context(verified=True))
    outcome = await executor.on_inbound(conversation_id, "go")
    run_id = outcome.run_id
    assert run_id is not None
    await set_context(engine, conversation_id, _context(verified=False))
    await executor.on_inbound(conversation_id, "42")
    await executor.resume_human(conversation_id)
    await executor.resume_human(conversation_id)

    steps = [step["step_id"] for step in await trace_rows(engine, run_id)]
    assert len(steps) == len(set(steps))
    frame_seqs = [int(step.split(":")[-3]) for step in steps]
    first_seen = list(dict.fromkeys(frame_seqs))
    assert first_seen == sorted(first_seen), (
        "frame_seq is allocated monotonically, so each new frame's number is larger than every "
        "number seen before it"
    )
    verify_frames = {
        int(step.split(":")[-3]) for step in steps if step.split(":")[-2] in {"tell", "hold"}
    }
    assert len(verify_frames) >= 2, "each redirect push is a new frame"


def test_a_node_result_cannot_ask_for_two_ways_out_at_once() -> None:
    """Independent review finding R11.

    ``_advance`` has to pick an order when a result asks to suspend *and* push *and* pop, and
    whichever it picks silently discards the rest. Phase 2's runners never do it; phase 3 and 4
    return richer results, and a dropped ``push_graph`` would be very hard to find.
    """
    from support_core.engine.types import NodeResult, SuspendReason

    assert NodeResult(pop=True).pop
    with pytest.raises(ValidationError, match="only one of suspend, push_graph or pop"):
        NodeResult(
            pop=True,
            push_graph=GraphInvocation(graph="verify"),
        )
    with pytest.raises(ValidationError, match="only one of suspend, push_graph or pop"):
        NodeResult(
            suspend=SuspendReason(status="waiting_customer"),
            push_graph=GraphInvocation(graph="verify"),
        )
