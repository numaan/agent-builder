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

What is not here: the desk API, the email webhook, tracing, metrics and the replay endpoint are
phase 7, and this factory is what that phase extends.
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
from support_core.api.runtime import AppRuntime, build_runtime
from support_core.channels import ChannelAdapter, InboundRejected
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


async def _serve_socket(runtime: AppRuntime, socket: WebSocket) -> None:
    """One web chat connection, from accept to disconnect.

    The connection is registered against the *conversation*, so what it receives is what the
    conversation says, whoever ran the turn - this socket's own request, another tab's, or the
    drain worker's. Nothing about the conversation is held in this function: it is read from the
    database when the socket opens and written by the engine as turns commit.
    """
    await socket.accept()
    requested = socket.query_params.get("session")
    adapter = runtime.adapter(WEB_CHAT)
    connection = WebSocketConnection(socket)
    conversation_id: uuid.UUID | None = None
    try:
        try:
            session = requested if requested else new_session_key()
            if requested:
                adapter.conversation_key({"session": requested})
            conversation = await runtime.conversation_for_key(WEB_CHAT_CHANNEL, session)
        except InboundRejected as exc:
            await connection.push({"type": "error", "detail": str(exc), "fatal": True})
            await socket.close(code=1008)
            return

        conversation_id = conversation.id
        runtime.connections.add(conversation_id, connection)
        state = await runtime.state(conversation_id)
        await connection.push(
            {
                "type": "ready",
                "session": session,
                "provider": runtime.provider_name,
                "pack": runtime.pack.manifest.id,
                "suggestions": list(runtime.config.suggestions),
                **json.loads(state.model_dump_json()),
            }
        )

        while True:
            raw = await socket.receive_text()
            await _handle_frame(runtime, connection, session, conversation_id, raw)
    except WebSocketDisconnect:
        return
    finally:
        if conversation_id is not None:
            runtime.connections.discard(conversation_id, connection)


async def _handle_frame(
    runtime: AppRuntime,
    connection: WebSocketConnection,
    session: str,
    conversation_id: uuid.UUID,
    raw: str,
) -> None:
    """One frame from the browser."""
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
    payload["session"] = session
    try:
        accepted = await runtime.accept(payload, channel=WEB_CHAT)
    except InboundRejected as exc:
        await connection.push({"type": "error", "detail": str(exc)})
        return
    state = await runtime.state(accepted.conversation_id)
    await connection.push(
        {
            "type": "turn",
            "queued": accepted.queued,
            **json.loads(state.model_dump_json()),
        }
    )
