"""The FastAPI application. Implements DESIGN.md section 4.1 ("the service exposes an HTTP API
for channels") with section 12's web chat endpoint.

    app = create_app(load_pack("./pack")) - DESIGN.md section 4.1

Three endpoints and a page:

``GET /healthz``
    What is running: the pack and its fingerprint, the provider, the channels served.

``POST /channels/web_chat/messages``
    The webhook shape - one payload in, an acknowledgement out. It exists in phase W because it
    is the shape phase 7's email webhook takes, and because it is where "return promptly rather
    than hold a connection per waiter" is visible: a conversation whose lock is held elsewhere
    is answered ``queued`` and handed to the drain worker (phase 2 review finding R7).

``WS /channels/web_chat/ws``
    The conversation itself. The socket carries the customer's messages in and the *committed*
    outbound messages out. It is identified by a session key, never by the connection: a
    conversation suspended waiting for the customer (DESIGN.md section 7.2) resumes on whichever
    connection the reply arrives on, including a different one after a reconnect.

``GET /``
    The demo client, when ``AppConfig.serve_client`` is on.

``/desk/...``
    The human agent desk of DESIGN.md section 13: the handoff queue, a reply, a resume with a
    state patch, a close, a human approval, and the transcript a packet links to. **Off unless
    ``AppConfig.serve_desk`` says otherwise, and behind a bearer token when it is** - phase W
    review finding W1, where this router was found mounted beside the customer chat, on by
    default and open. See :mod:`support_core.api.desk`.

    Why it stays on this application rather than moving to a listener of its own, which was the
    other half of that finding: DESIGN.md section 4.1 makes a deployment "one service", section
    12 says the desk "uses the same API surface", and - the practical reason - a desk ``reply``
    reaches a customer's open socket only from the process that holds that socket, because the
    connection registry is a dictionary in one process (phase W self-critique item 4). Splitting
    the desk onto its own listener today would quietly break live delivery of a human's reply
    until phase 7's fan-out lands, and would trade a hole that is now closed for a regression
    that is not. What made the exposure was never the shared port; it was a default that served
    customer data to anyone who asked. Phase 7, which owns both the fan-out and this surface, can
    take the desk off the customer's listener with nothing to lose by then.

What is not here: the email webhook, tracing, metrics and the replay endpoint are phase 7, and
this factory is what that phase extends.
"""

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api.config import AppConfig
from support_core.api.desk import desk_router
from support_core.api.runtime import AppRuntime, build_runtime
from support_core.channels import ChannelAdapter, ChatState, InboundRejected
from support_core.channels.web_chat import CHANNEL as WEB_CHAT
from support_core.engine.hooks import EngineHooks
from support_core.graph.manifest import Channel
from support_core.graph.pack import Pack
from support_core.llm.provider import LLMProvider

WEB_CHAT_CHANNEL: Channel = "web_chat"
"""The same value as :data:`~support_core.channels.web_chat.CHANNEL`, typed as the manifest's
``Channel`` so the app can hand it to the runtime without a cast."""

STATIC_DIR = Path(__file__).resolve().parent / "static"
"""The demo client. Plain HTML, CSS and JavaScript: no build step, no framework, and no request
to anything but this service, so the page works on a laptop with no network."""

SESSION_BYTES = 12
"""Length of a session key the server generates for a client that arrives without one."""

HELLO_TIMEOUT = 15.0
"""How long a connection may stay open without saying who it is.

The key arrives in a frame rather than in the URL (finding W9), so a client that connects and
says nothing would otherwise hold a socket for ever for free."""


def new_session_key() -> str:
    """A fresh web chat session key.

    Random rather than sequential because it is the only thing standing between one customer's
    conversation and another's: a caller who knows a key can read that conversation's transcript
    and speak into it. Phase 7's deployment guidance has to say so; phase W says it here.
    """
    return secrets.token_hex(SESSION_BYTES)


class WebSocketConnection:
    """A :class:`~support_core.channels.web_chat.ChatConnection` over a real WebSocket.

    The lock is not decoration: the engine pushes committed messages from whichever task is
    running the turn - this connection's, another connection's, or a drain worker's - and two
    tasks writing one socket interleave frames.
    """

    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket
        self._lock = asyncio.Lock()

    async def push(self, event: Mapping[str, Any]) -> None:
        async with self._lock:
            await self.socket.send_text(json.dumps(dict(event)))


def create_app(
    pack: Pack,
    *,
    config: AppConfig | None = None,
    engine: AsyncEngine | None = None,
    provider: LLMProvider | None = None,
    hooks: EngineHooks | None = None,
    adapters: Sequence[ChannelAdapter] | None = None,
) -> FastAPI:
    """Build the service for one pack (DESIGN.md section 4.1).

    ``engine``, ``provider``, ``hooks`` and ``adapters`` are seams for tests and for a pack
    repository that wants to supply its own; the ordinary caller passes a pack and a config.
    """
    settings = config or AppConfig()
    # Before anything is built, because a desk that cannot be served safely must stop the
    # application rather than start one (finding W1).
    desk_token = settings.build_desk_credential() if settings.serve_desk else None
    runtime = build_runtime(
        pack,
        settings,
        engine=engine,
        provider=provider,
        hooks=hooks,
        adapters=adapters,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(
        title=settings.title,
        version=pack.manifest.version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    app.include_router(_routes(runtime))
    if desk_token is not None:
        # DESIGN.md section 12: the human desk "is not a customer channel but uses the same API
        # surface". Mounted on the same app for the same reason section 4.1 gives for everything
        # else - one service, one image, one database - but never without a credential, and
        # never by default. The module docstring says why it is here rather than on its own port.
        app.include_router(desk_router(runtime, token=desk_token))
    if settings.serve_client:
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _routes(runtime: AppRuntime) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        """What is running here, and whether it can reach its database."""
        database = "ok"
        try:
            async with runtime.engine.connect() as connection:
                await connection.execute(sql_text("SELECT 1"))
        except Exception as exc:  # a health endpoint reports the failure, it does not raise
            database = f"unavailable: {type(exc).__name__}"
        body = {
            "status": "ok" if database == "ok" else "degraded",
            "pack": {
                "id": runtime.pack.manifest.id,
                "version": runtime.pack.manifest.version,
                "fingerprint": runtime.pack.pin.fingerprint,
                "entry_graph": runtime.pack.manifest.entry_graph,
            },
            "provider": runtime.provider_name,
            "channels": sorted(runtime.hub.adapters),
            "database": database,
        }
        return JSONResponse(body, status_code=200 if database == "ok" else 503)

    @router.post("/channels/web_chat/messages")
    async def web_chat_message(request: Request) -> JSONResponse:
        """One customer message, the way a channel webhook delivers one.

        Returns as soon as the turn this call could run is finished - or immediately, with
        ``queued: true``, if another turn holds the conversation's lock. It never waits for the
        lock: that is the whole of the queue-and-return mode, and the drain worker is what makes
        it safe.
        """
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "the body is not JSON"}, status_code=400)
        try:
            accepted = await runtime.accept(payload, channel=WEB_CHAT)
        except InboundRejected as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "conversation_id": str(accepted.conversation_id),
                "status": accepted.status,
                "queued": accepted.queued,
                "created": accepted.created,
            },
            status_code=202 if accepted.queued else 200,
        )

    @router.websocket("/channels/web_chat/ws")
    async def web_chat_socket(socket: WebSocket) -> None:
        await _serve_socket(runtime, socket)

    if runtime.config.serve_client:

        @router.get("/")
        async def client() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return router


async def _read_text(socket: WebSocket) -> str | None:
    """The next text frame, or ``None`` if the client sent something that is not one.

    ``WebSocket.receive_text`` reads ``message["text"]`` and a binary frame has no such key, so
    it raised ``KeyError: 'text'`` straight out of the handler: an unhandled exception on this
    phase's public entry point, an ASGI traceback per frame, and an abnormal close - producible
    by any anonymous client, which is phase W review finding W2. A transport that will not carry
    a kind of frame says so and closes; it does not fall over.
    """
    message = await socket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text = message.get("text")
    return text if isinstance(text, str) else None


class _Session:
    """One web chat connection's own state: which session key, and which conversation yet.

    ``conversation`` is ``None`` until somebody's message creates one. Opening a socket used to
    create a durable ``conversation`` and ``run`` row before a single word had been typed, from
    an unauthenticated endpoint with no rate limit, so a loop of connects filled two tables
    (finding W8). A connect now *resolves*; the first message creates.

    A connection with no conversation yet waits in the registry under its channel key, and
    :meth:`~support_core.channels.web_chat.ConnectionRegistry.attach` moves it across when the
    row appears - whoever created it, this socket or a ``POST`` or another tab.
    """

    def __init__(self, runtime: AppRuntime, connection: "WebSocketConnection", key: str) -> None:
        self.runtime = runtime
        self.connection = connection
        self.key = key
        self.conversation: uuid.UUID | None = None

    def watch(self, conversation_id: uuid.UUID | None) -> None:
        """Start receiving what this conversation says, or wait for it to exist."""
        self.conversation = conversation_id
        if conversation_id is None:
            self.runtime.connections.wait(self.key, self.connection)
        else:
            self.runtime.connections.add(conversation_id, self.connection)

    def release(self) -> None:
        # `forget` rather than `discard`: this connection may have been moved onto a conversation
        # by somebody else's message since it registered, so it cannot know where it ended up.
        self.runtime.connections.forget(self.connection)


async def _serve_socket(runtime: AppRuntime, socket: WebSocket) -> None:
    """One web chat connection, from accept to disconnect.

    The connection is registered against the *conversation*, so what it receives is what the
    conversation says, whoever ran the turn - this socket's own request, another tab's, or the
    drain worker's. Nothing about the conversation is held in this function: it is read from the
    database when the socket opens and written by the engine as turns commit.

    The session key arrives in the first frame rather than in the query string (finding W9). It
    is the whole of this channel's access control, and a URL is written verbatim into uvicorn's
    access log - and into any proxy, CDN or APM log in front of it - so the one secret a customer
    holds was being written down by four systems that have no use for it. A frame is not.
    """
    await socket.accept()
    connection = WebSocketConnection(socket)
    try:
        hello = await asyncio.wait_for(_read_text(socket), timeout=HELLO_TIMEOUT)
    except TimeoutError:
        await connection.push(
            {"type": "error", "detail": "no opening frame arrived", "fatal": True}
        )
        await socket.close(code=1008)
        return
    except WebSocketDisconnect:
        return

    try:
        key = _session_of(runtime, hello)
    except InboundRejected as exc:
        await connection.push({"type": "error", "detail": str(exc), "fatal": True})
        await socket.close(code=1008 if hello is not None else 1003)
        return

    session = _Session(runtime, connection, key)
    try:
        known = await runtime.conversation_if_known(WEB_CHAT_CHANNEL, key)
        session.watch(known.id if known is not None else None)
        state = ChatState(session=key) if known is None else await runtime.state(known.id)
        await connection.push(
            {
                "type": "ready",
                "session": key,
                "provider": runtime.provider_name,
                "pack": runtime.pack.manifest.id,
                "suggestions": list(runtime.config.suggestions),
                **json.loads(state.model_dump_json()),
            }
        )

        while True:
            raw = await _read_text(socket)
            if raw is None:
                await connection.push(
                    {
                        "type": "error",
                        "detail": "this endpoint reads JSON text frames; a binary frame is not one",
                        "fatal": True,
                    }
                )
                await socket.close(code=1003)  # 1003: unsupported data
                return
            await _handle_frame(session, raw)
    except WebSocketDisconnect:
        return
    finally:
        session.release()


def _session_of(runtime: AppRuntime, hello: str | None) -> str:
    """The session key from the opening frame, validated, or a fresh one.

    ``{"type": "hello"}`` with no key is a new conversation and the server names it, which is
    what a first-time browser sends. Anything else is refused before a row exists.
    """
    if hello is None:
        msg = "the opening frame must be JSON text, not binary"
        raise InboundRejected(msg)
    try:
        opening = json.loads(hello)
    except ValueError as exc:
        msg = "the opening frame is not JSON"
        raise InboundRejected(msg) from exc
    if not isinstance(opening, dict) or opening.get("type") != "hello":
        msg = 'the first frame must be {"type": "hello", "session": "<key or omitted>"}'
        raise InboundRejected(msg)
    requested = opening.get("session")
    if requested is None:
        return new_session_key()
    return runtime.adapter(WEB_CHAT).conversation_key({"session": requested})


async def _handle_frame(session: _Session, raw: str) -> None:
    """One frame from the browser.

    The conversation is created here rather than at connect (finding W8), and it is created
    *before* the turn runs and after the payload has been found to be a message at all - so a
    malformed frame writes nothing, and a socket is watching its own conversation before the
    engine can produce a message for it.
    """
    runtime = session.runtime
    connection = session.connection
    try:
        payload = json.loads(raw)
    except ValueError:
        await connection.push({"type": "error", "detail": "that frame is not JSON"})
        return
    if not isinstance(payload, dict):
        await connection.push({"type": "error", "detail": "a frame must be a JSON object"})
        return
    # The session is the connection's, not the frame's: a socket may not speak for a
    # conversation it did not open.
    payload["session"] = session.key
    try:
        inbound = await runtime.adapter(WEB_CHAT).parse_inbound(payload)
    except InboundRejected as exc:
        await connection.push({"type": "error", "detail": str(exc)})
        return
    # `deliver_inbound` creates the conversation if this is the first message on this key, and
    # attaches every connection waiting on the key - this one included - before the turn runs.
    accepted = await runtime.deliver_inbound(inbound)
    session.conversation = accepted.conversation_id
    if accepted.queued:
        # Per-caller, so it is not part of the ``turn`` frame the whole conversation is told
        # about (finding W3). The turn itself is announced by the runtime, to every connection
        # watching, whoever ran it.
        await connection.push({"type": "queued", "conversation_id": str(accepted.conversation_id)})
