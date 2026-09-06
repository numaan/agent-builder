"""A real server for the API tests.

The web chat channel is a WebSocket, and a WebSocket is exactly the thing an in-process test
client abstracts away: the frames, the ordering, the disconnect, the reconnect on a *different*
connection. So these tests run the application under uvicorn on an ephemeral port and talk to it
over TCP with an ordinary HTTP and WebSocket client, which is the same path a browser takes.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
import websockets
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from support_core import load_pack
from support_core.api import AppConfig, create_app
from support_core.engine.hooks import EngineHooks
from support_core.llm.provider import LLMProvider

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_PACKS = Path(__file__).resolve().parent / "packs"
CASSETTES = Path(__file__).resolve().parent / "cassettes"
ACME = REPO_ROOT / "packs" / "acme_billing"
DEMO_CONFIG = REPO_ROOT / "demo" / "acme_web_chat.json"

START_TIMEOUT = 20.0


def build_app(
    pack_path: Path,
    engine: AsyncEngine,
    *,
    config: AppConfig | None = None,
    provider: LLMProvider | None = None,
    hooks: EngineHooks | None = None,
) -> FastAPI:
    settings = config or AppConfig(pack=pack_path, provider="none")
    return create_app(
        load_pack(pack_path), config=settings, engine=engine, provider=provider, hooks=hooks
    )


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[str]:
    """Run ``app`` under uvicorn on a free port; yield ``host:port``."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        while not server.started:
            if task.done():  # pragma: no cover - a failed startup re-raises here
                await task
            if asyncio.get_running_loop().time() > deadline:  # pragma: no cover
                msg = "the test server did not start"
                raise TimeoutError(msg)
            await asyncio.sleep(0.01)
        socket = server.servers[0].sockets[0]
        yield f"127.0.0.1:{socket.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=START_TIMEOUT)


class Chat:
    """One web chat connection, with a little patience built in."""

    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.messages: list[str] = []
        self.events: list[dict[str, Any]] = []

    async def recv(self, timeout: float = 30.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.socket.recv(), timeout)
        event: dict[str, Any] = json.loads(raw)
        self.events.append(event)
        if event["type"] == "message":
            self.messages.append(event["text"])
        return event

    async def ready(self, timeout: float = 30.0) -> dict[str, Any]:
        event = await self.recv(timeout)
        assert event["type"] == "ready", event
        return event

    async def say(self, text: str, timeout: float = 60.0) -> dict[str, Any]:
        """Send one message and read everything until the turn's own event arrives."""
        await self.socket.send(json.dumps({"type": "message", "text": text}))
        while True:
            event = await self.recv(timeout)
            if event["type"] in {"turn", "error"}:
                return event

    async def send_raw(self, raw: str) -> None:
        await self.socket.send(raw)


@asynccontextmanager
async def chatting(
    host: str, session: str | None = None, *, hello: bool = True
) -> AsyncIterator[Chat]:
    """One web chat connection, having said hello.

    The session key travels in the opening frame rather than in the query string (review finding
    W9): it is the whole of this channel's access control and a URL is written into every access
    log on the way. ``hello=False`` is for the tests that drive the opening handshake themselves.
    """
    url = f"ws://{host}/channels/web_chat/ws"
    async with websockets.connect(url, open_timeout=START_TIMEOUT) as socket:
        if hello:
            opening: dict[str, Any] = {"type": "hello"}
            if session is not None:
                opening["session"] = session
            await socket.send(json.dumps(opening))
        yield Chat(socket)


def texts(events: Sequence[dict[str, Any]]) -> list[str]:
    return [event["text"] for event in events if event.get("type") == "message"]


def reset_acme_backend() -> None:
    """Put the sample pack's in-memory billing system and passcodes back to seed.

    Through :func:`~support_core.tools.loading.import_pack_tools`, because a pack imported by
    path and a pack imported as ``packs.acme_billing`` are two module objects with two
    independent fakes; resetting the wrong one leaves yesterday's refund in place and sends the
    conversation down the denial path (reviews/phase-4.md).
    """
    from support_core.tools.loading import import_pack_tools

    import_pack_tools(ACME).reset_backend()
