"""The human agent desk. Implements DESIGN.md section 13's desk API over section 12's surface.

    The human desk API lets the human reply directly, take over fully (`close`), or hand back
    (`resume` with optional state patch, for example marking an override as approved). On
    `resume`, the graph continues from the handoff node's `resumed` edge. - DESIGN.md section 13

    **Human desk**: not a customer channel but uses the same API surface to inject human replies
    and to resume or close. - DESIGN.md section 12

Six endpoints, and the interesting thing about them is how little they do. A desk action is a
call into the engine (:meth:`~support_core.engine.executor.Executor.resume_human`) or a row in
the ``handoff`` table, and this module is the HTTP shape of that and nothing else: no business
logic, no packet building, no decision about what a reason means. That is deliberate, because
the desk is the one surface where a *person* can move the conversation, and every rule that
matters - a gate, an approval hash, a risk tier - has to keep holding when they do.

Which is why ``approve`` is the endpoint to read carefully. Phase 4 built
``requires_human_approval`` and left it unsatisfiable: the runtime consumes the customer's
approval and then looks for a second row with ``approved_by = 'human'``, and nothing in the
system could write one, so a tool that declared the flag always refused. This is what writes it -
and it writes a *copy of the customer's own row*, binding and arguments and all, so a desk can
say "yes, that action" and cannot say "yes, this other action". The human is a second signature
on one proposal, never a way to propose something.

**There is no authentication here.** Phase W's review recorded that the web chat endpoints have
none and deferred it to phase 7, which owns the channel surface; this adds a surface where the
same gap exposes *other people's* conversations rather than your own, so it is worse, and it is
the first thing phase 7 must fix. It is written at the top of this module rather than in a
review because whoever mounts this router needs to read it.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from support_core.api.runtime import AppRuntime
from support_core.engine.errors import StatePatchError
from support_core.storage import repositories as repo
from support_core.storage.models import Handoff

DESK_AUTHOR = "human"
"""Who a desk reply is attributed to in the transcript.

Not ``agent``. A customer's transcript should be able to say which sentences a person wrote, and
an audit that cannot tell the model's words from a human's is not much of an audit. Channel
adapters render it like any other outbound message.
"""


class ReplyBody(BaseModel):
    """A human answering the customer directly, without giving the workflow back."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=8000)
    human_id: str | None = None


class ResumeBody(BaseModel):
    """Handing the conversation back to the graph (DESIGN.md section 13's ``resume``)."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    """Something to say to the customer as control returns. Optional."""

    patch: dict[str, Any] = Field(default_factory=dict)
    """A state patch for the frame the run is suspended in - the design's own example is
    "marking an override as approved".

    Checked against that frame's own declared state model **before** anything is written
    (:meth:`~support_core.engine.executor.Executor._check_patch`), so a field the graph does not
    declare is a 400 naming it and the run does not move. It used to be written first and
    validated on the next node entry, which meant one mistyped field parked the conversation for
    ever under a ``pack_incompatible`` handoff - blaming the pack for a typo at the desk (review
    finding P1). It may never set ``identity_verified``, and it is refused while an approval is
    live on that frame."""

    human_id: str | None = None


class CloseBody(BaseModel):
    """Taking the conversation over fully (DESIGN.md section 13's ``close``)."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    human_id: str | None = None


class ApproveBody(BaseModel):
    """A human's signature on the action the customer already approved."""

    model_config = ConfigDict(extra="forbid")

    human_id: str | None = None


def _summary(row: Handoff) -> dict[str, Any]:
    """One queue row, without its packet: what a desk's list view shows."""
    return {
        "id": str(row.id),
        "conversation_id": str(row.conversation_id),
        "run_id": str(row.run_id) if row.run_id else None,
        "queue": row.queue,
        "reason": row.reason,
        "status": row.status,
        "workflow": row.graph_id,
        "node": row.node_id,
        "human_id": row.human_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sla_due_at": row.sla_due_at.isoformat() if row.sla_due_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


def desk_router(runtime: AppRuntime) -> APIRouter:
    """The desk endpoints, mounted by :func:`~support_core.api.app.create_app`."""
    router = APIRouter(prefix="/desk", tags=["desk"])

    async def _load(handoff_id: uuid.UUID) -> Handoff | None:
        async with runtime.executor.sessions() as session, session.begin():
            row = await repo.get_handoff(session, handoff_id)
            if row is not None:
                session.expunge(row)
            return row

    async def _resolve(handoff_id: uuid.UUID, status: str, human_id: str | None) -> None:
        async with runtime.executor.sessions() as session, session.begin():
            await repo.resolve_handoff(
                session,
                handoff_id,
                status=status,
                when=runtime.executor.hooks.clock(),
                human_id=human_id,
            )

    @router.get("/handoffs")
    async def list_handoffs(
        queue: str | None = None, status: str | None = "open", limit: int = 50
    ) -> JSONResponse:
        """The queue, oldest first, so an SLA means something."""
        async with runtime.executor.sessions() as session, session.begin():
            rows = await repo.list_handoffs(
                session, queue=queue, status=status, limit=max(1, min(limit, 200))
            )
            return JSONResponse({"handoffs": [_summary(row) for row in rows]})

    @router.get("/handoffs/{handoff_id}")
    async def read_handoff(handoff_id: uuid.UUID) -> JSONResponse:
        """One handoff, with the whole packet (DESIGN.md section 13)."""
        row = await _load(handoff_id)
        if row is None:
            return JSONResponse({"error": "no such handoff"}, status_code=404)
        return JSONResponse({**_summary(row), "packet": row.packet})

    @router.post("/handoffs/{handoff_id}/reply")
    async def reply(handoff_id: uuid.UUID, request: Request) -> JSONResponse:
        """Say something to the customer, leaving the conversation parked.

        Deliberately not a resume. Answering a question and giving the workflow back are two
        different acts - a human often wants to ask the customer something before deciding - and
        an endpoint that did both would resume a graph every time somebody typed.

        The message goes through the same path a node's message does: written durably, then
        handed to the channel. So it reaches a live web chat socket, and a client that connects
        later reads it in the transcript.
        """
        body = await _body(request, ReplyBody)
        if isinstance(body, JSONResponse):
            return body
        row = await _load(handoff_id)
        if row is None:
            return JSONResponse({"error": "no such handoff"}, status_code=404)
        await runtime.say(row.conversation_id, body.text, author=DESK_AUTHOR)
        return JSONResponse({"ok": True, "conversation_id": str(row.conversation_id)})

    @router.post("/handoffs/{handoff_id}/resume")
    async def resume(handoff_id: uuid.UUID, request: Request) -> JSONResponse:
        """Hand the workflow back (DESIGN.md section 13: the ``resumed`` edge)."""
        body = await _body(request, ResumeBody)
        if isinstance(body, JSONResponse):
            return body
        row = await _load(handoff_id)
        if row is None:
            return JSONResponse({"error": "no such handoff"}, status_code=404)
        try:
            # Checked before the customer is told anything, so a refused patch is an error the
            # operator can act on rather than a message sent for a resume that did not happen.
            # `resume_human` checks it again under the conversation lock, which is the
            # authoritative one; this is the one that keeps the ordering honest (finding P1).
            await runtime.executor.check_state_patch(row.conversation_id, body.patch)
            if body.text:
                await runtime.say(row.conversation_id, body.text, author=DESK_AUTHOR)
            outcome = await runtime.executor.resume_human(
                row.conversation_id, patch=body.patch or None
            )
        except StatePatchError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        await _resolve(handoff_id, "resumed", body.human_id)
        await runtime.announce(row.conversation_id)
        return JSONResponse(
            {
                "ok": True,
                "status": outcome.status,
                "steps": outcome.steps,
                "handoff_reason": outcome.handoff_reason,
            }
        )

    @router.post("/handoffs/{handoff_id}/close")
    async def close(handoff_id: uuid.UUID, request: Request) -> JSONResponse:
        """Take the conversation over fully (the ``closed`` edge, or the run's end)."""
        body = await _body(request, CloseBody)
        if isinstance(body, JSONResponse):
            return body
        row = await _load(handoff_id)
        if row is None:
            return JSONResponse({"error": "no such handoff"}, status_code=404)
        if body.text:
            await runtime.say(row.conversation_id, body.text, author=DESK_AUTHOR)
        outcome = await runtime.executor.resume_human(row.conversation_id, close=True)
        await _resolve(handoff_id, "closed", body.human_id)
        await runtime.announce(row.conversation_id)
        return JSONResponse({"ok": True, "status": outcome.status})

    @router.post("/handoffs/{handoff_id}/approve")
    async def approve(handoff_id: uuid.UUID, request: Request) -> JSONResponse:
        """Sign the pending action, so a ``requires_human_approval`` tool can run (8.2).

        It approves *the action the customer already approved* and nothing else: the row written
        here is a copy of theirs, with the same tool, arguments, hash, run, frame and confirm
        node, and ``approved_by = 'human'``. There is no way to name a different action, because
        the desk supplies no arguments - which is the whole point of a second signature.

        Idempotent in the way that matters: a second call writes a second human row, and the
        runtime consumes exactly one per call, so a double click does not authorise a second
        refund. The customer's approval is still single-use underneath it.
        """
        body = await _body(request, ApproveBody)
        if isinstance(body, JSONResponse):
            return body
        row = await _load(handoff_id)
        if row is None:
            return JSONResponse({"error": "no such handoff"}, status_code=404)
        async with runtime.executor.sessions() as session, session.begin():
            live = await repo.live_approvals(session, row.conversation_id)
            pending = [a for a in live if a.approved_by == "customer"]
            if not pending:
                return JSONResponse(
                    {
                        "error": (
                            "this conversation has no action the customer has approved and the "
                            "system has not yet run, so there is nothing for a human to sign"
                        )
                    },
                    status_code=409,
                )
            template = pending[-1]
            approval_id = await repo.record_human_approval(
                session, template=template, now=runtime.executor.hooks.clock()
            )
            return JSONResponse(
                {
                    "ok": True,
                    "approval_id": str(approval_id),
                    "tool": template.tool,
                    "args": template.args,
                    "args_hash": template.args_hash,
                }
            )

    @router.get("/conversations/{conversation_id}/transcript")
    async def transcript(conversation_id: uuid.UUID, limit: int = 200) -> JSONResponse:
        """What a packet's ``transcript_url`` points at (DESIGN.md section 13).

        A real link rather than a plausible one. A packet that carried an address nothing served
        would be a fabricated citation of the conversation itself.
        """
        async with runtime.executor.sessions() as session, session.begin():
            conversation = await repo.get_conversation(session, conversation_id)
            if conversation is None:
                return JSONResponse({"error": "no such conversation"}, status_code=404)
            rows = await repo.transcript(session, conversation_id, max(1, min(limit, 500)))
            handoffs = await repo.list_handoffs(
                session, status=None, conversation_id=conversation_id, limit=50
            )
            return JSONResponse(
                {
                    "conversation_id": str(conversation_id),
                    "channel": conversation.channel,
                    "status": conversation.status,
                    "summary": conversation.summary,
                    "handoffs": [_summary(row) for row in handoffs],
                    "messages": [
                        {
                            "author": row.author,
                            "direction": row.direction,
                            "text": row.text,
                            "at": row.created_at.isoformat() if row.created_at else None,
                        }
                        for row in rows
                    ],
                }
            )

    return router


async def _body[BodyT: BaseModel](request: Request, model: type[BodyT]) -> BodyT | JSONResponse:
    """Parse a desk body, refusing rather than raising. An empty body is an empty object."""
    raw: Any = {}
    body = await request.body()
    if body.strip():
        try:
            raw = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "the body is not JSON"}, status_code=400)
    try:
        return model.model_validate(raw if isinstance(raw, dict) else {"_": raw})
    except Exception as exc:
        return JSONResponse({"error": f"that is not a valid request: {exc}"}, status_code=400)


__all__: Sequence[str] = ["DESK_AUTHOR", "desk_router"]
