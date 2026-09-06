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

**Every endpoint here is behind a bearer token, and the router cannot be built without one.**
The desk is not a customer channel: it lists every conversation in the deployment, reads any
transcript, and writes into any of them - and ``approve`` is the human half of
``requires_human_approval``, so an unauthenticated caller could countersign their own action,
which is the one property a second signature exists to have. Phase W's review found this router
mounted beside the customer chat, on the same port, on by default and open (finding W1), and
reproduced all three: enumerating the queue, reading another customer's transcript, and writing
into it. :func:`~support_core.api.config.AppConfig.build_desk_credential` is what makes a missing
token a startup failure rather than an open desk; :func:`require_desk_token` is what makes a
wrong one a 401.

What that is *not*: it is not per-operator identity, it is not rotation, and it is not an audit
of who did what - the desk takes a ``human_id`` on every action for the last of those. Phase 7
owns this surface and can put real operator accounts on it. This is the part that cannot wait,
because the alternative to it is customer data served to anyone who can reach the chat.
"""

import secrets
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
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


def _at(when: Any) -> str | None:
    return str(when.isoformat()) if when else None


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


def require_desk_token(token: str) -> Any:
    """A dependency that refuses every desk request without this exact bearer token.

    Three details that are the difference between a check and a decoration:

    * it is attached to the **router**, not to each endpoint, so a route added later is behind it
      by construction rather than by whoever writes it remembering (``test_desk_auth.py``
      enumerates the router and asserts exactly that);
    * the comparison is :func:`secrets.compare_digest`, so the time a refusal takes does not
      describe the token;
    * the credential is read from the ``Authorization`` header and from nowhere else. Not a query
      parameter: phase W's review found the web chat session key in uvicorn's access log because
      it travelled in a URL (finding W9), and a desk token in a URL would be the same defect with
      a much larger blast radius.
    """

    async def check(authorization: str | None = Header(default=None)) -> None:
        scheme, _, presented = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(presented.strip(), token):
            raise HTTPException(
                status_code=401,
                detail="the desk needs a bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return Depends(check)


def desk_router(runtime: AppRuntime, *, token: str) -> APIRouter:
    """The desk endpoints, mounted by :func:`~support_core.api.app.create_app`.

    ``token`` is required rather than optional, and there is no value of it that means "no
    credential": :func:`~support_core.api.config.AppConfig.build_desk_credential` is the only
    thing that produces one and it refuses an empty or short string. A caller that wanted an
    open desk would have to write the check out of this module.
    """
    router = APIRouter(prefix="/desk", tags=["desk"], dependencies=[require_desk_token(token)])

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
        """One handoff, with the whole packet (DESIGN.md section 13).

        Plus anything the customer has said *since* it was raised. A run parked
        ``waiting_human`` queues customer messages rather than running them - resuming early
        would drop the wait the pack asked for - so those messages were durable and invisible,
        and a customer answering "a specialist will pick this up" was talking into a void
        (review finding P6). They are part of what the person picking this up needs to read.
        """
        row = await _load(handoff_id)
        if row is None:
            return JSONResponse({"error": "no such handoff"}, status_code=404)
        async with runtime.executor.sessions() as session, session.begin():
            waiting = await repo.pending_inbound(session, row.conversation_id)
            queued = [{"text": message.text, "at": _at(message.created_at)} for message in waiting]
        return JSONResponse({**_summary(row), "packet": row.packet, "waiting_messages": queued})

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

        It also approves *the action this handoff showed*, which is a different property and was
        the weaker half (review finding P4). The endpoint used to take the newest live approval
        in the whole **conversation**, whatever the handoff said, whatever run it belonged to,
        and whether or not the handoff was still open - so a human reading a packet that said
        "issue_refund - indeterminate" could sign a different, newer action with one click, and a
        resolved handoff still signed. Now the approval is resolved *from the packet*: the run,
        the confirm node, the tool and the arguments the human was looking at, all four, and a
        handoff that is no longer open signs nothing.

        Idempotent in the way that matters: a second call writes a second human row, and the
        runtime consumes exactly one per call, so a double click does not authorise a second
        refund. (Since the supersede of review finding P5, the second human row also cancels the
        first, so a double click leaves exactly one.) The customer's approval is still single-use
        underneath it.
        """
        body = await _body(request, ApproveBody)
        if isinstance(body, JSONResponse):
            return body
        row = await _load(handoff_id)
        if row is None:
            return JSONResponse({"error": "no such handoff"}, status_code=404)
        if row.status != "open":
            return JSONResponse(
                {"error": f"this handoff is {row.status}, so there is nothing left to sign"},
                status_code=409,
            )
        shown = (row.packet or {}).get("pending_action")
        if not isinstance(shown, dict) or shown.get("status") != "proposed":
            return JSONResponse(
                {
                    "error": (
                        "this handoff does not show an action awaiting a signature, so there is "
                        "nothing here for a human to sign. Re-read the handoff: an action "
                        "proposed since it was raised is not the one you were shown"
                    )
                },
                status_code=409,
            )
        async with runtime.executor.sessions() as session, session.begin():
            live = await repo.live_approvals(session, row.conversation_id)
            candidates = [
                approval
                for approval in live
                if approval.approved_by == "customer"
                and approval.run_id == row.run_id
                and approval.node_id == shown.get("node_id")
                and approval.tool == shown.get("tool")
                and dict(approval.args or {}) == dict(shown.get("args") or {})
            ]
            if not candidates:
                return JSONResponse(
                    {
                        "error": (
                            f"the action this handoff showed - {shown.get('tool')} - is no "
                            f"longer awaiting a signature on this run; it has run, been "
                            f"superseded, or been withdrawn"
                        )
                    },
                    status_code=409,
                )
            template = candidates[-1]
            try:
                approval_id = await repo.record_human_approval(
                    session, template=template, now=runtime.executor.hooks.clock()
                )
            except ValueError as exc:
                # An approval bound to no run, frame and confirm node could never be consumed
                # (review finding P9). A legitimate 4xx, not a traceback.
                return JSONResponse({"error": str(exc)}, status_code=409)
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


__all__: Sequence[str] = ["DESK_AUTHOR", "desk_router", "require_desk_token"]
