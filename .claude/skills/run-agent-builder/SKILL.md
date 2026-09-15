---
name: run-agent-builder
description: >-
  Build, launch, and drive the support-core web chat demo (the agent-builder
  repo). Use when asked to run, start, serve, launch, smoke-test, screenshot,
  or drive the app / the demo / support-core / the Acme billing support agent,
  or to bring up its uvicorn server and Postgres + Qdrant stores.
---

# Run: support-core web chat demo

`app.py` is a FastAPI service (`create_app`) served by **uvicorn**, backed by
**Postgres + pgvector** and **Qdrant**. The demo (`demo/acme_web_chat.json`) uses
the **replay** provider (recorded cassettes), so no model key and no network are
needed — the four-message refund flow is deterministic. There are **two web
transports over the same conversation**:

- the original **WebSocket** web-chat channel at `/channels/web_chat/ws` (page `/`);
- an **AG-UI** SSE endpoint at `POST /channels/ag_ui` (page `/agui`) that translates
  the same committed messages and approval gate into AG-UI protocol events.

**Drive it with the committed driver**, not by eyeballing a browser:
[`.claude/skills/run-agent-builder/driver.py`](.claude/skills/run-agent-builder/driver.py)
speaks the same WebSocket protocol as the browser client and asserts the four
behaviours the demo exists to show. It exits 0 on success.

> **This machine is Windows, and the app cannot run natively here** — the code
> uses Python 3.12 PEP 695 syntax (`support_core/llm/service.py`), which the only
> Windows Python (3.11) *cannot even parse*. The verified path runs the app inside
> **WSL Ubuntu-24.04** (Python 3.12) against **Docker Desktop**'s containers.
> On a native Linux/CI box, drop the `wsl -d Ubuntu-24.04 -u root --` prefix from
> every command and run `scripts/db-up.sh` directly.
>
> All paths below are relative to the repo root (`D:\workspace\agent-builder`).

## Prerequisites (one-time)

Docker Desktop installed and **running** (it hosts Postgres + Qdrant). Then the
WSL runtime:

```bash
wsl --install -d Ubuntu-24.04 --no-launch
wsl -d Ubuntu-24.04 -u root -- bash -lc 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12-venv python3-pip'
```

## Build (one-time)

The venv MUST be a **Linux** venv built inside WSL (a Windows `.venv` can't run the
code). `.venv` is gitignored. Installing onto the mounted `D:` drive is slow (many
small-file writes) — allow several minutes.

```bash
wsl -d Ubuntu-24.04 -u root -- bash -lc 'cd /mnt/d/workspace/agent-builder && rm -rf .venv && python3 -m venv .venv && ./.venv/bin/python -m pip install -U pip -q && ./.venv/bin/python -m pip install -e ".[dev]"'
```

## Bring up the stores + schema (one-time per boot)

Docker Desktop is Windows-side, so run `db-up.sh` from **Git Bash on Windows** (not
WSL); the containers publish to the Windows host and WSL reaches them via
`localhost` forwarding. Migrations and the knowledge sync run in WSL:

```bash
sh scripts/db-up.sh
```

```bash
wsl -d Ubuntu-24.04 -u root -- bash -lc 'cd /mnt/d/workspace/agent-builder && ./.venv/bin/python -m alembic upgrade head'
```

The knowledge sync is **not optional** — two refund steps answer from the pack's
policy docs, and the citation guardrail hands off if they are unindexed:

```bash
wsl -d Ubuntu-24.04 -u root -- bash -lc 'cd /mnt/d/workspace/agent-builder && ./.venv/bin/support pack knowledge sync packs/acme_billing'
```

## Run (agent path) — the driver

**Launch uvicorn as a long-lived background task** (hold the command open with
`exec`; do NOT `nohup ... &` inside a one-shot `wsl bash -lc` — WSL reaps it when
the session ends). Bind `0.0.0.0` so a Windows browser can reach it too:

```bash
wsl -d Ubuntu-24.04 -u root -- bash -lc 'cd /mnt/d/workspace/agent-builder && SUPPORT_APP_CONFIG=demo/acme_web_chat.json exec ./.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000 2>&1'
```

Wait for health, then drive it. **Run the driver against a freshly started server**
(see Gotchas — the billing fake is stateful):

```bash
curl -sS http://127.0.0.1:8000/healthz
```

```bash
wsl -d Ubuntu-24.04 -u root -- bash -lc 'cd /mnt/d/workspace/agent-builder && ./.venv/bin/python .claude/skills/run-agent-builder/driver.py'
```

Expected tail:

```
[PASS] identity gate first
[PASS] topic change deferred + refund proposed for approval
[PASS] refund executes, timing cited from a knowledge doc
[PASS] deferred address change resumes for approval
RESULT: all checks PASSED - the demo flow runs end to end
```

Point the driver elsewhere with an arg: `driver.py <host:port>`.

### AG-UI transport (SSE)

A second driver checks the AG-UI endpoint, driving the interrupt flow so **both**
approval gates show as AG-UI tool calls (`issue_refund`, `set_address`). Also run it
against a **freshly started** server:

```bash
wsl -d Ubuntu-24.04 -u root -- bash -lc 'cd /mnt/d/workspace/agent-builder && ./.venv/bin/python .claude/skills/run-agent-builder/agui_driver.py'
```

Expected tail:

```
[PASS] identity gate first
[PASS] topic deferred + refund proposed as a tool call
[PASS] refund executes, timing cited from a knowledge doc
[PASS] deferred address change resumes as a tool call
RESULT: all checks PASSED - the AG-UI flow runs end to end
```

The endpoint by hand: `GET /channels/ag_ui` returns provider/pack/suggestions;
`POST /channels/ag_ui` with `{"threadId","messages":[{"role":"user","content":"…"}]}`
streams `text/event-stream` AG-UI events (RUN_STARTED, TEXT_MESSAGE_*, TOOL_CALL_*
for a confirm gate, STATE_SNAPSHOT, RUN_FINISHED).

## Run (human path) — the browser

Three pages: <http://127.0.0.1:8000/> (built-in WebSocket UI),
<http://127.0.0.1:8000/agui> (built-in AG-UI SSE UI), and
<http://127.0.0.1:8000/app/> (the **pack's own** front end, shipped in
`packs/acme_billing/ui/` and served when it exists). Windows `localhost:8000` reaches
the WSL server. Click the suggested messages in order; watch the bordered "Your
approval is needed" panel gate the refund. Useful for a look; the drivers are what
prove it works.

## Test

```bash
wsl -d Ubuntu-24.04 -u root -- bash -lc 'cd /mnt/d/workspace/agent-builder && ./.venv/bin/python -m pytest -q'
```

(Database tests run against the `support_test` DB that `db-up.sh` creates. The
`live` group is opt-in — `pytest -m live` needs `ANTHROPIC_API_KEY`.)

## Stop

```bash
wsl -d Ubuntu-24.04 -u root -- pkill -f "uvicorn app:app"
```

```bash
sh scripts/db-down.sh
```

## Gotchas (the battle scars)

- **Python 3.12+ is a hard wall, not a preference.** `support_core/llm/service.py`
  uses PEP 695 (`def _validate[ModelT: BaseModel](...)`). Windows Python 3.11 dies
  with `SyntaxError: expected '('` on *import*, before uvicorn can load the app.
  Relaxing `requires-python` lets pip install but does not help — it is a parser
  error. Use a real 3.12 (WSL Ubuntu-24.04 ships 3.12.3).
- **The sample billing system is an in-memory, process-global fake**
  (`packs/acme_billing/tools`). Issuing the refund mutates it: a *second*
  conversation for the same customer then finds nothing to refund and **hands off
  to a human** instead of proposing a refund. Always **restart uvicorn before a
  clean demo run / driver run**. (This is exactly what made the driver "fail" the
  first time — the server had already refunded Sam from an earlier browser run.)
- **Docker daemon is Windows-side.** After starting Docker Desktop, the Linux
  engine pipe (`\\.\pipe\dockerDesktopLinuxEngine`) can take a minute-plus to
  appear even though the backend process is up. Poll `docker info` until it
  answers before `db-up.sh`.
- **Run the server as a held-open background process.** `nohup uvicorn ... &`
  inside `wsl bash -lc '...'` is reaped when that shell exits (the server vanishes,
  exit 15). Keep the `exec uvicorn` command itself alive as the background task.
- **WSL reaches the containers via `localhost`.** WSL2 localhost-forwarding makes
  `localhost:5432` / `:6333` (published by Docker Desktop to the Windows host)
  reachable from inside Ubuntu — verified. No `SUPPORT_DATABASE_URL` override needed.
- **Browser screenshots can time out** when Claude's window is behind another
  window ("page did not finish rendering"). The WebSocket driver is the reliable
  harness; for a browser look, fall back to reading the DOM (`get_page_text`).
- **WebSocket protocol** (see `support_core/api/static/app.js`): first frame is
  `{"type":"hello"}` (session key travels in the frame, never the query string —
  it would leak into access logs); then `{"type":"message","text":...}`. Replies
  are `message` frames (`author` = agent/customer) and a closing `turn` frame whose
  `awaiting.kind` is `question` (agent asking), `confirm` (an **approval gate**), or
  `handoff` (parked to a human queue).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SyntaxError: expected '('` at `service.py` | You are on Python 3.11. Use WSL 3.12 (see Prerequisites). |
| Driver reports `awaiting=handoff` instead of the refund | The billing fake is already mutated. Restart uvicorn (Stop, then Run). |
| `db-up.sh`: cannot connect to Docker daemon | Docker Desktop not fully up. Poll `docker info` until it answers, then retry. |
| `curl localhost:8000` refused from Windows | Bind uvicorn to `--host 0.0.0.0` (not `127.0.0.1`) so WSL2 forwarding exposes it. |
| Server exits with code 15 right after launch | You `nohup`'d it inside a one-shot WSL shell. Launch it as a held-open task instead. |
