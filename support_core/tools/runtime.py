"""The tool runtime. Implements DESIGN.md sections 8.1 to 8.4: the risk policy, the approval
binding, idempotency, and the ``tool_call`` row.

This module is the only place in the system that can execute a tool, and it is written on the
assumption that every caller might be hostile or broken:

* **The risk policy is checked here**, not in the caller. The read-only gateway of section 8.4
  refuses a WRITE tool before it ever reaches this module; this module refuses it again, because
  "the gateway is correct" is not a property anyone should have to keep true for money to be
  safe (DESIGN.md section 8.2: "enforced by the runtime, not by prompts" - and not by the layer
  above either).
* **The approval is checked here.** Phase 1's validator proves statically that a ``confirm``
  exists on every path and lives in the tool node's own graph. Nothing in this module assumes
  that pass ran: a WRITE or HIGH tool without a live, matching, unconsumed
  :class:`~support_core.storage.models.ActionApproval` is refused, whatever the graph says.
* **The idempotency key is claimed before the tool runs.** DESIGN.md section 7.3 says a step
  re-executes after a crash and "tool idempotency keys make re-execution safe"; that is only
  true if a row exists to make it safe *with*. A row left ``running`` is a call whose outcome
  nobody knows, and for a non-idempotent tool the answer is a refusal, not a retry - which is
  the difference between one refund and two.

The key is the step id of DESIGN.md section 7.1 (``run_id:frame_seq:node_id:attempt``), which
phase 2 proved stable across crashes, loops and repeated sub-graph invocation. A model-loop call
adds a suffix, because one ``llm`` node's step can make several READ calls.
"""

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession as Session
from sqlalchemy.ext.asyncio import async_sessionmaker

from support_core.graph.context import CustomerContext
from support_core.llm.tool_loop import ModelToolSpec, ToolOutcome
from support_core.storage import repositories as repo
from support_core.tools.approval import hash_for
from support_core.tools.base import (
    ApprovalRecord,
    Tool,
    ToolContext,
    ToolError,
    ToolFailed,
    ToolRefused,
)
from support_core.tools.registry import ToolRegistry
from support_core.tools.risk import MODEL_CALLABLE, SIDE_EFFECTING, Risk

Caller = Literal["tool_node", "model_loop"]
"""Which row of DESIGN.md section 8.2's table applies. There is no third caller: a node that is
neither is given no capability to invoke a tool at all (see the executor's ``_tool_access``)."""

CUSTOMER_KEY = "customer"
"""The only part of :class:`~support_core.graph.context.ConversationContext` a tool may patch."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CallSite:
    """Where in a run a call is being made from. Every field comes from durable state."""

    conversation_id: uuid.UUID
    run_id: uuid.UUID
    frame_seq: int
    node_id: str
    step_id: str
    customer: CustomerContext
    channel: str = "web_chat"


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """What one call produced, and how it was reached."""

    tool: str
    risk: Risk
    output: BaseModel
    output_json: dict[str, Any]
    idempotency_key: str
    tool_call_id: uuid.UUID
    approval_id: uuid.UUID | None = None
    customer_patch: dict[str, Any] | None = None
    """The full new ``ctx.customer``, when the tool asked to change it."""

    replayed: bool = False
    """True when the tool did not run: a completed row under this key already existed."""

    pending: bool = False
    """An async tool that has been dispatched (DESIGN.md section 7.2, waiting_async_tool)."""


@dataclass(slots=True)
class _Claim:
    """The outcome of taking (or failing to take) an idempotency key."""

    tool_call_id: uuid.UUID | None = None
    approval: ApprovalRecord | None = None
    replay: ToolCallResult | None = None


class ToolRuntime:
    """Executes tools for one pack against one database."""

    def __init__(
        self,
        registry: ToolRegistry,
        sessions: async_sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.registry = registry
        self.sessions = sessions
        self.clock = clock

    # -- the one entry point ---------------------------------------------------------------

    async def invoke(
        self,
        *,
        tool_name: str,
        args: Mapping[str, Any],
        site: CallSite,
        caller: Caller,
        allowed: Sequence[str] | None = None,
        requires_approval: str | None = None,
        key_suffix: str = "",
    ) -> ToolCallResult:
        """Run one tool, or refuse to.

        ``allowed`` is the caller's own narrow list - a ``tool`` node's single declared tool, or
        an ``llm`` node's ``tools:``. ``requires_approval`` is the ``confirm`` node id the graph
        bound this call to; it is checked against the stored approval, so naming the wrong
        confirm is a refusal rather than a way to borrow another action's authorisation.
        """
        tool = self.registry.get(tool_name)
        if tool is None:
            known = ", ".join(self.registry.names) or "none"
            msg = f"no tool named {tool_name!r} is registered by this pack; it exports {known}"
            raise ToolRefused(msg)
        if allowed is not None and tool_name not in allowed:
            msg = (
                f"{site.node_id}: {tool_name!r} is not a tool this step may call "
                f"({', '.join(allowed) or 'none'})"
            )
            raise ToolRefused(msg)
        if caller == "model_loop" and tool.risk not in MODEL_CALLABLE:
            # The read-only gateway of DESIGN.md 8.4 refused this already. Refusing it a second
            # time is deliberate: the gateway is one object away from the model, and this is the
            # object that would otherwise do the thing.
            msg = (
                f"{tool_name!r} is {tool.risk.value} risk and cannot be called from a model tool "
                f"loop; DESIGN.md section 8.2 allows read-tier tools there and nothing else"
            )
            raise ToolRefused(msg)

        args_hash, canonical = hash_for(tool, args)
        key = site.step_id + key_suffix

        claim = await self._claim(
            tool=tool,
            canonical=canonical,
            args_hash=args_hash,
            site=site,
            key=key,
            requires_approval=requires_approval,
        )
        if claim.replay is not None:
            return claim.replay
        assert claim.tool_call_id is not None

        return await self._execute(
            tool=tool,
            canonical=canonical,
            site=site,
            key=key,
            tool_call_id=claim.tool_call_id,
            approval=claim.approval,
        )

    async def complete_async(
        self, *, tool_name: str, site: CallSite, payload: Mapping[str, Any], key_suffix: str = ""
    ) -> ToolCallResult:
        """Finish an async tool from its callback (DESIGN.md section 7.2)."""
        tool = self.registry.require(tool_name)
        key = site.step_id + key_suffix
        now = self.clock()
        async with self.sessions() as session, session.begin():
            row = await repo.get_tool_call(session, key)
            if row is None:
                msg = f"no dispatched call of {tool_name!r} under {key!r} to complete"
                raise ToolRefused(msg)
            if row.status == "succeeded":
                return self._replayed(tool, row)
            if row.status != "awaiting_callback":
                msg = (
                    f"the call of {tool_name!r} under {key!r} is {row.status!r}, not awaiting "
                    f"a callback"
                )
                raise ToolRefused(msg)
            try:
                output = tool.output_model.model_validate(dict(payload))
            except ValidationError as exc:
                await repo.finish_tool_call(
                    session, row.id, status="failed", when=now, error=str(exc)
                )
                msg = f"the callback payload for {tool_name!r} does not fit its output model: {exc}"
                raise ToolFailed(msg) from exc
            output_json = dict(output.model_dump(mode="json"))
            await repo.finish_tool_call(
                session, row.id, status="succeeded", when=now, result=output_json
            )
            return ToolCallResult(
                tool=tool.name,
                risk=tool.risk,
                output=output,
                output_json=output_json,
                idempotency_key=key,
                tool_call_id=row.id,
                approval_id=row.approval_id,
            )

    # -- the claim -------------------------------------------------------------------------

    async def _claim(
        self,
        *,
        tool: Tool,
        canonical: dict[str, Any],
        args_hash: str,
        site: CallSite,
        key: str,
        requires_approval: str | None,
    ) -> _Claim:
        """Take the idempotency key and the approval together, in one transaction.

        Together, because either alone is a state this system should not be able to reach: an
        approval consumed with no call to show for it silently voids the customer's decision,
        and a claimed key with an unconsumed approval leaves the approval spendable by the next
        thing that asks.
        """
        now = self.clock()
        async with self.sessions() as session, session.begin():
            existing = await repo.get_tool_call(session, key)
            if existing is not None:
                return await self._reenter(session, tool, existing, now)

            call = await repo.start_tool_call(
                session,
                repo.ToolCallStart(
                    idempotency_key=key,
                    run_id=site.run_id,
                    step_id=site.step_id,
                    node_id=site.node_id,
                    tool=tool.name,
                    risk=tool.risk.value,
                    args=canonical,
                    created_at=now,
                    status="awaiting_callback" if tool.async_ else "running",
                ),
            )
            record = await self._authorise(
                session,
                tool=tool,
                site=site,
                args_hash=args_hash,
                requires_approval=requires_approval,
                tool_call_id=call.id,
                now=now,
            )
            if record is not None:
                await repo.set_tool_call_approval(session, call.id, record.id)
            return _Claim(tool_call_id=call.id, approval=record)

    async def _authorise(
        self,
        session: Session,
        *,
        tool: Tool,
        site: CallSite,
        args_hash: str,
        requires_approval: str | None,
        tool_call_id: uuid.UUID,
        now: datetime,
    ) -> ApprovalRecord | None:
        """DESIGN.md section 8.2's table, as code. Raises rather than returning a verdict.

        Raising is the point: there is no path through this method that both fails to find an
        approval and lets the caller carry on, and the exception rolls back the claim, so a
        refused call has not spent its idempotency key either.
        """
        if not tool.needs_confirm:
            if tool.confirm_exempt:
                # DESIGN.md 8.2's exemption. The validator reports every one of these with the
                # reason the pack gave; nothing is silent about it at run time either.
                return None
            return None
        if not requires_approval:
            msg = (
                f"{site.node_id}: {tool.name!r} is {tool.risk.value} risk and this call names no "
                f"confirm node (requires_approval); DESIGN.md section 8.2 requires an "
                f"ActionApproval bound to the arguments"
            )
            raise ToolRefused(msg)
        approval = await repo.consume_approval(
            session,
            conversation_id=site.conversation_id,
            run_id=site.run_id,
            frame_seq=site.frame_seq,
            node_id=requires_approval,
            tool=tool.name,
            args_hash=args_hash,
            approved_by=("customer", "human"),
            now=now,
            tool_call_id=tool_call_id,
        )
        if approval is None:
            msg = (
                f"{site.node_id}: no live approval for {tool.name!r} with these arguments. "
                f"An approval must have been recorded by confirm node "
                f"{requires_approval!r} in this frame (#{site.frame_seq}), must match "
                f"sha256(tool + canonical_json(args)) = {args_hash[:16]}..., and must not have "
                f"been used already"
            )
            raise ToolRefused(msg)
        if tool.requires_human_approval:
            human = await repo.consume_approval(
                session,
                conversation_id=site.conversation_id,
                run_id=site.run_id,
                frame_seq=site.frame_seq,
                node_id=requires_approval,
                tool=tool.name,
                args_hash=args_hash,
                approved_by=("human",),
                now=now,
                tool_call_id=tool_call_id,
            )
            if human is None:
                msg = (
                    f"{site.node_id}: {tool.name!r} declares requires_human_approval and no "
                    f"human has approved these arguments; the desk that records one is phase 6"
                )
                raise ToolRefused(msg)
        return ApprovalRecord(
            id=approval.id,
            tool=approval.tool,
            args_hash=approval.args_hash,
            approved_by=approval.approved_by,
            node_id=approval.node_id,
            frame_seq=approval.frame_seq,
        )

    async def _reenter(self, session: Session, tool: Tool, existing: Any, now: datetime) -> _Claim:
        """Another pass over a key that already exists (DESIGN.md sections 7.3, 8.1)."""
        status = existing.status
        if status == "succeeded":
            await repo.reenter_tool_call(session, existing.id, status=status, when=now)
            return _Claim(replay=self._replayed(tool, existing))
        if status == "awaiting_callback":
            await repo.reenter_tool_call(session, existing.id, status=status, when=now)
            return _Claim(replay=self._replayed(tool, existing, pending=True, allow_empty=True))
        if status == "failed":
            await repo.reenter_tool_call(session, existing.id, status=status, when=now)
            msg = (
                f"{tool.name!r} already failed under this step and its outcome is recorded: "
                f"{existing.error}"
            )
            raise ToolFailed(msg)
        # ``running`` or ``indeterminate``: a process died between the claim and the result, so
        # nobody knows whether the side effect happened.
        if tool.idempotent:
            await repo.reenter_tool_call(session, existing.id, status="running", when=now)
            approval = (
                ApprovalRecord(
                    id=existing.approval_id,
                    tool=tool.name,
                    args_hash="",
                    approved_by="customer",
                )
                if existing.approval_id is not None
                else None
            )
            # The approval was consumed by the first attempt and belongs to this key; consuming
            # a second one would mean two customer decisions for one call.
            return _Claim(tool_call_id=existing.id, approval=approval)
        await repo.reenter_tool_call(session, existing.id, status="indeterminate", when=now)
        msg = (
            f"{tool.name!r} is not idempotent and a previous attempt under this step "
            f"({existing.idempotency_key}) did not record an outcome, so it may already have "
            f"happened; at-most-once means this call is refused rather than repeated "
            f"(DESIGN.md section 8.1)"
        )
        raise ToolRefused(msg)

    def _replayed(
        self, tool: Tool, row: Any, *, pending: bool = False, allow_empty: bool = False
    ) -> ToolCallResult:
        stored = dict(row.result or {})
        if not stored and not allow_empty:  # pragma: no cover - a succeeded row always has one
            msg = f"the recorded result of {tool.name!r} under {row.idempotency_key} is missing"
            raise ToolFailed(msg)
        try:
            output = tool.output_model.model_validate(stored)
        except ValidationError as exc:
            if not pending:
                msg = (
                    f"the recorded result of {tool.name!r} no longer fits its output model "
                    f"(the pack changed under a suspended run): {exc}"
                )
                raise ToolFailed(msg) from exc
            output = tool.output_model.model_construct()
        patch = dict(row.context_patch or {}).get(CUSTOMER_KEY)
        return ToolCallResult(
            tool=tool.name,
            risk=tool.risk,
            output=output,
            output_json=dict(output.model_dump(mode="json")) if not pending else stored,
            idempotency_key=row.idempotency_key,
            tool_call_id=row.id,
            approval_id=row.approval_id,
            customer_patch=dict(patch) if isinstance(patch, dict) else None,
            replayed=True,
            pending=pending,
        )

    # -- execution -------------------------------------------------------------------------

    async def _execute(
        self,
        *,
        tool: Tool,
        canonical: dict[str, Any],
        site: CallSite,
        key: str,
        tool_call_id: uuid.UUID,
        approval: ApprovalRecord | None,
    ) -> ToolCallResult:
        context = ToolContext(
            idempotency_key=key,
            conversation_id=site.conversation_id,
            run_id=site.run_id,
            step_id=site.step_id,
            node_id=site.node_id,
            frame_seq=site.frame_seq,
            risk=tool.risk,
            customer=site.customer.model_copy(deep=True),
            channel=site.channel,
            approval=approval,
        )
        payload = tool.input_model.model_validate(canonical)
        try:
            output = await asyncio.wait_for(tool.run(payload, context), timeout=tool.timeout_s)
            output_json, customer = self._settle(tool, output, context)
        except TimeoutError as exc:
            await self._fail(tool_call_id, f"timed out after {tool.timeout_s}s")
            msg = f"{tool.name!r} timed out after {tool.timeout_s}s"
            raise ToolFailed(msg) from exc
        except ToolError as exc:
            await self._fail(tool_call_id, str(exc))
            raise
        except Exception as exc:
            await self._fail(tool_call_id, f"{type(exc).__name__}: {exc}")
            msg = f"{tool.name!r} raised {type(exc).__name__}: {exc}"
            raise ToolFailed(msg) from exc

        patch = {CUSTOMER_KEY: customer} if customer is not None else None
        async with self.sessions() as session, session.begin():
            await repo.finish_tool_call(
                session,
                tool_call_id,
                status="awaiting_callback" if tool.async_ else "succeeded",
                when=self.clock(),
                result=output_json,
                context_patch=patch,
            )
        return ToolCallResult(
            tool=tool.name,
            risk=tool.risk,
            output=output,
            output_json=output_json,
            idempotency_key=key,
            tool_call_id=tool_call_id,
            approval_id=approval.id if approval else None,
            customer_patch=customer,
            pending=tool.async_,
        )

    def _settle(
        self, tool: Tool, output: BaseModel, context: ToolContext
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Validate what came back, and what the tool asked to change about ``ctx``."""
        if not isinstance(output, tool.output_model):
            try:
                output = tool.output_model.model_validate(output)
            except ValidationError as exc:
                msg = f"{tool.name!r} returned something that is not its output model: {exc}"
                raise ToolFailed(msg) from exc
        output_json = dict(output.model_dump(mode="json"))
        patch = context.context_patch
        if not patch:
            return output_json, None
        if tool.risk not in SIDE_EFFECTING:
            msg = (
                f"{tool.name!r} is read-tier and asked to change the conversation context; a "
                f"read tool has no side effects (DESIGN.md section 8.1)"
            )
            raise ToolFailed(msg)
        unknown = sorted(set(patch) - {CUSTOMER_KEY})
        if unknown:
            msg = (
                f"{tool.name!r} tried to patch context key(s) {unknown}; only 'customer' may "
                f"be patched"
            )
            raise ToolFailed(msg)
        merged = dict(context.customer.model_dump(mode="json"))
        merged.update(dict(patch[CUSTOMER_KEY]))
        try:
            CustomerContext.model_validate(merged)
        except ValidationError as exc:
            msg = f"{tool.name!r} patched ctx.customer with something it cannot hold: {exc}"
            raise ToolFailed(msg) from exc
        return output_json, merged

    async def _fail(self, tool_call_id: uuid.UUID, error: str) -> None:
        async with self.sessions() as session, session.begin():
            await repo.finish_tool_call(
                session, tool_call_id, status="failed", when=self.clock(), error=error
            )


class RegistryToolRunner:
    """The model-facing runner of DESIGN.md section 8.4, over the registry.

    Built per node by the executor and handed to the phase-3
    :class:`~support_core.llm.tool_loop.ReadOnlyToolGateway`, which is the only thing that ever
    holds it. It reports each tool's *true* tier - including a WRITE tool a pack wrongly listed
    on an ``llm`` node - because reporting the truth is what lets the gateway refuse it, and the
    runtime refuses it again underneath.
    """

    def __init__(self, runtime: ToolRuntime, site: CallSite) -> None:
        self.runtime = runtime
        self.site = site
        self.sequence = 0

    async def describe(self, names: Sequence[str]) -> Sequence[ModelToolSpec]:
        specs: list[ModelToolSpec] = []
        for name in names:
            tool = self.runtime.registry.get(name)
            if tool is None:
                continue
            specs.append(
                ModelToolSpec(
                    name=tool.name,
                    description=tool.description,
                    input_schema=dict(tool.input_model.model_json_schema()),
                    risk=tool.risk,
                )
            )
        return specs

    async def invoke(
        self, name: str, arguments: Mapping[str, Any], *, call_id: str, step_id: str
    ) -> ToolOutcome:
        """One model-requested call. A refusal is content, not an exception.

        DESIGN.md section 8.4 has the runtime feed the result back to the model; a model asking
        for something it may not have is a thing that happens, and it needs to be told no in a
        form it can react to. The refusal still reaches the trace through the gateway's own
        ``refusals`` list.
        """
        self.sequence += 1
        suffix = f"#tool{self.sequence}"
        try:
            result = await self.runtime.invoke(
                tool_name=name,
                args=arguments,
                site=self.site,
                caller="model_loop",
                key_suffix=suffix,
            )
        except ToolRefused as exc:
            return ToolOutcome(content=f"refused: {exc}", is_error=True)
        except ToolFailed as exc:
            return ToolOutcome(content=f"error: {exc}", is_error=True)
        return ToolOutcome(content=json.dumps(result.output_json, ensure_ascii=False, default=str))
