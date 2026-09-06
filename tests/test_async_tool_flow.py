"""The async tool path, driven through the executor (DESIGN.md section 7.2, review finding R1).

Phase 4 tested the two halves of an async call by calling
:meth:`~support_core.tools.runtime.ToolRuntime.complete_async` directly with the same
hand-built ``CallSite`` the dispatch used - which is exactly the assumption the executor
breaks. A dispatching pass claims its idempotency key at attempt *n*; the checkpoint that
records the suspension then increments the frame's attempt counter, so the pass that delivers
the callback computes attempt *n+1* and looks the call up under a key nobody claimed.

The consequence measured by the review: the callback is refused, the ``tool_call`` row is
stranded at ``awaiting_callback`` for ever, and a graph whose ``on_error`` returns to the tool
node dispatches a **second** time - two side effects from one customer intent and one callback.

Both tests here fail against the reviewed code: the first with two ledger entries and two
``awaiting_callback`` rows, the second with a handoff instead of a completed workflow.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.engine import Executor
from support_core.graph.pack import Pack
from tests.engine_support import PACKS, Recorder, path, run_row, tool_calls
from tests.tool_support import LEDGER, unvalidated_pack

ASYNC_PACK = PACKS / "async_pack"


@pytest.fixture(autouse=True)
def ledger() -> None:
    LEDGER.reset()


def pack_for(entry: str) -> Pack:
    pack = unvalidated_pack(ASYNC_PACK)
    pack.manifest.entry_graph = entry
    return pack


def build(entry: str, engine: AsyncEngine) -> tuple[Executor, Recorder]:
    recorder = Recorder()
    return Executor(pack_for(entry), engine, hooks=recorder.hooks()), recorder


async def dispatched(executor: Executor, engine: AsyncEngine) -> uuid.UUID:
    """Run the turn that dispatches the async tool and assert it suspended cleanly."""
    conversation_id = await executor.start_conversation()
    await executor.on_inbound(conversation_id, "do the long thing")
    row = await run_row(engine, conversation_id)
    assert row["status"] == "waiting_async_tool"
    assert LEDGER.executed == [("dispatch_async", 5.0)], "dispatched exactly once"
    calls = await tool_calls(engine, conversation_id)
    assert [call["status"] for call in calls] == ["awaiting_callback"]
    return conversation_id


async def test_a_dispatched_async_tool_is_completed_by_its_callback(engine: AsyncEngine) -> None:
    """The round trip the phase never ran: dispatch, suspend, callback, carry on.

    One handler invocation, one ``tool_call`` row, and it ends ``succeeded`` with the result the
    callback carried mapped into state through ``into``.
    """
    executor, _recorder = build("plain", engine)
    conversation_id = await dispatched(executor, engine)

    outcome = await executor.resume_async_tool(conversation_id, payload={"ok": True})

    assert outcome.status == "done"
    assert LEDGER.executed == [("dispatch_async", 5.0)], "the handler ran once, not twice"
    calls = await tool_calls(engine, conversation_id)
    assert len(calls) == 1, f"one call, not {len(calls)}"
    assert calls[0]["status"] == "succeeded"
    assert calls[0]["result"] == {"ok": True}
    row = await run_row(engine, conversation_id)
    assert await path(engine, row["id"]) == ["dispatch_it", "dispatch_it", "tell_them", "done"]
    assert row["frames"] == []


async def test_a_callback_does_not_make_the_tool_node_dispatch_a_second_time(
    engine: AsyncEngine,
) -> None:
    """Review finding R1's measured scenario, exactly.

    The graph's ``on_error`` returns to the tool node, so a callback that cannot find its call
    is not merely lost: the node re-enters, claims a fresh key, and has the side effect again.
    The ledger is the only honest answer to "did it happen twice", and it says once.
    """
    executor, _recorder = build("root", engine)
    conversation_id = await dispatched(executor, engine)

    outcome = await executor.resume_async_tool(conversation_id, payload={"ok": True})

    assert LEDGER.executed == [("dispatch_async", 5.0)], "one intent, one callback, one effect"
    assert outcome.status == "done"
    calls = await tool_calls(engine, conversation_id)
    assert [call["status"] for call in calls] == ["succeeded"]
    assert not any(call["status"] == "awaiting_callback" for call in calls), (
        "no call is stranded waiting for a callback that already arrived"
    )
