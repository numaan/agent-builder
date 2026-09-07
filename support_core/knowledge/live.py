"""``LiveLookupRetriever``. Implements DESIGN.md section 9.1's third backend: "READ tools exposed
to the retriever for dynamic facts".

Section 9.1 says the whole of it in one line - "routes entity-specific questions ('what does my
plan include') to READ tools" - and that line hides a decision. Deciding *which* question is
entity-specific, and what arguments the tool needs, is a classification and extraction problem,
and doing it properly means a structured model call with nothing in the graph constraining its
answer. This phase does not build that. What it builds is the part where the *pack* has made the
decision, in a line a reviewer can read:

.. code-block:: yaml

    live_lookups:
      - tool: get_plan_details
        triggers: [plan, included, allowance]
        args: { customer_ref: ctx.customer.ref }

The narrowing is recorded as a deviation in reviews/phase-5.md, along with what it cannot do: a
question whose answer needs an identifier only the question contains ("what is charge
ch_9912 for") does not reach a live lookup here.

**The tool runtime is not bypassed.** The call goes through the same
:class:`~support_core.llm.tool_loop.ReadOnlyToolGateway` an ``llm`` node's model loop uses, built
by the executor from this retriever's declared tool list. Everything that gateway refuses, this
refuses: a tool the pack did not declare here, a tool that is not READ tier, a tool whose
declared tier and the runtime's disagree, and a call past the turn's budget. There is no path in
this module to a runner or to the runtime; PLAN.md's standing rule ("no WRITE or HIGH tool
without an ``ActionApproval``") holds because a WRITE tool cannot be reached at all.
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

from support_core.graph.context import ConversationContext
from support_core.knowledge.sources import LiveLookup
from support_core.knowledge.types import Passage, RetrievalRequest
from support_core.llm.tool_loop import ReadOnlyToolGateway
from support_core.llm.types import ToolCall

logger = logging.getLogger(__name__)

LIVE_VERSION = "live"
"""``source_version`` for a live lookup.

A tool result has no version in the sense DESIGN.md section 9.2 means: it is true at the instant
it was read and there is nothing to re-read later. Saying ``live`` rather than inventing a
timestamp-shaped version is the honest answer, and it makes a trace that rests on a live lookup
visibly different from one that rests on a document - which is what somebody investigating a
wrong answer needs to know first."""

MAX_LOOKUP_CHARS = 2000
"""A tool result rendered into a passage. Layer 7 budgets the block as a whole; this stops one
verbose tool from being the only passage that fits."""


class LiveLookupRetriever:
    """READ tools as a retrieval backend (DESIGN.md section 9.1)."""

    name = "live"

    def __init__(self, lookups: Sequence[LiveLookup], gateway: ReadOnlyToolGateway | None) -> None:
        self.lookups = tuple(lookups)
        self.gateway = gateway

    def matching(self, query: str) -> list[LiveLookup]:
        """Which lookups this query fires. A lookup with no triggers fires for every query."""
        lowered = query.lower()
        return [
            lookup
            for lookup in self.lookups
            if not lookup.triggers or any(trigger.lower() in lowered for trigger in lookup.triggers)
        ]

    async def retrieve(self, request: RetrievalRequest) -> Sequence[Passage]:
        if self.gateway is None or not self.lookups:
            return []
        fired = self.matching(request.query)[: request.k]
        if not fired:
            return []
        # The gateway resolves the declared tools' specs - their risk tiers above all - on this
        # call and refuses anything it has not resolved. Skipping it would make every live lookup
        # a refusal, which is how this was found: a retriever that returns nothing and a retriever
        # that is being refused look identical from the outside.
        await self.gateway.specs()
        passages: list[Passage] = []
        for lookup in fired:
            try:
                arguments = _arguments(lookup, request.ctx)
            except KeyError as exc:
                # A lookup whose argument expression names something this conversation does not
                # have - an unverified customer with no `ref` - is skipped, not failed. It is a
                # pack saying "call this when you can", and a retrieval that raised would turn a
                # missing optional fact into a handoff.
                logger.info("live lookup %s skipped: %s", lookup.tool, exc)
                continue
            outcome = await self.gateway.call(
                ToolCall(id=f"live:{lookup.tool}", name=lookup.tool, arguments=arguments)
            )
            if outcome.is_error:
                # Refusals are logged and dropped rather than raised. A refusal here is the
                # gateway working: the pack declared a tool it may not call this way, and the
                # right consequence is a retrieval with one fewer passage rather than a parked
                # conversation.
                logger.warning("live lookup %s refused: %s", lookup.tool, outcome.content)
                continue
            passages.append(
                Passage(
                    text=outcome.content[:MAX_LOOKUP_CHARS],
                    source_id=f"tool:{lookup.tool}",
                    source_version=LIVE_VERSION,
                    locator=f"{lookup.tool}({_render_args(arguments)})",
                    score=1.0,
                    backend=self.name,
                )
            )
        return passages

    def declared(self) -> tuple[str, ...]:
        """The tools this retriever may call, for the gateway the executor builds around it."""
        return declared_tools(self.lookups)


def declared_tools(lookups: Sequence[LiveLookup]) -> tuple[str, ...]:
    """The tool names a set of lookups may call, deduplicated, in declaration order.

    A free function as well as a method because the executor needs the answer *before* it has a
    retriever: the gateway is built from this list and the retriever is built around the gateway.
    """
    return tuple(dict.fromkeys(lookup.tool for lookup in lookups))


def _arguments(lookup: LiveLookup, ctx: ConversationContext) -> dict[str, Any]:
    """Resolve ``{name: 'ctx.customer.ref'}`` against the conversation context.

    Only ``ctx``, and only attribute access. This is not the expression language of
    :mod:`support_core.graph.expr`, and it deliberately is not: that language is for pack-authored
    graph expressions type-checked at load against a state model, and a retriever has no frame and
    no state model to check against. What it needs is a path into one known object, which is small
    enough to be safe by being unable to express anything else - no calls, no indexing, no
    literals, and a leading underscore is unaddressable, as everywhere else in this repository.
    """
    resolved: dict[str, Any] = {}
    for name, expression in lookup.args.items():
        parts = expression.split(".")
        if parts[0] != "ctx":
            msg = f"{lookup.tool}: argument {name!r} must start with 'ctx.', got {expression!r}"
            raise KeyError(msg)
        value: Any = ctx
        for part in parts[1:]:
            if not part or part.startswith("_"):
                msg = f"{lookup.tool}: {expression!r} is not an addressable path"
                raise KeyError(msg)
            value = getattr(value, part, None)
            if value is None:
                msg = f"{lookup.tool}: {expression!r} is not set on this conversation"
                raise KeyError(msg)
        resolved[name] = value
    return resolved


def _render_args(arguments: dict[str, Any]) -> str:
    """A locator fragment. Sorted so the same call renders the same way in every trace."""
    try:
        return json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers the known cases
        return "?"
