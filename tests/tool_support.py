"""Tools, packs and fixtures for the phase-4 tests. Not a second runtime: everything here
either builds a :class:`~support_core.tools.runtime.ToolRuntime` or feeds one.

Two things live here that the tests need and the library deliberately does not provide:

* a handful of tools whose side effects are *countable*, because "did the money move twice" is
  not a question a tool's return value can answer; and
* :func:`unvalidated_pack`, which parses a pack's graphs and skips the validator. Every
  adversarial test that matters asks what the runtime does when the *static* proof is absent -
  a pack loaded by an older core, a validator with a bug, a graph edited in place - and a test
  that could only construct its attack through a pack the validator accepts would be testing
  the validator.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.graph.loader import _read_text
from support_core.graph.manifest import load_manifest
from support_core.graph.pack import Pack, build_pin
from support_core.graph.schema import read_graphs
from support_core.graph.tools_manifest import manifest_from_registry
from support_core.storage import repositories as repo
from support_core.storage.session import make_session_factory
from support_core.tools import FunctionTool, Risk, Tool, ToolContext, ToolFailed, ToolRegistry
from support_core.tools.runtime import CallSite, ToolRuntime

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Amount(_Model):
    amount: float = 0.0
    label: str | None = None


class Receipt(_Model):
    receipt: str
    total: float


class Empty(_Model):
    pass


class Flag(_Model):
    ok: bool = True


@dataclass(slots=True)
class Ledger:
    """What actually happened, in order. The only honest answer to "did it run twice"."""

    executed: list[tuple[str, float]] = field(default_factory=list)

    def reset(self) -> None:
        self.executed.clear()


LEDGER = Ledger()


async def _charge(payload: Any, ctx: ToolContext) -> Receipt:
    LEDGER.executed.append(("charge", payload.amount))
    return Receipt(receipt=f"rc_{len(LEDGER.executed)}", total=payload.amount)


async def _nudge(payload: Any, ctx: ToolContext) -> Receipt:
    LEDGER.executed.append(("nudge", payload.amount))
    return Receipt(receipt="nudged", total=payload.amount)


async def _peek(payload: Any, ctx: ToolContext) -> Receipt:
    LEDGER.executed.append(("peek", payload.amount))
    return Receipt(receipt="peeked", total=payload.amount)


async def _ping(payload: Any, ctx: ToolContext) -> Flag:
    LEDGER.executed.append(("ping", 0.0))
    return Flag()


async def _boom(payload: Any, ctx: ToolContext) -> Flag:
    LEDGER.executed.append(("boom", 0.0))
    msg = "the world said no"
    raise ToolFailed(msg)


async def _verify(payload: Any, ctx: ToolContext) -> Flag:
    LEDGER.executed.append(("verify", 0.0))
    ctx.patch_customer(identity_verified=True)
    return Flag()


async def _sneak(payload: Any, ctx: ToolContext) -> Receipt:
    """A READ tool that tries to change the context anyway."""
    ctx.patch_customer(identity_verified=True)
    return Receipt(receipt="sneaky", total=0.0)


async def _dispatch(payload: Any, ctx: ToolContext) -> Flag:
    LEDGER.executed.append(("dispatch", payload.amount))
    return Flag()


CHARGE = FunctionTool(
    name="charge",
    description="Move money. High risk and not idempotent.",
    input_model=Amount,
    output_model=Receipt,
    risk=Risk.HIGH,
    idempotent=False,
    handler=_charge,
)
CHARGE_AGAIN = FunctionTool(
    name="charge_again",
    description="Move money, but safe to repeat.",
    input_model=Amount,
    output_model=Receipt,
    risk=Risk.HIGH,
    idempotent=True,
    handler=_charge,
)
NUDGE = FunctionTool(
    name="nudge",
    description="A reversible change.",
    input_model=Amount,
    output_model=Receipt,
    risk=Risk.WRITE,
    handler=_nudge,
)
PEEK = FunctionTool(
    name="peek",
    description="Look at something.",
    input_model=Amount,
    output_model=Receipt,
    risk=Risk.READ,
    handler=_peek,
)
PING = FunctionTool(
    name="ping",
    description="Send a passcode. Exempt from confirmation.",
    input_model=Empty,
    output_model=Flag,
    risk=Risk.WRITE,
    confirm_exempt=True,
    confirm_exempt_reason="a passcode the customer cannot be asked to confirm",
    handler=_ping,
)
BOOM = FunctionTool(
    name="boom",
    description="Always fails.",
    input_model=Empty,
    output_model=Flag,
    risk=Risk.WRITE,
    confirm_exempt=True,
    confirm_exempt_reason="it never works anyway",
    handler=_boom,
)
VERIFY = FunctionTool(
    name="verify",
    description="Mark the identity verified.",
    input_model=Empty,
    output_model=Flag,
    risk=Risk.WRITE,
    confirm_exempt=True,
    confirm_exempt_reason="the identity check itself",
    handler=_verify,
)
SNEAK = FunctionTool(
    name="sneak",
    description="A read tool that tries to change the context.",
    input_model=Amount,
    output_model=Receipt,
    risk=Risk.READ,
    handler=_sneak,
)
DISPATCH = FunctionTool(
    name="dispatch",
    description="A long-running write that completes by callback.",
    input_model=Amount,
    output_model=Flag,
    risk=Risk.WRITE,
    confirm_exempt=True,
    confirm_exempt_reason="dispatching is not the act",
    handler=_dispatch,
)
HUMAN_ONLY = FunctionTool(
    name="human_only",
    description="Needs a human as well as the customer.",
    input_model=Amount,
    output_model=Receipt,
    risk=Risk.HIGH,
    requires_human_approval=True,
    handler=_charge,
)

TEST_TOOLS: list[Tool] = [
    CHARGE,
    CHARGE_AGAIN,
    NUDGE,
    PEEK,
    PING,
    BOOM,
    VERIFY,
    SNEAK,
    DISPATCH,
    HUMAN_ONLY,
]


def registry() -> ToolRegistry:
    return ToolRegistry(TEST_TOOLS)


def runtime(engine: AsyncEngine, **kwargs: Any) -> ToolRuntime:
    return ToolRuntime(registry(), make_session_factory(engine), **kwargs)


async def conversation_and_run(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """A conversation and a run to hang tool calls and approvals off."""
    sessions = make_session_factory(engine)
    async with sessions() as session, session.begin():
        conversation = await repo.create_conversation(session, channel="web_chat")
        run = await repo.create_run(
            session,
            conversation_id=conversation.id,
            pack_version="1.0.0",
            pack_fingerprint="test",
        )
        return conversation.id, run.id


def site(
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    node_id: str = "do_it",
    frame_seq: int = 1,
    attempt: int = 0,
    verified: bool = True,
) -> CallSite:
    from support_core.engine.types import step_id
    from support_core.graph.context import CustomerContext

    return CallSite(
        conversation_id=conversation_id,
        run_id=run_id,
        frame_seq=frame_seq,
        node_id=node_id,
        step_id=step_id(run_id, frame_seq, node_id, attempt),
        customer=CustomerContext(ref="cus_1", identity_verified=verified),
    )


async def approve(
    engine: AsyncEngine,
    *,
    conversation_id: uuid.UUID,
    run_id: uuid.UUID,
    tool: str,
    args: dict[str, Any],
    node_id: str = "confirm_it",
    frame_seq: int = 1,
    step: str = "step-confirm",
    approved_by: str = "customer",
    args_hash: str | None = None,
) -> uuid.UUID:
    """Record an ``action_approval`` as a ``confirm`` node's checkpoint would."""
    from datetime import UTC, datetime

    from support_core.tools.approval import approval_hash

    sessions = make_session_factory(engine)
    async with sessions() as session, session.begin():
        return await repo.record_approval(
            session,
            repo.ApprovalWrite(
                conversation_id=conversation_id,
                run_id=run_id,
                frame_seq=frame_seq,
                node_id=node_id,
                step_id=step,
                tool=tool,
                args=args,
                args_hash=args_hash or approval_hash(tool, args),
                approved_by=approved_by,
                approved_at=datetime.now(UTC),
            ),
        )


def unvalidated_pack(path: Path, tools: Sequence[Tool] | None = None) -> Pack:
    """Load a pack's graphs *without* the validator.

    Every phase-1 static rule is skipped: this is a pack as an older core, a buggy validator, or
    an operator with a text editor might leave one. The runtime's job is to be correct anyway.
    """
    manifest = load_manifest(path)
    files = sorted(entry.relative_to(path).as_posix() for entry in (path / "graphs").glob("*.yaml"))
    graphs, findings = read_graphs(path, files)
    fatal = [f for f in findings if f.rule.endswith(("invalid", "invalid_yaml"))]
    assert not fatal, fatal
    built = ToolRegistry(list(tools) if tools is not None else TEST_TOOLS)
    return Pack(
        path=path,
        manifest=manifest,
        persona=_read_text(path / "persona.md"),
        policies=_read_text(path / "policies.md"),
        graphs=graphs,
        tools=manifest_from_registry(built),
        pin=build_pin(manifest, graphs, "0.0.1"),
        registry=built,
    )
