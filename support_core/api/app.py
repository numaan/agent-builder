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

from fastapi import APIRouter, FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core.api.config import AppConfig
from support_core.api.desk import desk_router
from support_core.api.runtime import AppRuntime, build_runtime
from support_core.channels import ChannelAdapter, ChatState, InboundRejected
from support_core.channels.ag_ui import AgUiStream, RunAgentInput, RunTranslator, sse
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

AG_UI_TURN_TIMEOUT = 60.0
"""How long an AG-UI run's SSE stream waits for the turn it started to settle.

A run is one turn, and a turn is seconds; a queued turn waits on the drain worker, which has its
own budget. This bound stops an SSE response hanging for ever if the turn it is watching never
produces its end-of-turn frame - the connection is closed with a ``RUN_ERROR`` instead."""


RETRY_AFTER_SECONDS = 5
"""What a caller refused for want of a database connection is told to wait.

Seconds rather than minutes because the thing it is waiting for is a turn finishing, and a turn
is seconds long."""

OVERLOADED_DETAIL = (
    "this service has no free database connection; the message was not accepted, please retry"
)
"""Said to a caller whose request could not get a connection.

Honest in both halves. "Not accepted" is true: a pool timeout at the top of the handler happens
before or while the inbound row is written, so unlike a ``queued`` answer there is nothing
durable holding the customer's words, and telling them to retry is the only correct instruction.
It names no exception, no pool and no SQL - security review's "no error path puts internal state
into an outbound message" applies to a 503 as much as to a turn."""


def overloaded() -> JSONResponse:
    """The answer to a request that could not get a database connection.

    Security review finding S2: this used to be an unhandled
    ``sqlalchemy.exc.TimeoutError`` - a 500 with a full traceback, after a thirty-second wait,
    from an anonymous endpoint. A 503 with a ``Retry-After`` is what a caller can act on and what
    a load balancer already understands.
    """
    return JSONResponse(
        {"error": OVERLOADED_DETAIL},
        status_code=503,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


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

    async def on_pool_timeout(request: Request, exc: Exception) -> JSONResponse:
        """Every route's answer to "no database connection was free" (finding S2).

        On the application rather than in each handler, so that the desk endpoints and anything
        phase 7 mounts are covered by construction - the same reason the desk's bearer dependency
        is on the router rather than on each route. The webhook catches it itself as well,
        because it has a more specific thing to say and because a handler should not rely on a
        backstop for its own ordinary failure.
        """
        return overloaded()

    app.add_exception_handler(SQLTimeoutError, on_pool_timeout)
    app.include_router(_routes(runtime))
    if desk_token is not None:
        # DESIGN.md section 12: the human desk "is not a customer channel but uses the same API
        # surface". Mounted on the same app for the same reason section 4.1 gives for everything
        # else - one service, one image, one database - but never without a credential, and
        # never by default. The module docstring says why it is here rather than on its own port.
        app.include_router(desk_router(runtime, token=desk_token))
    if settings.serve_client:
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
        # A pack may ship its own front end (DESIGN.md section 12). When it does, serve it at
        # `/app` beside the built-in demo pages - the same `serve_client` gate, so a deployment
        # with its own hosting turns both off together. `ui.dir` is validated to a single segment
        # inside the pack, and a pack that ships no UI mounts nothing.
        pack_ui = pack.path / pack.manifest.ui.dir
        if pack_ui.is_dir():
            app.mount("/app", StaticFiles(directory=pack_ui, html=True), name="pack-ui")
    return app


def _routes(runtime: AppRuntime) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        """What is running here, and whether it can reach its database.

        Answers in milliseconds whatever the load is doing, which is the half of security review
        finding S2 that mattered most: this endpoint took 28.5 seconds during a flood and an
        orchestrator reads that as an instance to restart.
        :meth:`~support_core.api.runtime.AppRuntime.database_health` says how it stays fast.

        A saturated pool is reported as ``degraded`` with **200**, not 503. The process is alive
        and Postgres is reachable; what is full is a queue that empties by itself in seconds, and
        503 here is what takes the replica out of rotation - or restarts it - at the exact moment
        its in-flight turns would be lost. 503 stays where it was: a database this service cannot
        reach at all.
        """
        database = await runtime.database_health()
        in_use, capacity = runtime.pool_pressure()
        body = {
            "status": "ok" if database == "ok" else "degraded",
            "pool": {"in_use": in_use, "capacity": capacity},
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
        return JSONResponse(body, status_code=503 if database.startswith("unavailable") else 200)

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
        except SQLTimeoutError:
            return overloaded()
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

    @router.get("/channels/ag_ui")
    async def ag_ui_info() -> JSONResponse:
        """What an AG-UI client needs before its first run: the same static bits the socket's
        ``ready`` frame carries (provider, pack, the demo's one-click suggestions). No conversation
        state and no secret - everything here is already in ``/healthz``."""
        return JSONResponse(
            {
                "provider": runtime.provider_name,
                "pack": runtime.pack.manifest.id,
                "suggestions": list(runtime.config.suggestions),
                # The pack's own branding and starter messages, so a pack front end (and the
                # generic client) can theme per-pack with no assets. `suggestions` above stays the
                # deployment's (the demo config); `ui.suggestions` is the pack's own.
                "ui": json.loads(runtime.pack.manifest.ui.model_dump_json()),
            }
        )

    @router.post("/channels/ag_ui")
    async def ag_ui_run(request: Request) -> Response:
        """One AG-UI run over the web chat channel (see :mod:`support_core.channels.ag_ui`).

        The body is an AG-UI ``RunAgentInput``; ``threadId`` is the web chat session key and
        stays in the body, never the URL (finding W9). The response is a ``text/event-stream`` of
        AG-UI events for the single turn this message runs - the same committed messages and the
        same approval gate the socket sees, in AG-UI's vocabulary.
        """
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "the body is not JSON"}, status_code=400)
        try:
            run_input = RunAgentInput.model_validate(body)
        except ValidationError:
            return JSONResponse({"error": "this is not an AG-UI RunAgentInput"}, status_code=400)
        text = run_input.latest_user_text()
        if text is None:
            return JSONResponse({"error": "no user message to run"}, status_code=400)
        thread_id = run_input.threadId or new_session_key()
        run_id = run_input.runId or f"run_{uuid.uuid4().hex}"
        try:
            # Validate the session key by the web chat adapter's own rules before opening a stream.
            key = runtime.adapter(WEB_CHAT).conversation_key({"session": thread_id})
        except InboundRejected as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        async def events() -> AsyncIterator[str]:
            translator = RunTranslator(thread_id, run_id)
            stream = AgUiStream()
            # Watch the key before delivering, so `deliver_inbound`'s `attach` moves this stream
            # onto the conversation before the turn runs (the same ordering the socket relies on).
            runtime.connections.wait(key, stream)
            try:
                yield sse(translator.run_started())
                try:
                    await runtime.accept({"session": thread_id, "text": text}, channel=WEB_CHAT)
                except InboundRejected as exc:
                    yield sse(translator.run_error(str(exc)))
                    return
                except SQLTimeoutError:
                    yield sse(translator.run_error(OVERLOADED_DETAIL))
                    return
                # Drain committed frames until the end-of-turn frame. Inline turns have already
                # queued theirs; a queued turn's arrive from the drain worker. A timeout stops a
                # run that never settles rather than holding the response open for ever.
                while not translator.done:
                    try:
                        frame = await asyncio.wait_for(
                            stream.queue.get(), timeout=AG_UI_TURN_TIMEOUT
                        )
                    except TimeoutError:
                        yield sse(translator.run_error("the run did not finish in time"))
                        return
                    for event in translator.translate(frame):
                        yield sse(event)
                yield sse(translator.run_finished())
            finally:
                runtime.connections.forget(stream)

        return StreamingResponse(events(), media_type="text/event-stream")

    if runtime.config.serve_client:

        @router.get("/")
        async def client() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @router.get("/agui")
        async def ag_ui_client() -> FileResponse:
            return FileResponse(STATIC_DIR / "agui.html")

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
    except SQLTimeoutError:
        # The opening handshake reads the transcript, and that read is on the same pool as the
        # turns (finding S2). Say so and close, rather than raising out of the ASGI handler.
        await connection.push({"type": "error", "detail": OVERLOADED_DETAIL, "fatal": True})
        await socket.close(code=1013)  # 1013: try again later
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
    try:
        accepted = await runtime.deliver_inbound(inbound)
    except SQLTimeoutError:
        # The socket's version of the webhook's 503 (finding S2). Not fatal: the connection is
        # fine, this message was not accepted, and the customer can send it again.
        await connection.push({"type": "error", "detail": OVERLOADED_DETAIL})
        return
    session.conversation = accepted.conversation_id
    if accepted.queued:
        # Per-caller, so it is not part of the ``turn`` frame the whole conversation is told
        # about (finding W3). The turn itself is announced by the runtime, to every connection
        # watching, whoever ran it.
        await connection.push({"type": "queued", "conversation_id": str(accepted.conversation_id)})
