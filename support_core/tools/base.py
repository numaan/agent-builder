"""The tool contract. Implements DESIGN.md section 8.1, and the vocabulary section 8.2 polices.

DESIGN.md section 8.1 gives :class:`Tool` almost verbatim; the two additions here are recorded
in reviews/phase-4.md:

* ``confirm_exempt`` and ``confirm_exempt_reason``. Section 8.2 offers the exemption ("A pack
  may mark a WRITE tool ``confirm_exempt: true`` for side effects the customer cannot reasonably
  be asked about, such as sending a one-time passcode") but section 8.1's class does not carry
  it. The reason is required, because "the validator lists every exemption in its report so they
  are reviewed deliberately" is not satisfied by a line nobody has to justify (phase-1 deferred
  finding J).
* ``ToolContext.patch_customer``. DESIGN.md section 19 step 9 has ``verify_otp`` set
  ``ctx.customer.identity_verified``, and section 6.1 says ``ctx`` is read-only *to nodes*. Both
  hold: the node does not write it, the tool does, and the engine commits the patch in the same
  transaction as the step that called it.

Nothing in this module touches storage, the registry or the risk policy. It is the shape a pack
writes against, and it is deliberately small.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from support_core.graph.context import CustomerContext
from support_core.tools.risk import Risk, needs_confirm

TOOL_NAME = r"^[a-z][a-z0-9_]*$"
"""Tool names are identifiers: they appear in graph YAML, in prompts and in the approval hash."""


class ToolError(Exception):
    """A tool call did not produce a result. Routed by the ``tool`` node's ``on_error``."""


class ToolRefused(ToolError):
    """The runtime would not run the call at all.

    A missing approval, a tier a caller may not reach, arguments that do not fit the input
    model, a non-idempotent call whose previous outcome is unknown. Distinct from
    :class:`ToolFailed` because a refusal is the runtime saying no, not the world saying no; the
    engine records both the same way but the message matters to whoever reads the trace.
    """


class ToolFailed(ToolError):
    """The tool ran (or tried to) and raised, timed out, or returned the wrong shape."""


class ApprovalRecord(BaseModel):
    """The ``action_approval`` row that authorised a call (DESIGN.md sections 8.1, 8.2, 17)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    tool: str
    args_hash: str
    approved_by: str
    node_id: str | None = None
    frame_seq: int | None = None


@dataclass(slots=True)
class ToolContext:
    """What a tool is given besides its input (DESIGN.md section 8.1).

    "``ToolContext`` carries the idempotency key, the customer identity (verified or not), the
    trace span, and the approval record if any." The trace span itself is phase 7; what stands in
    for it here is the step id, which *is* the trace step's identity (DESIGN.md section 7.1).
    """

    idempotency_key: str
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    step_id: str
    node_id: str
    frame_seq: int
    risk: Risk
    customer: CustomerContext
    """The identity as the conversation currently knows it - verified or not. A tool that acts on
    "the customer" must decide for itself whether ``identity_verified`` is enough for what it is
    about to do; the graph's gates are the first line, not the only one."""

    channel: str = "web_chat"
    approval: ApprovalRecord | None = None
    """The approval that authorised this call, for a WRITE or HIGH tool that needed one."""

    context_patch: dict[str, Any] = field(default_factory=dict)
    """Changes to :class:`~support_core.graph.context.ConversationContext` this call asks for.

    Only a WRITE or HIGH tool may write here (a READ tool has no side effects by definition), and
    the engine applies it in the checkpoint transaction, so a crash cannot leave the tool's
    effect and the context out of step. Use :meth:`patch_customer`.
    """

    def patch_customer(self, **fields: Any) -> None:
        """Ask the engine to update ``ctx.customer`` (DESIGN.md section 19 step 9).

        The fields are validated against :class:`CustomerContext` by the engine before anything
        is written, so a typo is a tool failure rather than a silently ignored key.
        """
        customer = dict(self.context_patch.get("customer") or {})
        customer.update(fields)
        self.context_patch["customer"] = customer


class Tool(BaseModel):
    """One side effect the system can have (DESIGN.md section 8.1).

    Subclass and implement :meth:`run`, or use :class:`FunctionTool` to wrap a coroutine.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, frozen=True)

    name: str = Field(pattern=TOOL_NAME)
    description: str = Field(min_length=1)
    """Shown to the model. Untrusted by nothing and trusted by nobody: it is pack-written text
    that reaches layer 5 of the prompt, so it is neutralised like every other pack string."""

    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk: Risk
    idempotent: bool = True
    """``False`` means the runtime enforces at-most-once via the idempotency key: a call whose
    outcome is unknown after a crash is never repeated (DESIGN.md section 8.1)."""

    requires_human_approval: bool = False
    confirm_exempt: bool = False
    """DESIGN.md section 8.2, WRITE only. Requires :attr:`confirm_exempt_reason`."""

    confirm_exempt_reason: str | None = None

    patches_context: frozenset[str] = frozenset()
    """Fields of :class:`~support_core.graph.context.CustomerContext` this tool may change.

    Empty - the default - means the tool may change nothing about the customer, which is what
    almost every tool wants. "Any WRITE tool may declare the customer verified" was a far wider
    grant than "the tool that checks the passcode may set ``identity_verified``", and the
    difference is the whole identity gate (review finding R4): a WRITE tool may be
    ``confirm_exempt``, so nothing else stood between a careless tool and a verified customer.

    The names are checked against ``CustomerContext`` when the tool is built, so a typo is a
    load error rather than a patch that silently never applies, and :meth:`ToolContext.
    patch_customer` writes outside the declaration are refused at run time.
    """

    timeout_s: float = Field(default=15.0, gt=0)
    async_: bool = False
    """Long-running: the ``tool`` node suspends ``waiting_async_tool`` and the call completes
    through :meth:`~support_core.engine.executor.Executor.resume_async_tool`."""

    @model_validator(mode="after")
    def _exemption_is_justified(self) -> "Tool":
        if self.confirm_exempt:
            if self.risk is not Risk.WRITE:
                msg = (
                    f"tool {self.name!r} is {self.risk.value} risk and confirm_exempt; DESIGN.md "
                    f"section 8.2 offers the exemption for write-tier tools only"
                )
                raise ValueError(msg)
            if not (self.confirm_exempt_reason or "").strip():
                msg = (
                    f"tool {self.name!r} is confirm_exempt and must say why in "
                    f"confirm_exempt_reason; the validator reports every exemption so a human "
                    f"reviews it deliberately (DESIGN.md section 8.2)"
                )
                raise ValueError(msg)
        elif self.confirm_exempt_reason:
            msg = f"tool {self.name!r} gives a confirm_exempt_reason but is not confirm_exempt"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _context_writes_are_declarable(self) -> "Tool":
        if not self.patches_context:
            return self
        if self.risk is Risk.READ:
            msg = (
                f"tool {self.name!r} is read risk and declares patches_context; a read tool has "
                f"no side effects, and the conversation context is one (DESIGN.md section 8.1)"
            )
            raise ValueError(msg)
        unknown = sorted(self.patches_context - set(CustomerContext.model_fields))
        if unknown:
            known = ", ".join(sorted(CustomerContext.model_fields))
            msg = (
                f"tool {self.name!r} declares patches_context {unknown}, which "
                f"CustomerContext does not have; it has {known}"
            )
            raise ValueError(msg)
        return self

    @property
    def needs_confirm(self) -> bool:
        """Whether a ``confirm`` node must authorise this tool (DESIGN.md section 8.2).

        The policy lives here and in :mod:`support_core.tools.risk` and nowhere else, so the
        validator and the runtime cannot disagree about it.
        """
        return needs_confirm(self.risk, confirm_exempt=self.confirm_exempt)

    async def run(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
        """Do the thing. Raise :class:`ToolFailed` (or anything) to fail the call."""
        msg = f"tool {self.name!r} does not implement run()"
        raise NotImplementedError(msg)


Handler = Callable[[Any, ToolContext], Awaitable[Any]]
"""A plain coroutine ``(input_model_instance, ctx) -> output_model_instance``."""


class FunctionTool(Tool):
    """A :class:`Tool` whose behaviour is a coroutine rather than a subclass."""

    handler: Handler

    async def run(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
        result = await self.handler(input, ctx)
        if isinstance(result, BaseModel):
            return result
        if isinstance(result, Mapping):
            return self.output_model.model_validate(dict(result))
        msg = (
            f"tool {self.name!r} returned {type(result).__name__}; a handler must return its "
            f"output model or a mapping"
        )
        raise ToolFailed(msg)
