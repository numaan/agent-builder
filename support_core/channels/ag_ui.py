"""AG-UI transport over the web chat channel. Implements the AG-UI protocol
(https://github.com/ag-ui-protocol/ag-ui) as a *presentation* of the same
conversation the WebSocket channel serves.

DESIGN.md section 12 already allows "WebSocket or SSE" for web chat and streams
"the final message only"; AG-UI is that SSE option, spoken in a standard event
vocabulary a CopilotKit-style front end understands. It is **not a new channel**:
a run is delivered over the ordinary ``web_chat`` channel, against the ordinary
session key, so an AG-UI client and a socket on the same session are two
connections on one conversation - exactly the property section 12 protects.

This module holds the parts that carry no dependency on the API layer: the
request model, the connection that buffers pushed frames, and the translator from
the engine's committed frames (``{"type": "message", ...}`` / ``{"type": "turn",
...}`` - see :mod:`support_core.channels.web_chat`) into AG-UI events. The route in
:mod:`support_core.api.app` owns registration, delivery and draining, because those
need the runtime and importing it here would be a cycle.

The mapping, and why:

* Each committed agent/human message becomes ``TEXT_MESSAGE_START`` ->
  ``TEXT_MESSAGE_CONTENT`` (the whole text in one chunk - the engine commits whole
  messages and never streams tokens) -> ``TEXT_MESSAGE_END``.
* The end-of-turn frame becomes a ``STATE_SNAPSHOT`` carrying ``status`` and the
  same ``awaiting`` summary the socket client is given (``question`` / ``confirm``
  / ``handoff``), so an AG-UI client can render the status pill and every kind of
  wait.
* When the turn ends at a ``confirm`` gate (DESIGN.md section 8.2), the proposed
  tool is *additionally* emitted as ``TOOL_CALL_START`` / ``TOOL_CALL_ARGS`` /
  ``TOOL_CALL_END`` naming the tool (e.g. ``issue_refund``). That is AG-UI's
  human-in-the-loop shape: the front end renders the tool call as an approval, and
  the customer approves by sending the next message ("yes"), which is how this
  engine's approval has always worked. The argument hash is never exposed, for the
  reason :class:`~support_core.channels.web_chat.AwaitingSummary` gives.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# AG-UI event type names (a subset of the protocol - the ones this transport emits).
RUN_STARTED = "RUN_STARTED"
RUN_FINISHED = "RUN_FINISHED"
RUN_ERROR = "RUN_ERROR"
TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
TOOL_CALL_START = "TOOL_CALL_START"
TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
TOOL_CALL_END = "TOOL_CALL_END"
STATE_SNAPSHOT = "STATE_SNAPSHOT"


def sse(event: Mapping[str, Any]) -> str:
    """One AG-UI event as an SSE frame: ``data: <json>\\n\\n``."""
    return f"data: {json.dumps(dict(event))}\n\n"


class AgUiMessage(BaseModel):
    """One message in an AG-UI ``RunAgentInput``.

    Lenient on purpose: a real AG-UI client sends ``id``, ``role``, ``content`` and
    sometimes tool fields, and this transport reads only role and content.
    """

    model_config = ConfigDict(extra="allow")

    role: str
    content: str | None = None
    id: str | None = None


class RunAgentInput(BaseModel):
    """The body an AG-UI client POSTs to start a run.

    Only ``threadId`` and the latest user message are used here; ``tools``,
    ``context``, ``state`` and ``forwardedProps`` are accepted (a client sends them)
    and ignored. ``threadId`` is this transport's carrier for the web chat session
    key, so it stays in the body and never the URL - the same reason phase W review
    finding W9 keeps the socket's key out of the query string.
    """

    model_config = ConfigDict(extra="allow")

    threadId: str | None = None
    runId: str | None = None
    messages: list[AgUiMessage] = Field(default_factory=list)

    def latest_user_text(self) -> str | None:
        """The text of the last user-authored message, or ``None`` if there is none."""
        for message in reversed(self.messages):
            if message.role == "user" and isinstance(message.content, str):
                text = message.content.strip()
                if text:
                    return text
        return None


class AgUiStream:
    """A :class:`~support_core.channels.web_chat.ChatConnection` that buffers frames.

    Registered in the web chat :class:`~support_core.channels.web_chat.ConnectionRegistry`
    exactly like a socket, so the engine's broadcasts reach it; the route drains the
    queue and translates each frame. Unbounded because one run's frames are few and
    the queue lives only for that run.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()

    async def push(self, event: Mapping[str, Any]) -> None:
        await self.queue.put(dict(event))


class RunTranslator:
    """Turns the engine's committed frames into AG-UI events for one run.

    Stateful across a run: it numbers the messages it has seen and remembers the last
    agent line, so a ``confirm`` gate can show what is being approved. ``done`` goes
    true when the end-of-turn frame has been translated - the signal for the route to
    finish the run.
    """

    def __init__(self, thread_id: str, run_id: str) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self._message_seq = 0
        self._last_agent_text = ""
        self.done = False

    def run_started(self) -> dict[str, Any]:
        return {"type": RUN_STARTED, "threadId": self.thread_id, "runId": self.run_id}

    def run_finished(self) -> dict[str, Any]:
        return {"type": RUN_FINISHED, "threadId": self.thread_id, "runId": self.run_id}

    def run_error(self, message: str) -> dict[str, Any]:
        return {"type": RUN_ERROR, "message": message, "runId": self.run_id}

    def translate(self, frame: Mapping[str, Any]) -> list[dict[str, Any]]:
        """AG-UI events for one internal frame (may be empty)."""
        kind = frame.get("type")
        if kind == "message":
            return self._message(frame)
        if kind == "turn":
            self.done = True
            return self._turn(frame)
        # `queued` and anything else the socket path handles per-connection: nothing to show.
        return []

    def _message(self, frame: Mapping[str, Any]) -> list[dict[str, Any]]:
        text = str(frame.get("text", ""))
        author = frame.get("author", "agent")
        if author != "human":  # agent and human both render as the assistant side
            self._last_agent_text = text
        self._message_seq += 1
        message_id = f"{self.run_id}-msg-{self._message_seq}"
        return [
            {"type": TEXT_MESSAGE_START, "messageId": message_id, "role": "assistant"},
            {"type": TEXT_MESSAGE_CONTENT, "messageId": message_id, "delta": text},
            {"type": TEXT_MESSAGE_END, "messageId": message_id},
        ]

    def _turn(self, frame: Mapping[str, Any]) -> list[dict[str, Any]]:
        awaiting = frame.get("awaiting")
        summary = awaiting if isinstance(awaiting, Mapping) else None
        form = summary.get("form") if summary else None
        # Keep the (possibly large) form schema out of the state snapshot; it rides the tool call.
        snapshot_awaiting = {k: v for k, v in summary.items() if k != "form"} if summary else None
        events: list[dict[str, Any]] = [
            {
                "type": STATE_SNAPSHOT,
                "snapshot": {"status": frame.get("status"), "awaiting": snapshot_awaiting},
            }
        ]
        if summary and form:
            # Generative UI: the node declares a form, so it is rendered rather than answered as
            # free text. The front end renders `render_form`'s schema and returns the values.
            call_id = f"{self.run_id}-form-{summary.get('node') or 'form'}"
            events += [
                {"type": TOOL_CALL_START, "toolCallId": call_id, "toolCallName": "render_form"},
                {"type": TOOL_CALL_ARGS, "toolCallId": call_id, "delta": json.dumps(form)},
                {"type": TOOL_CALL_END, "toolCallId": call_id},
            ]
        elif summary and summary.get("kind") == "confirm" and summary.get("tool"):
            # A confirm gate with no form is a tool call the front end renders as an approval.
            tool = str(summary["tool"])
            call_id = f"{self.run_id}-approve-{summary.get('node') or tool}"
            args = json.dumps({"proposal": self._last_agent_text})
            events += [
                {"type": TOOL_CALL_START, "toolCallId": call_id, "toolCallName": tool},
                {"type": TOOL_CALL_ARGS, "toolCallId": call_id, "delta": args},
                {"type": TOOL_CALL_END, "toolCallId": call_id},
            ]
        return events
