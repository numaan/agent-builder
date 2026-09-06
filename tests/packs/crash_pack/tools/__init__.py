"""One tool that has a side effect and can die immediately after having it.

``spend`` appends a line to the file named by ``SUPPORT_TEST_LEDGER`` and then, if
``SUPPORT_TEST_CRASH`` is set, calls :func:`os._exit` - stopping the process between the side
effect and the ``tool_call`` row that records it. That is the exact window DESIGN.md section
8.1's ``idempotent: False`` exists for, and a file is how the count survives the process that
made it.
"""

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from support_core.tools import FunctionTool, Risk, Tool, ToolContext

LEDGER_ENV = "SUPPORT_TEST_LEDGER"
CRASH_ENV = "SUPPORT_TEST_CRASH"
CRASH_EXIT = 9


class SpendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float = 1.0


class SpendOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt: str


async def _spend(payload: Any, ctx: ToolContext) -> SpendOutput:
    ledger = Path(os.environ[LEDGER_ENV])
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(f"{payload.amount}\n")
        handle.flush()
        os.fsync(handle.fileno())
    if os.environ.get(CRASH_ENV):
        # Not an exception: the process stops here, with the side effect done and nothing
        # written down about it.
        os._exit(CRASH_EXIT)
    return SpendOutput(receipt="ok")


SPEND: Tool = FunctionTool(
    name="spend",
    description="Spend money. Not safe to repeat.",
    input_model=SpendInput,
    output_model=SpendOutput,
    risk=Risk.WRITE,
    idempotent=False,
    confirm_exempt=True,
    confirm_exempt_reason="the test is about idempotency, not about confirmation",
    handler=_spend,
)

TOOLS: list[Tool] = [SPEND]
