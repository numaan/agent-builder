#!/usr/bin/env python3
"""Drive the running support-core web chat the way a browser does, and check it.

This is the interaction harness for the `run-agent-builder` skill. It speaks the
same WebSocket protocol as `support_core/api/static/app.js`:

    ->  {"type": "hello"}                     (omit "session" for a fresh conversation)
    <-  {"type": "ready", "session", "provider", "pack", "suggestions", "history", ...}
    ->  {"type": "message", "text": "..."}
    <-  {"type": "message", "author": "agent"|"customer", "text": "..."}   (zero or more)
    <-  {"type": "turn", "status", "awaiting": {...}|null}                 (end of turn)

The demo runs against the *replay* provider (recorded cassettes), so the flow below
is deterministic - BUT the sample pack's billing system is an in-memory fake that is
global to the server process (packs/acme_billing/tools). Issuing the refund mutates
it: a second conversation for the same customer then finds nothing to refund and
hands off to a human instead. So this driver must run against a FRESHLY STARTED
server. Restart uvicorn, then run this once.

The `awaiting` field on a `turn` frame carries the run's gate, with a `kind`:
    - "question": the agent is asking the customer for something (e.g. the passcode).
    - "confirm":  an APPROVAL gate - the agent proposed a tool call and stopped.
    - "handoff":  the run parked and a packet went to a human queue.

The script asserts the four behaviours the demo exists to show (identity gate,
deferred topic + approval gate, action + knowledge-cited timing, resumed deferral -
DESIGN.md sections 6.6 and 9.2) and exits non-zero if any is missing.

Usage:
    python driver.py [host:port]        # default 127.0.0.1:8000

Needs the `websockets` package (a project dependency, so it is already in the venv).
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import websockets

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8000"
WS_URL = f"ws://{HOST}/channels/web_chat/ws"
HEALTH_URL = f"http://{HOST}/healthz"

# (message, label, substrings that must appear in the agent's reply, required
#  awaiting.kind after the turn - "question"/"confirm" for a gate that must be open,
#  or None for a turn that must leave no gate).
FLOW = [
    (
        "I got charged twice for the Pro Plan this month; can I have one of them back?",
        "identity gate first",
        ["six-digit code"],
        "question",
    ),
    (
        "The code is 581139. Also, can you change my address while we are at it?",
        "topic change deferred + refund proposed for approval",
        ["made a note", "refund", "29.00"],
        "confirm",
    ),
    (
        "Yes please, go ahead and refund it.",
        "refund executes, timing cited from a knowledge doc",
        ["refunded", "five to seven business days"],
        "question",  # confirm gate now closed; agent asks "anything else?" (node=anything_else)
    ),
    (
        "Yes please - it is 4 Elm Row, Edinburgh, EH7 4AH, United Kingdom.",
        "deferred address change resumes for approval",
        ["Elm Row", "address"],
        "confirm",
    ),
]

TURN_TIMEOUT = 30.0  # a replay turn settles in well under this


def check_health() -> None:
    with urllib.request.urlopen(HEALTH_URL, timeout=10) as resp:
        body = json.loads(resp.read())
    ok = body.get("status") == "ok" and body.get("database") == "ok"
    mark = "PASS" if ok else "FAIL"
    print(
        f"[{mark}] /healthz status={body.get('status')} database={body.get('database')} "
        f"provider={body.get('provider')} pack={body.get('pack', {}).get('id')}"
    )
    if not ok:
        raise SystemExit(f"health check failed: {body}")


async def run_turn(
    ws: websockets.WebSocketClientProtocol, text: str
) -> tuple[list[str], dict | None]:
    """Send one message; collect agent replies until the end-of-turn `turn` frame."""
    await ws.send(json.dumps({"type": "message", "text": text}))
    agent_lines: list[str] = []
    awaiting: dict | None = None
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=TURN_TIMEOUT)
        frame = json.loads(raw)
        kind = frame.get("type")
        if kind == "message" and frame.get("author") == "agent":
            agent_lines.append(frame.get("text", ""))
        elif kind == "turn":
            awaiting = frame.get("awaiting")
            return agent_lines, awaiting
        elif kind == "error":
            raise SystemExit(f"server error frame: {frame.get('detail')}")


async def main() -> int:
    check_health()
    failures = 0
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"type": "hello"}))  # fresh conversation
        ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=TURN_TIMEOUT))
        assert ready.get("type") == "ready", ready
        print(
            f"[ready] session={ready.get('session')} provider={ready.get('provider')} "
            f"pack={ready.get('pack')}"
        )
        print()

        for text, label, needles, want_kind in FLOW:
            print(f">>> {text}")
            lines, awaiting = await run_turn(ws, text)
            reply = " ".join(lines).replace("\n", " ")
            for line in lines:
                print(f"    agent: {line.strip()}")
            got_kind = awaiting.get("kind") if awaiting else None
            node = awaiting.get("node") if awaiting else None
            print(f"    [awaiting={got_kind}{f' node={node}' if node else ''}]")

            problems = [n for n in needles if n.lower() not in reply.lower()]
            if got_kind != want_kind:
                problems.append(f"expected awaiting.kind={want_kind!r}, got {got_kind!r}")

            if problems:
                failures += 1
                print(f"[FAIL] {label}: {'; '.join(problems)}")
            else:
                print(f"[PASS] {label}")
            print()

    print("=" * 60)
    if failures:
        print(f"RESULT: {failures} check(s) FAILED")
        return 1
    print("RESULT: all checks PASSED - the demo flow runs end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
