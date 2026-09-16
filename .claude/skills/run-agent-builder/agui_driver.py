#!/usr/bin/env python3
"""Drive the running support-core AG-UI endpoint and check its event stream.

The companion to driver.py (which drives the WebSocket). This one speaks the AG-UI
protocol over SSE against ``POST /channels/ag_ui``: each run POSTs one user message
and reads a ``text/event-stream`` of AG-UI events (RUN_STARTED, TEXT_MESSAGE_*,
TOOL_CALL_* for the approval gate, STATE_SNAPSHOT, RUN_FINISHED).

It runs the demo's interrupt flow so both approval gates show as AG-UI tool calls:
identity -> (topic deferred + refund proposed as a TOOL_CALL for issue_refund) ->
refund done -> (deferred address change resumed as a TOOL_CALL for set_address).

Like driver.py this must run against a FRESHLY STARTED server: the sample pack's
billing system is an in-memory, process-global fake, so a second run for the same
customer would find nothing to refund and hand off instead.

Usage:
    python agui_driver.py [host:port]        # default 127.0.0.1:8000

Standard library only - no venv package needed.
"""

from __future__ import annotations

import json
import sys
import urllib.request
import uuid

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8000"
BASE = f"http://{HOST}"
THREAD = f"agui-drive-{uuid.uuid4().hex[:8]}"

# (message, label, substrings that must appear in the assistant text, tool the confirm
#  gate must name after the turn - or None for a turn that leaves no tool call).
FLOW = [
    (
        "I got charged twice for the Pro Plan this month; can I have one of them back?",
        "identity gate first",
        ["six-digit code"],
        None,
    ),
    (
        "The code is 581139. Also, can you change my address while we are at it?",
        "topic deferred + refund proposed as a tool call",
        ["made a note", "29.00"],
        "issue_refund",
    ),
    (
        "Yes please, go ahead and refund it.",
        "refund executes, timing cited from a knowledge doc",
        ["refunded", "five to seven business days"],
        None,
    ),
    (
        "Yes please - it is 4 Elm Row, Edinburgh, EH7 4AH, United Kingdom.",
        "deferred address change resumes as a tool call",
        ["Elm Row"],
        "set_address",
    ),
]


def get_info() -> dict:
    with urllib.request.urlopen(f"{BASE}/channels/ag_ui", timeout=10) as resp:
        return json.loads(resp.read())


def run(text: str) -> list[dict]:
    """POST one AG-UI run and return the list of parsed AG-UI events."""
    body = json.dumps(
        {
            "threadId": THREAD,
            "runId": f"run_{uuid.uuid4().hex}",
            "messages": [{"id": uuid.uuid4().hex, "role": "user", "content": text}],
        }
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/channels/ag_ui",
        data=body,
        headers={"content-type": "application/json", "accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def said(events: list[dict]) -> str:
    return " ".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")


def tools_called(events: list[dict]) -> list[str]:
    return [e["toolCallName"] for e in events if e["type"] == "TOOL_CALL_START"]


def main() -> int:
    info = get_info()
    print(
        f"[info] provider={info.get('provider')} pack={info.get('pack')} "
        f"suggestions={len(info.get('suggestions', []))}"
    )
    print(f"[thread] {THREAD}\n")

    failures = 0
    for text, label, needles, want_tool in FLOW:
        print(f">>> {text}")
        events = run(text)
        reply = said(events)
        for line in reply.split("\n"):
            if line.strip():
                print(f"    agent: {line.strip()}")
        called = tools_called(events)
        print(f"    [tool_calls={called or 'none'}]")

        problems = [n for n in needles if n.lower() not in reply.lower()]
        if events[0].get("type") != "RUN_STARTED" or events[-1].get("type") != "RUN_FINISHED":
            problems.append("run was not framed by RUN_STARTED/RUN_FINISHED")
        if want_tool and want_tool not in called:
            problems.append(f"expected a tool call for {want_tool!r}, got {called}")
        if not want_tool and called:
            problems.append(f"unexpected tool call(s): {called}")

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
    print("RESULT: all checks PASSED - the AG-UI flow runs end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
