"""A separate OS process that drives the engine. Used by the durability and concurrency tests.

An in-process "crash" (raise, throw the executor away, dispose the engine) is a good model of a
dead process, but it is still a model: the same interpreter, the same event loop, and a
connection closed politely. This script is the real thing. It connects to the same Postgres,
runs a turn, and either

* ``crash``: calls :func:`os._exit` at a named probe point, so the process stops between one
  machine instruction and the next, with an open transaction and a connection the server has to
  notice is gone; or
* ``run``: processes a message normally, optionally sleeping inside the turn while holding the
  conversation's advisory lock, so another process genuinely contends for it.

Usage::

    python -m tests.engine_child '{"mode": "crash", "pack": "...", ...}'

It prints one JSON object on stdout when it does not crash.
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - only when run as a script
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from support_core import load_pack  # noqa: E402
from support_core.engine import Executor  # noqa: E402
from support_core.engine.hooks import EngineHooks  # noqa: E402
from support_core.storage.config import test_database_url  # noqa: E402

EXIT_CRASHED = 9


async def main(options: dict[str, Any]) -> int:
    engine = create_async_engine(test_database_url(), poolclass=NullPool)
    pack = load_pack(options["pack"])
    seen: dict[str, int] = {}

    async def probe(point: str, detail: dict[str, Any]) -> None:
        if point != options.get("crash_at"):
            if point == options.get("sleep_at") and seen.get("sleep", 0) == 0:
                seen["sleep"] = 1
                await asyncio.sleep(options.get("sleep_seconds", 0.0))
            return
        count = seen.get(point, 0)
        seen[point] = count + 1
        if count == options.get("crash_after", 0):
            # Not an exception: the process stops existing. No unwinding, no rollback sent,
            # no lock released by us - Postgres has to do it when the connection dies.
            sys.stdout.flush()
            os._exit(EXIT_CRASHED)

    executor = Executor(
        pack,
        engine,
        hooks=EngineHooks(probe=probe),
        lock_wait_seconds=options.get("lock_wait_seconds", 30.0),
    )
    conversation_id = uuid.UUID(options["conversation"])
    outcome = await executor.on_inbound(conversation_id, options.get("text", "hello"))
    await engine.dispose()
    print(
        json.dumps(
            {
                "status": outcome.status,
                "queued": outcome.queued,
                "messages_processed": outcome.messages_processed,
                "steps": outcome.steps,
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(asyncio.run(main(json.loads(sys.argv[1]))))
