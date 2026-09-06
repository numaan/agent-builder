"""A process that dies between the side effect and the record. DESIGN.md sections 7.3 and 8.1.

    Engine crash mid-node: on resume, the step is re-executed. Tool idempotency keys make
    re-execution safe. - DESIGN.md section 7.3

    ``idempotent: bool = True`` - if False, runtime enforces at-most-once via key.
    - DESIGN.md section 8.1

The in-process version of this is in ``tests/test_tool_runtime.py``; it is a good model of a
dead process but it is still a model - the same interpreter, the same event loop, a connection
closed politely. This one is the real thing, in the tradition of phase 2's
``tests/engine_child.py``: a separate OS process calls :func:`os._exit` *inside the tool*, after
the money has moved and before anything has been written down about it. The count of side
effects lives in a file, because that is the only thing that survives the process that made it.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.engine import Executor
from tests.engine_support import PACKS, outbound_texts, path, run_row, tool_calls

CRASH_PACK = PACKS / "crash_pack"
CRASH_EXIT = 9
REPO_ROOT = Path(__file__).resolve().parent.parent


def _child(
    conversation_id: uuid.UUID, ledger: Path, *, crash: bool
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["SUPPORT_TEST_LEDGER"] = str(ledger)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    if crash:
        environment["SUPPORT_TEST_CRASH"] = "1"
    else:
        environment.pop("SUPPORT_TEST_CRASH", None)
    options = {
        "mode": "run",
        "pack": str(CRASH_PACK),
        "conversation": str(conversation_id),
        "text": "spend it",
    }
    return subprocess.run(
        [sys.executable, "-m", "tests.engine_child", json.dumps(options)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    return tmp_path / "ledger.txt"


async def test_a_process_killed_after_the_tool_ran_does_not_run_it_again(
    engine: AsyncEngine, ledger: Path
) -> None:
    """The property the whole phase turns on: one refund, whatever the crash did.

    The child dies inside ``spend``, with the line written and no ``tool_call`` result. What it
    leaves behind is a ``running`` row - a call that may or may not have happened - and for a
    non-idempotent tool the only honest answer is to refuse rather than repeat. The conversation
    routes to ``on_error`` and tells the customer a person will check.
    """
    os.environ["SUPPORT_TEST_LEDGER"] = str(ledger)
    executor = Executor(load_pack(CRASH_PACK), engine)
    conversation_id = await executor.start_conversation()

    crashed = _child(conversation_id, ledger, crash=True)
    assert crashed.returncode == CRASH_EXIT, crashed.stderr
    assert ledger.read_text(encoding="utf-8").split() == ["7.0"], "the money moved once"

    async with engine.connect() as connection:
        status = await connection.execute(text("SELECT status FROM tool_call"))
        assert status.scalar_one() == "running", "claimed, with no outcome recorded"
        run_status = await connection.execute(
            text("SELECT status FROM run WHERE conversation_id = :c"), {"c": conversation_id}
        )
        assert run_status.scalar_one() == "running", "and the turn never finished"

    # A different process picks the conversation up, as `recover_stalled` or the next message
    # would. The step re-executes under the same id, which is the whole point.
    outcome = await executor.drain(conversation_id)

    assert ledger.read_text(encoding="utf-8").split() == ["7.0"], "and only once"
    assert outcome.status == "done"
    row = await run_row(engine, conversation_id)
    assert "gave_up" in await path(engine, row["id"])
    assert "A person will check" in " ".join(await outbound_texts(engine, conversation_id))

    calls = await tool_calls(engine, conversation_id)
    assert len(calls) == 1, "one row, re-entered rather than duplicated"
    assert calls[0]["status"] == "indeterminate"
    assert calls[0]["attempts"] == 2


async def test_the_same_conversation_without_a_crash_completes(
    engine: AsyncEngine, ledger: Path
) -> None:
    """The control: the same pack, the same child process, no crash."""
    executor = Executor(load_pack(CRASH_PACK), engine)
    conversation_id = await executor.start_conversation()
    finished = _child(conversation_id, ledger, crash=False)
    assert finished.returncode == 0, finished.stderr
    assert json.loads(finished.stdout)["status"] == "done"
    assert ledger.read_text(encoding="utf-8").split() == ["7.0"]

    calls = await tool_calls(engine, conversation_id)
    assert [call["status"] for call in calls] == ["succeeded"]
    row = await run_row(engine, conversation_id)
    assert "gave_up" not in await path(engine, row["id"])
