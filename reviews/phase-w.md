# Phase W review: web chat slice (pulled forward from phase 7)

Design: DESIGN.md sections 12 (channels), 4.1 (deployment topology), 7.1 to 7.3 (turn loop,
suspension, failure handling). Backlog: the Phase W checklist and its exit criterion.

## Plan

Written before any code, per PLAN.md step 1.

### What this phase is for

Someone opens a browser, types a refund request at `packs/acme_billing`, and watches the
conversation work - including the identity check and the confirmation before money moves. The
code that does it has to be the code phase 7 extends: the same `ChannelAdapter` protocol its
email adapter implements, the same `create_app` its desk API and email webhook are added to, and
the same queue-and-return inbound path (phase 2 review finding R7, customer half).

### Task breakdown

1. **A durable channel key on the conversation** (migration `0007`). DESIGN.md 12 identifies a
   conversation by "thread id, session id" - a value the *channel* owns, not a database id and
   never a connection. `conversation.channel_key` plus a unique `(channel, channel_key)` index is
   what makes a web chat reconnect and an email thread the same mechanism, and it is what stops
   two simultaneous first messages creating two conversations. `Executor.start_conversation`
   grows a `channel_key` keyword; nothing else in the engine changes.

2. **`support_core/channels/`**: the protocol of DESIGN.md section 12, exactly its three
   methods (`parse_inbound`, `send`, `conversation_key`), plus the value types they exchange
   (`InboundMessage`, `ConversationRef`) and a `ChannelHub` that resolves a conversation from an
   inbound message and binds `EngineHooks.send` to whichever adapter owns the conversation's
   channel.

3. **`support_core/channels/web_chat.py`**: the WebSocket adapter and its connection registry. A
   connection registers under a *conversation id* and is unregistered when it closes; a message
   goes to every live connection for that conversation and to none if there are none, because
   the rows are durable and a reconnecting client is sent the transcript.

4. **`support_core/api/`**: `create_app(pack)` (DESIGN.md 4.1), `GET /healthz`,
   `POST /channels/web_chat/messages` (the webhook shape, queue-and-return),
   `WS /channels/web_chat/ws`, the static client, and the drain worker that makes
   `lock_wait_seconds=0` safe. `AppConfig` chooses the provider (replay cassettes, live
   Anthropic, or none) by configuration rather than by a code change.

5. **The client page**: `support_core/api/static/`, plain HTML, CSS and JavaScript, no build
   step, no framework, no external network dependency. It must make the confirmation step
   obvious, because that is the moment the design is really demonstrating.

6. **A root `app.py`** exactly as DESIGN.md 4.1 writes it (`app = create_app(load_pack(...))`),
   and `demo/acme_web_chat.json`, the configuration that points the app at the recorded
   provider.

7. **Tests**: web chat round trip against the sample pack through the real HTTP and WebSocket
   path; a suspend and resume across two WebSocket connections; concurrent clients that do not
   serialise; a second caller that returns promptly instead of holding a connection while
   another turn holds the lock; the adapter protocol's generality.

### Decisions made up front, and why

- **The protocol keeps DESIGN.md's three methods.** Phase 7's email adapter has to implement it
  unchanged, so nothing web-chat-shaped may leak into it: no connection, no socket, no session.
  `send` therefore takes a `ConversationRef` - an immutable snapshot of the conversation row -
  rather than the ORM object DESIGN.md's signature names, because an adapter that holds a live
  ORM instance can write through it, and because the object has to survive being handed to a
  transport that may use it after the session closed. This is the one deliberate change to the
  written signature and it is recorded here and in the decisions log.

- **`lock_wait_seconds=0` is the default for the HTTP path, with a drain worker behind it.**
  Phase 2's review measured four of five callers blocked for a whole turn, each holding a
  connection; phase 3 made turns seconds long. The mechanism already exists (`queued=True` and
  `Executor.drain`); what was missing is a caller. The drain worker is that caller. The
  scheduler for `recover_stalled` and `sweep_timeouts` stays in phase 7.

- **Streaming is the final message only, and only after it commits.** No node text is sent
  before the checkpoint that wrote it, so nothing reaches a customer that a later guardrail
  would have stopped. There is no token streaming from the model at all, and there must not be
  one until guardrails run before send (phase 7).

- **A browser may not describe the customer.** The inbound payload carries a session key and
  text and nothing else; the `ConversationContext` a new conversation starts with comes from
  server-side configuration. A page that could post `customer.ref` or `identity_verified` would
  be an identity-spoofing hole with a friendly interface.

- **`send` must not be able to abort a turn.** Phase 2's self-critique names a raising `send`
  hook as one of two ways delivery can take a completed turn down with it. A closed socket is
  the ordinary case in a browser, so the hub catches everything a transport raises and reports
  it out of band.

### Intended deviations from DESIGN.md, and why

1. **`ChannelAdapter.send` receives a `ConversationRef`, not the `Conversation` row** (above).
2. **The client page is served by core**, not by the pack repository. DESIGN.md 4.1 gives the
   pack repo an `app.py` and nothing else; a demo client has to live somewhere and core is where
   the channel is. It is served only when `AppConfig.serve_client` is on.
3. **`AppConfig` is a JSON file plus environment overrides.** DESIGN.md 20 says secrets come
   from the environment, which is kept; but "which provider, which cassettes, what a new
   conversation knows" is deployment configuration and a file is what makes the demo one command
   rather than six exported variables.

### What this phase deliberately does not build

The desk API, the email adapter, tracing, metrics, the replay endpoint, guardrails and the
scheduler are phase 7. Retrieval is phase 5. The interrupt check and the handoff node are
phase 6. Nothing here may pre-empt them; the seams they need are named where they are left.

---

## Implementation notes

### Shape of the code

```
support_core/channels/
  base.py        the protocol (three methods), InboundMessage, ConversationRef
  hub.py         conversation-from-key, and the binding of EngineHooks.send to an adapter
  web_chat.py    the WebSocket adapter, the connection registry, what a client is told
support_core/api/
  config.py      AppConfig: which pack, which provider, what a new conversation knows
  runtime.py     builds provider, LLM layer, executor, hub, drain worker; accept() and state()
  drain.py       DrainQueue: the caller of Executor.drain that makes lock_wait_seconds=0 safe
  app.py         create_app: /healthz, the web chat webhook, the WebSocket, the client page
  static/        index.html, app.css, app.js
app.py                      DESIGN.md 4.1's file: app = create_app(load_pack(config.pack))
demo/acme_web_chat.json     the demo's configuration
support_core/storage/migrations/versions/0007_channel_key.py
```

Four things carry most of the weight.

**The conversation's name is a column, not a connection.** `conversation.channel_key`, unique
per channel among the rows that have one, is what a web chat session key and (phase 7) a mail
thread id both land in. Everything else follows from it: a reconnect finds the conversation, two
tabs share it, a `POST` and a socket are the same conversation, and two callers racing on one key
get one conversation because the index says so rather than because the code was lucky.

**Delivery cannot hurt a turn.** `ChannelHub.deliver` catches everything a transport raises,
because it runs inside the transaction that marks messages `sent`: phase 2's self-critique names
a raising `send` hook as one of two ways delivery can take a finished turn down with it, and a
closed browser tab is the ordinary case, not an exceptional one. A connection that raises is
dropped from the registry; the message it missed is in the transcript the next connection is
sent.

**Queue and return, with somebody to pick it up.** The HTTP path builds its executor with
`lock_wait_seconds=0`. A handler that cannot take the conversation lock answers `202` with
`queued: true` and submits the conversation id - not the message, which is already durable - to
`DrainQueue`, which calls `Executor.drain` until it is the one holding the lock or a budget runs
out. Two details that are not obvious: submissions coalesce per conversation, because `drain`
processes the whole pending queue in order, so ten messages are one thing to do; and a
submission that arrives *while* that conversation is being drained is remembered and re-queued,
because a row that commits after the last claim found nothing would otherwise wait for the next
message. Giving up is safe (the message stays `pending` and in order) and retrying for ever is
not, which is why there is a budget.

**A turn the worker ran still has to reach the browser.** The drain worker announces the
conversation's new state to whoever is watching it. Without that, a message that arrived during
somebody else's turn would produce agent messages on the socket but no status change, and the
client would never learn that the turn ended waiting for a confirmation.

### Where the plan changed while building

- **`create_app` grew a check that the pack enabled the channel.** DESIGN.md 12 says "packs
  enable channels in `pack.yaml`", and the first version served web chat whatever the manifest
  said. A pack that declares only email would have been served under timeouts and a persona
  written for somewhere else.
- **A configured `ConversationContext` is validated at startup, and may not set
  `identity_verified`.** The context a new conversation begins with is the one thing in this
  phase that could hand out a verified identity for free; refusing it in configuration is one
  line and closes it before a pack's gates ever see it.
- **No reset of the sample pack's fake billing system.** The pack exposes `reset_backend()` "so a
  demo or a test can start from the same account every time", and it was tempting to call it when
  a demo conversation opens. It is not the real thing: a real backend has no reset, and resetting
  a process-global fake underneath every other conversation is a demo-only shortcut. The README
  says to restart the app instead.
- **The client offers the recorded messages as buttons.** Replay is by prompt fingerprint, so an
  unrecorded message is a `CassetteMiss`, and a demo whose first typed sentence dead-ends is not
  a demo. The buttons come from configuration (`suggestions`), and a test asserts they are
  exactly the recorded conversation's turns, so the demo cannot drift from the cassettes.

### Running the demo by hand

Exactly what was done, on Windows 11 with Docker running, on 2026-09-06:

```sh
sh scripts/db-up.sh
SUPPORT_DATABASE_URL=postgresql+asyncpg://support:support@localhost:5432/support \
  .venv/Scripts/python.exe -m alembic upgrade head
SUPPORT_APP_CONFIG=demo/acme_web_chat.json .venv/Scripts/python.exe -m uvicorn app:app --port 8123
```

`GET /healthz` answered `{"status":"ok","pack":{"id":"acme-billing",...},"provider":"replay",
"channels":["web_chat"],"database":"ok"}`. The page was then opened at `http://127.0.0.1:8123/`
in a browser and the conversation driven by clicking the four suggested messages.

What was on the screen, in order:

1. The header showed `acme-billing`, `provider: replay`, a green connection dot and the status
   `idle`; the footer showed the session key the server had assigned
   (`7fb8b039ea6c69a97be96887`).
2. After *"I got charged twice for the Pro Plan this month; can I have one of them back?"*: the
   agent answered **"I have sent a six-digit code to me@example.com. What is it?"** and the
   status became `waiting_customer`. The identity check came first, before anything looked at
   the account - the gate doing its job, visibly.
3. After *"The code is 581139."*: the agent proposed **"I can refund 29.00 USD for Pro Plan -
   September (2026-09-03) to your original payment method. Shall I go ahead?"**, and the amber
   panel appeared: *"Your approval is needed - the assistant has proposed an action and stopped.
   Nothing happens to your money until you answer"*, `waiting to run issue_refund`, with the
   proposal quoted underneath it. Nothing moved.
4. The page was then **reloaded** mid-conversation. The transcript came back from the database,
   the note read "reconnected to this conversation", the status was still `waiting_customer` and
   the approval panel was still there - a different WebSocket connection, the same conversation,
   found by its session key.
5. After *"Yes please, go ahead and refund it."*: **"That is refunded. It takes five to seven
   business days to show on your statement."** followed by **"Is there anything else I can help
   you with?"**, and the approval panel disappeared.
6. After *"No, that is all. Thanks!"*: the status became `done`.

Afterwards, in the database: two conversations (this one and an earlier scripted run through the
same socket path), each with exactly **one** `issue_refund` tool call, and two `action_approval`
rows, both consumed. The pack's in-memory ledger held `issue_refund:ch_1002:29.0` once.

So: yes, the demo was run by hand through the real HTTP and WebSocket path, it reached the
confirmation step, and it completed the refund.

---

## Self-critique

PLAN.md step 3's four questions, answered against DESIGN.md 12, 4.1 and 7.1 to 7.3.

### What did I skip or simplify?

1. **There is no authentication anywhere.** A session key is an unguessable 96-bit token, and
   that is the *whole* of the access control: anyone who has one can read that conversation's
   transcript and speak into it. There is no signed cookie, no origin check on the WebSocket
   handshake, no CSRF defence on the webhook, no rate limit, and no webhook signature
   verification. For a demo on `127.0.0.1` that is honest; for anything else it is not, and it is
   phase 7's first job on this surface, because that phase adds a desk API where the same gap
   would be somebody else's customer data.
2. **SSE is not implemented.** DESIGN.md 12 says "WebSocket or SSE"; one of the two is enough for
   the phase and the second would share nothing with the first except the adapter.
3. **"Streaming of the final message only" is implemented as "the message is pushed the moment it
   commits", not as progressive text.** One frame per message, not a stream of chunks. That is a
   reading of the design, and it is the safe reading (see the third question below), but a reader
   expecting characters to appear one at a time will not get them.
4. **Live delivery is process-local.** The connection registry is a dictionary in one process, so
   two app processes sharing a database will each deliver only to the sockets they hold. Nothing
   is lost - the messages are rows and a reconnecting client is sent the transcript - but a
   customer whose turn was run by the other process sees nothing until they reconnect. DESIGN.md
   4.1's "horizontal scaling is safe" is true of *execution*; it is not yet true of *delivery*,
   and phase 7 needs a fan-out (Postgres `LISTEN/NOTIFY` is the obvious one, since the database
   is already there).
5. **One configured context for every conversation.** `new_conversation_context` is a constant. A
   real deployment looks the customer up when a conversation opens; there is no seam for that
   yet, deliberately, because the seam should be designed with the desk and the email adapter
   that also need it.
6. **The drain worker lives and dies with the process.** A message queued when the process is
   killed waits for the next inbound message on that conversation. The scheduler that would call
   `recover_stalled` and `sweep_timeouts` is phase 7's, as the backlog says, so this is the
   documented half of R7 and not a surprise - but it is a real hole in a demo left running.
7. **No pagination of history** (the newest 200 messages) and **no de-duplication of
   at-least-once redelivery** in the client.

### Where does the code diverge from the design?

1. **`ChannelAdapter.send` takes a `ConversationRef`, not the `Conversation` row.** Recorded in
   the plan, in the decisions log and in the module docstring. An adapter holding a live ORM
   object could write through it, and a transport that keeps it past the session that loaded it
   would touch a detached instance. Everything an adapter can legitimately need - id, channel,
   key, customer ref, status, stored context - is on the snapshot.
2. **`POST /channels/web_chat/messages` is not in DESIGN.md.** Section 12 gives web chat a socket
   and email a webhook. The endpoint exists because it is the shape phase 7's email webhook
   takes, because queue-and-return is visible and measurable through it, and because a channel
   that can only be driven by a socket cannot be driven by a script. It is the same
   `AppRuntime.accept` call the socket makes.
3. **The demo client is served by core.** DESIGN.md 4.1 gives the pack repository an `app.py` and
   nothing else. `serve_client: false` turns it off.
4. **`AppConfig` is a JSON file plus environment overrides**, where DESIGN.md 20 says only that
   secrets come from the environment. Secrets still do: the database URL and the Anthropic key
   are read where they always were.

### Which tests are weak?

1. **Two of them are stopwatches.** `test_a_second_message_returns_promptly_while_a_turn_holds_the_lock`
   asserts a handler returned in under 0.75 s while a turn held the lock for 1.5 s, and the
   no-serialising test asserts five conversations finished in less than four times a slow turn.
   The margins are wide, but a heavily loaded CI machine could still make them flake, and a flaky
   timing test is worse than none because it teaches people to re-run. I could not think of a
   non-timing way to state "did not wait for the lock" that would still fail against a blocking
   handler.
2. **The client JavaScript has no tests at all.** There is no JS runner in this repository and
   adding one is not this phase's business, so `app.js` is covered by the manual demo above and
   by nothing else. What the *server* sends it is well covered; what it does with that is not.
3. **Nothing kills the server mid-turn.** Phase 2 proves the engine recovers, with real
   `os._exit` children. The channel layer's part - a socket that disappears mid-turn, a process
   that dies with a message queued in the drain worker, a client that reconnects into a run left
   `running` - is argued from the engine's guarantees rather than demonstrated.
4. **No test runs two app processes against one database.** The registry's process-locality
   (skipped item 4) is therefore stated in this critique rather than pinned by a test that would
   fail if somebody "fixed" it by broadcasting more widely.
5. **The live provider is never exercised**, so "the same flow runs against the live model" is
   supported only by the provider-resolution test. That is the phase-3 gap, inherited.
6. **The refund conversation is asserted through the channel exactly once.** The four-message
   script is the recorded one; a customer who says something else is covered only by the
   protocol-level refusal tests, not end to end.

### What would break under concurrency or a crash mid-step?

What holds, and why:

- **Two sockets on one conversation cannot both run a turn.** The advisory lock is unchanged and
  the HTTP layer takes it with `wait_seconds=0`; the loser's message is durable, `pending` and in
  order, and either the winner's drain picks it up or the drain worker does. This is phase 2's
  property, not a new one, and the HTTP layer's only new obligation - to have somebody call
  `drain` - is the worker.
- **A conversation is never duplicated by a race on its key**, because the unique index decides
  and the loser reads the winner's row (tested with five simultaneous callers).
- **A socket dying mid-turn does not affect the turn**, because the turn is durable state and the
  socket is a delivery convenience. The messages are written by the checkpoint whether or not
  anybody is listening.

What I know is fragile:

1. **A crash between the checkpoint commit and delivery re-offers every pending row**, so a
   client that stays connected across it sees the messages of that turn twice. `hooks.send` gets
   text and no message identity, so a transport cannot de-duplicate even if it wanted to;
   `InboundMessage.external_id` exists for the inbound direction and there is no outbound twin.
   Phase 7's email adapter will feel this harder than a browser does - a duplicated email is a
   customer complaint, a duplicated chat bubble is a shrug.
2. **A turn cancelled by shutdown** (uvicorn cancels the handler task) leaves the run `running`
   and recoverable, exactly as a crash does - but with nothing scheduled to recover it until
   somebody sends another message on that conversation.
3. **The drain worker's budget can expire** while another process legitimately holds the lock for
   a long turn. The message is not lost, but it waits for an event that phase W does not
   schedule.
4. **`announce` reads state in a second transaction after the drain**, so two turns finishing
   close together could tell a client about them out of order. It affects a status pill, not
   durable state.
5. **`AppRuntime.state` is three queries in one transaction and is called on every turn and every
   connection.** It is not cached and it re-reads up to 200 messages each time; fine for a demo,
   not fine at volume.
6. **The whole turn runs in the WebSocket handler's task.** A client that sends a second message
   while its own turn is running has it buffered in the socket rather than queued in the engine
   until the handler comes back round. Ordering is preserved and nothing is lost, but a burst on
   one socket is serialised by the reader rather than by the lock.

### Where email will strain this protocol

The phase was asked to design against email even though it implements web chat. Where I think it
will hurt, in order:

1. **Per-turn batching has no turn boundary in the engine.** DESIGN.md 12 says outbound email is
   "batched per turn into one email". The engine flushes outbound after *every* checkpoint that
   produced a message, so an adapter is handed messages one at a time, several times per turn,
   and nothing tells it that a turn ended. The refund conversation produces two messages from two
   nodes in one turn - that is two emails today. The boundary is knowable *outside* the engine
   (`AppRuntime.accept` returns when the turn does), so an email adapter can buffer and flush
   where the runtime says so, but that is a second mechanism beside the protocol rather than part
   of it. If phase 7 would rather have it in the protocol, the honest change is a fourth method
   (`flush(conversation)`), and this phase deliberately did not add one speculatively.
2. **`send` is called inside the transaction that marks the message `sent`.** A buffering adapter
   is therefore recorded as having delivered a message it has only queued, and an SMTP failure
   after the buffer flushes is invisible to the database. Web chat gets away with it because its
   "delivery" is a push to a socket; email will not.
3. **No outbound message identity** (fragility item 1 above). At-least-once redelivery of an
   email is a duplicated email.
4. **`ConversationRef.context` is where the customer's address lives**, which is an awkward place
   for it: it is `ctx` as JSON, so an email adapter reads `context["customer"]["email"]` with no
   schema. `customer_ref` is right there and typed; the address is not, because
   `ConversationContext` is the pack's view of the customer rather than the transport's.
5. **`conversation_key` is a pure function of one payload**, which suits `In-Reply-To` but not
   the case where a customer replies to an *old* thread the provider gives a new id: resolving
   that needs a lookup, not a function. The protocol makes that the adapter's problem, and the
   adapter has no database handle. Phase 7 will either give it one or resolve the thread before
   calling the adapter.
6. **Days-long gaps are fine**, which is the part that came out well: nothing an adapter needs is
   in memory, the conversation is a row found by a key, and the engine's suspension already has
   per-channel timeouts.

### What I fixed while writing this

Three items started here as findings and were fixed rather than recorded: `create_app` served a
channel the pack had not enabled, `send_failures` grew without bound in a long-running process,
and the "needs no network" test read only the HTML and not the JavaScript and CSS it loads.

## Verification run (completed by the orchestrator)

The implementing agent was interrupted by a session rate limit while starting its verification
run, after writing the plan, the implementation and the self-critique. The orchestrator ran the
verification and committed the phase. No source was changed at this step; only this section and
the BACKLOG status cell were added.

Commands run from the repository root with `.venv/Scripts/python.exe`, on 2026-09-06, against
the tree as phase W was committed. **These numbers are of that date and no other**: phase 6
landed on top of phase W, so a reader checking them against a later tree finds 1361 tests rather
than 1259, 18 pack warnings rather than 9, and migration head 0008 rather than 0007. None of the
three is a regression; all three are phase 6 (review finding W13). The resolution section at the
end of this file carries the numbers for the tree as it stands.

| Command | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 144 files already formatted |
| `mypy` (strict) | Success: no issues found in 144 source files |
| `pytest -q` | 1259 passed, 2 deselected, 408s |
| `pytest -m live -q` | 2 skipped (no `ANTHROPIC_API_KEY`), 1259 deselected |
| `pytest tests/verify_phase_2_resolution.py -q` | 39 passed |
| `support pack validate packs/acme_billing` | well-formed, 9 warnings, exit 0 |
| `alembic downgrade base` → `upgrade head` → `check` | clean; head is 0007, "No new upgrade operations detected" |

### The demo, driven by hand

Started with `SUPPORT_APP_CONFIG=demo/acme_web_chat.json python -m uvicorn app:app --port 8000`.
`GET /` served the client page (200). `GET /healthz` returned 200 with
`provider: "replay"`, `channels: ["web_chat"]`, `database: "ok"` and the pack fingerprint.

> **Corrected at resolution (review finding W12).** This paragraph used to end: "Note the
> README's own instructions say `/health`; the endpoint is `/healthz` (recorded as a finding for
> the review)." That was not true and is struck rather than left standing. The README said
> `/healthz` when phase W was committed (`git show 032fb6a:README.md`) and says it now; there was
> no defect, and nothing was fixed for one. The reviewer checked and could not reproduce it,
> which is the only reason it is not still in the record as a defect this project believed it
> had.

A script then drove the four recorded messages over a real websocket to
`/channels/web_chat/ws`, in one connection, against the running server:

1. *"I got charged twice for the Pro Plan this month; can I have one of them back?"* →
   **"I have sent a six-digit code to me@example.com. What is it?"**, suspended
   `waiting_customer` on `ask_code`. The identity gate fired before the account was read, which
   is the behaviour DESIGN.md section 6.4 asks for.
2. *"The code is 581139."* → **"I can refund 29.00 USD for Pro Plan - September (2026-09-03) to
   your original payment method. Shall I go ahead?"**, suspended with
   `awaiting: {kind: "confirm", node: "confirm_refund", tool: "issue_refund"}`. The proposal
   names the tool the approval is bound to, and nothing ran.
3. *"Yes please, go ahead and refund it."* → **"That is refunded. It takes five to seven business
   days to show on your statement."**, then *"Is there anything else I can help you with?"*
4. *"No, that is all. Thanks!"* → status `done`.

The phase W exit criterion holds: `create_app(load_pack("packs/acme_billing"))` starts and a
websocket client completes the phase-4 refund flow end to end, including the confirmation step,
against the recorded provider.

---

## Independent review

Reviewer: a separate agent that did not write this code. PLAN.md step 4, against DESIGN.md 4.1,
7.1 to 7.3, 12 and 14, the Phase W backlog checklist and its exit criterion, and the current
state of the tree (`c589c70`), not the state at which phase W was committed.

**Verdict.** The core of the phase is sound and the exit criterion holds independently: I started
`app:app` on a free port against a database nothing else was using, drove the four-message refund
conversation over a real WebSocket, and got the identity gate, the confirmation panel's
`awaiting: {kind: confirm, node: confirm_refund, tool: issue_refund}`, the refund, and `done` -
with exactly one `action_approval` row, bound to run, frame, confirm node and step, its arguments
hashed and consumed by exactly one `tool_call`. The channel boundary itself held every attack I
made on it: a forged key opens a new conversation rather than reaching one, a frame that carries
somebody else's `session` is overridden by the connection's, `extra="forbid"` refuses
`customer_ref` and `identity_verified`, the length cap holds, a socket that dies mid-turn does not
disturb the turn, twelve simultaneous callers on one key get one conversation, and nothing the
socket sends carries an approval hash, tool arguments, prompt text or a stack trace. The
queue-and-return path is real: twelve concurrent messages on one conversation returned in 0.43 s
total with eleven `202 queued`, and six conversations at once did not serialise. Every command in
the orchestrator's verification section reproduces. What stops this being a clean pass is not the
channel but what has been mounted beside it: `create_app` serves phase 6's **desk API on the same
port as the customer chat, with no authentication and `serve_desk` defaulting to on**, so any
browser that can open the demo page can list every conversation in the deployment, read another
customer's whole transcript and handoff packet, and post into it - no session key needed. That is
a live exposure on the surface being demoed, and it is not covered by the deferred phase-W auth
finding, whose stated premise ("phase 7 ... adds a desk API where the same gap would expose
somebody else's conversation") was overtaken when phase 6 shipped that desk early and on by
default. Two smaller must-fixes and nine should-fixes follow. The verification section itself is
honest apart from one phantom finding: the README never said `/health`.

### Findings

| id | severity | location | finding | suggested fix |
|----|----------|----------|---------|---------------|
| W1 | must-fix | `support_core/api/config.py:114`, `support_core/api/app.py:145-147` | The desk API is mounted on the customer-facing app, on the same port and origin, unauthenticated, and `serve_desk` defaults to `True`. Reproduced against a running server with no credentials: `GET /desk/handoffs` enumerated every conversation in the database (conversation id, run id, queue, reason, SLA); `GET /desk/handoffs/{id}` returned another conversation's full packet including its internal failure detail, prompt hash and filesystem path; `GET /desk/conversations/{id}/transcript` returned another customer's entire transcript. `POST /desk/handoffs/{id}/{reply,resume,close}` writes into another conversation, and `POST .../approve` is the human half of `requires_human_approval` - so a customer can supply both signatures on their own action, which is the one property a second signature exists to have. | Default `serve_desk` to `False`, and require a bearer token or mTLS on the `/desk` router before it can be turned on. Better: give the desk its own `create_desk_app` on its own port, so "the same API surface" (DESIGN.md 12) does not mean the same listener. |
| W2 | must-fix | `support_core/api/app.py:258` | A binary WebSocket frame where text is expected raises an unhandled `KeyError: 'text'` out of `socket.receive_text()`. The connection dies abnormally and Starlette logs a full ASGI traceback; an anonymous client can produce one per frame. The registry is cleaned up by the `finally`, so nothing is corrupted, but this is an uncaught exception on the phase's public entry point and no test sends a non-text frame. | Use `await socket.receive()` and branch on `"text" in message`, answering a binary frame with an error frame and `close(1003)`. |
| W3 | should-fix | `support_core/api/app.py:292-297`, `support_core/api/runtime.py:190-204` | The `turn` frame - the only thing that carries `status` and `awaiting`, and therefore the only thing that raises the approval panel - is pushed to the *connection that sent the message*, and `announce` broadcasts only after a **drain**. Reproduced: with two sockets on one conversation, the sender got `message` + `turn`, the second got `message` only; the same holds for a turn run by the `POST` handler while a socket watches (which is what `test_a_message_posted_by_webhook_reaches_the_open_socket` asserts, without noticing). So a second tab, and any customer whose reply arrived on another transport, sees the refund proposal with no approval panel and a stale status pill - the exact moment the phase says it exists to demonstrate. | Broadcast the `turn` frame to the conversation (`connections.broadcast`) rather than pushing it to one connection; keep `queued` in the HTTP response, where it is per-caller, rather than in the frame. |
| W4 | should-fix | `support_core/storage/repositories.py:216-238`, `:552-562` | Two inbound messages accepted concurrently on one conversation can be processed in the reverse of the order they were accepted. The pending queue orders by `created_at` - the *transaction start* timestamp - and every inbound row carries `ordinal = 0`, so the tie-break is a random UUID. Reproduced: two `POST`s issued together, first "I got charged twice...", second "The code is 581139."; the second was written 149 microseconds earlier, ran first, dead-ended the conversation into an `llm_unavailable` handoff, and the first message is still `pending` and will stay so. A single socket is serial and preserved order over eight rapid messages, so this is specific to the multi-caller burst that queue-and-return exists for. | Give `message` a per-conversation monotonic sequence assigned inside `enqueue_inbound` and order the pending queue by it. |
| W5 | should-fix | `support_core/engine/executor.py:1952-1978`, `support_core/storage/repositories.py:552` | `deliver_pending` deliberately runs outside the conversation lock, and `pending_outbound` selects without `FOR UPDATE SKIP LOCKED`, so a desk `reply` concurrent with a turn's `_flush_outbound` reads the same `pending_send` rows in both transactions and delivers each message twice before either marks them `sent`. Web chat shows a duplicated bubble; phase 7's email adapter sends a duplicated email. | `SELECT ... FOR UPDATE SKIP LOCKED` in `pending_outbound`. |
| W6 | should-fix | `support_core/engine/executor.py` (the 7.3 failure path), surfaced at `support_core/api/app.py:292` | On the turn where a node fails, the customer is told nothing at all. Reproduced: a message on a `done` conversation produced an `llm_unavailable` handoff and `waiting_human`, with **zero** outbound messages - the socket carried only a `turn` frame whose `status` changed. The *next* message is answered correctly ("Thank you - I have added that to the conversation..."), so phase 6's P6 fix works; the failing turn itself is silent. A web chat client can render the status; an email customer would get pure silence after asking a question. | Say the same "it is with one of our people" line on the turn that raises the handoff, not only on the ones queued behind it. |
| W7 | should-fix | `support_core/channels/hub.py:100-112` | `conversation_for` returns `created=True` for every loser of a create race, because it only looks at whether *its own first read* missed. Reproduced: twelve concurrent `POST`s on one key produced one conversation and twelve responses saying `"created": true`. Harmless today (nothing branches on it) and exactly the flag phase 7's email adapter would branch on to send a greeting. | Catch the `IntegrityError` explicitly rather than with `suppress`, and set the flag from whether the insert actually committed. |
| W8 | should-fix | `support_core/api/runtime.py:241-252` | Opening a WebSocket with any well-formed session key creates a durable `conversation` **and** `run` row before the customer has said anything, from an unauthenticated endpoint, with no rate limit. A loop of connects fills the table. Phase 7's email adapter has the same shape for bounce and delivery-receipt webhooks. | Resolve without creating on connect: send `ready` with a null conversation for an unknown key and create on the first message. |
| W9 | should-fix | `support_core/api/app.py:236`, `support_core/api/static/app.js:70-73` | The session key is the whole of the access control (self-critique item 1) and it travels in the WebSocket **query string**, so it is written verbatim into the access log by uvicorn and would be into any proxy, CDN or APM log. Verified in the server log: `WebSocket /channels/web_chat/ws?session=ba7515fd7978306d7a29e0d4 [accepted]`. The deferred finding says "no auth"; it does not say the one secret is logged. | Accept the key in a `Sec-WebSocket-Protocol` value or in the first frame after `accept()`, and take it out of the URL. |
| W10 | should-fix | `support_core/storage/repositories.py:565-581`, `support_core/api/runtime.py:280-303` | `transcript` returns outbound rows regardless of `status`, so `AppRuntime.state` shows a reconnecting client every committed message including any left `pending_send`. Today that is right (delivery is the only thing that can fail). It stops being right the moment phase 7 puts DESIGN.md 14's outbound guardrail at send time: the reconnect path reads rows directly and would show the customer text the guardrail had refused to deliver. The phase's promise - "a customer must never see text the outbound guardrails would have stopped" - holds only for the push path. | Filter `transcript` to `status = 'sent'` for outbound rows now, so the guardrail seam is not silently bypassed later. |
| W11 | should-fix | `support_core/api/static/app.js:79`, `:95-102` | The client renders any author that is not `agent` as the **customer** - so a human's desk reply (`author = "human"`, which phase 6 deliberately made distinguishable "so an audit can tell the model's words from a human's") appears in the transcript styled and positioned as the customer's own message. It also does not update `state.lastAgentMessage`, so a confirmation panel raised after a desk reply quotes the wrong proposal. | Render three authors; use the last `agent` message for the panel, which it already tracks. |
| W12 | nit | this file, "Verification run" | The recorded finding "the README's own instructions say `/health`; the endpoint is `/healthz`" is not reproducible. `git show 032fb6a:README.md` contains exactly one `/health` mention and it is `GET /healthz`; the current README is the same. Nothing was ever wrong and nothing was fixed. | Strike it. |
| W13 | nit | this file, "Verification run" | The section's numbers are stale relative to the tree it is now read against: 1259 passed (now 1361), 9 pack warnings (now 18), migration head 0007 (now 0008). All three are phase 6 landing on top, not a regression, but a reader checking the phase against the repository finds three mismatches and no note saying why. | Date-stamp the table, or re-run it at resolution. |
| W14 | nit | `support_core/api/config.py:38` | `RESOLVED = ("replay", "anthropic", "none")` is unused anywhere in the tree and no longer lists `glm`, which `resolve_provider` can return. Dead and wrong. | Delete it, or derive it from `ProviderChoice`. |
| W15 | nit | `support_core/api/static/app.js:180-190` | A fatal error frame (`close(1008)` after a malformed `?session=`) is followed by the ordinary reconnect handler, which reconnects with the same bad key for ever, backing off to five seconds. `fatal` is sent by the server and ignored by the client. | Do not reconnect when the last frame said `fatal`. |
| W16 | nit | `support_core/channels/base.py:92` | `InboundMessage.metadata` is written by nobody and read by nobody in the whole tree. As a seam it points the wrong way - see "Missed by self-critique". | Either drop it or make it reachable at send time. |

### Channel boundary attempts

Every scenario below was run against a real uvicorn server on port 8100 with
`demo/acme_web_chat.json` and a private database, over real WebSocket and HTTP.

| # | attempt | result |
|---|---------|--------|
| A1 | Connect with a guessed/forged session key (`aaaaaaaaaaaaaaaa`) | **Held.** A new, empty conversation; no history, `status: idle`. No path to an existing one. (It did create a row: W8.) |
| A2 | Reconnect with another client's key | Full transcript returned, as designed - the key is a bearer token. Known, deferred; sharpened by W9 (the key is in the access log). |
| A3 | Two sockets on one session key | Same conversation for both; both got the `message` frames. Only the sender got the `turn` frame - **W3**. |
| A4 | Frame carrying somebody else's `session` (`{"type":"message","session":"<victim>","text":...}`) | **Held.** `_handle_frame` overwrites `payload["session"]` with the connection's; the turn landed on the attacker's own conversation. |
| A5 | Frames shaped like another message type: `{"type":"ready"}`, `customer_ref`, `identity_verified`, a JSON array, non-JSON, no `text`, whitespace-only `text` | **Held.** Each answered with a non-fatal `error` frame naming the rejected field, socket stayed open. No identity field is accepted from a browser. |
| A6 | Oversized frames: 4 001 and 400 000 characters | **Held.** Both refused with "at most 4000 characters". |
| A7 | Binary frame where text is expected | **Broke.** Unhandled `KeyError: 'text'`, ASGI traceback, abnormal close - **W2**. |
| A8 | Send after the conversation is `done` | Accepted, run went to `waiting_human` via a 7.3 handoff, and **no message was sent to the customer** - **W6**. No cross-conversation effect. |
| A9 | Reconnect to a conversation parked `waiting_human` by phase 6's handoff, and speak into it | **Held and correct.** Transcript returned; the message was queued and acknowledged once with "it is with one of our people", and the run did not resume. |
| A10 | Read every frame the socket sends for leakage (`ready`, `turn`, `message`, `error`) | **Held.** `awaiting` carries `kind`, `node`, `tool` and no `args_hash`; no tool arguments, no prompt text, no other customer's identifier, no stack trace, no internal paths. The only internal detail is the node id (`confirm_refund`, `classify`), which the client needs. |
| A11 | Cross-conversation `POST /channels/web_chat/messages` with another key and `Origin: https://evil.example` | Wrote into that conversation. Known (no auth, no origin check), deferred to phase 7. |
| A12 | Unauthenticated `/desk/*` from the customer origin | **Broke, badly.** Enumerated every conversation, read another customer's packet and transcript - **W1**. |
| A13 | The confirm step's "yes" over the socket, checked against the approval chain | **Held.** One `action_approval` row, `approved_by: customer`, bound to run + frame + `confirm_refund` + step id, arguments hashed, `consumed_by_tool_call_id` set; exactly one `issue_refund` `tool_call`. No route around 8.2 from this channel. |

### Concurrency attempts

| # | attempt | result |
|---|---------|--------|
| C1 | Twelve simultaneous `POST`s on one new session key | One conversation (unique index held), eleven `202 queued`, one `200`, **0.43 s wall for all twelve** - no handler waited for the lock. All twelve inbound rows durable. `created` wrong on all twelve - **W7**. |
| C2 | Six conversations started simultaneously | 0.44 s wall against a 0.10 s single-turn baseline: they do not serialise against each other. |
| C3 | Client disconnects 50 ms into its own turn | Turn completed; the transcript on reconnect had the customer message and the agent's reply, nothing lost or duplicated. |
| C4 | Eight messages sent back-to-back on one socket faster than turns complete | All eight present, **in order**, none lost or duplicated. |
| C5 | Reconnect racing an in-flight turn | `ready` returned `status: running` with the partial history, then the new socket received the turn's `message` frame; the final transcript was correct. No loss, no duplicate. |
| C6 | Two `POST`s issued together on one conversation, in a known order | **Reordered** - the second ran first and permanently parked the conversation - **W4**. |
| C7 | Turn run by the drain worker with a socket watching | The watcher got the agent message *and* an announced `turn` frame. `on_drained` works; it is the non-drain path that does not (W3). |

### Commands run

| Command | Result |
|---|---|
| `ruff check .` (scratch dir excluded) | All checks passed |
| `ruff format --check .` | 160 files already formatted |
| `mypy` (strict) | Success: no issues found in 160 source files |
| `pytest -q` | **1361 passed, 2 deselected, 493 s** (the section's 1259 predates phase 6) |
| `pytest -m live -q` | 2 skipped, 1361 deselected (no `ANTHROPIC_API_KEY`) |
| `pytest tests/verify_phase_2_resolution.py -q` | 39 passed, 130 s |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (18 warning(s))`, exit 0 |
| `alembic downgrade base` then `upgrade head` then `check` then `current` | clean; "No new upgrade operations detected"; head is **0008** |
| `uvicorn app:app --port 8100` with `demo/acme_web_chat.json` on a private database | started; `GET /healthz` returned `{"status":"ok","provider":"replay","channels":["web_chat"],"database":"ok"}` and the pack fingerprint |
| the four-message refund conversation over a real WebSocket to that server | reproduced end to end: identity gate, then `awaiting {kind: confirm, node: confirm_refund, tool: issue_refund}`, then "That is refunded...", then `done`; one approval, one `issue_refund` call |
| README `/health` claim | **not reproducible**: `032fb6a:README.md` says `/healthz`, as does the current README (W12) |

Everything else in the verification section reproduced. The demo servers on ports 8000 and 8001
were not touched; the review used port 8100 and a `support_review` database created and dropped
for the purpose.

### Missed by self-critique

The self-critique is unusually good - four of its six "where email will strain this protocol"
items are exactly right, and its fragility list predicted W5's shape. What it missed:

1. **`InboundMessage.metadata` points the wrong way, and is the email threading gap.** The
   critique's item 5 worries that `conversation_key` is a pure function; the harder problem is
   that `metadata` - the field an email adapter would put `Message-ID` and `References` in - is
   written by nobody, read by nobody, and, decisively, **not reachable from `send`**. `send` is
   given a `ConversationRef` built from the conversation *row*; nothing carries the inbound
   message's transport headers to the outbound reply. An email adapter cannot set `In-Reply-To`
   without its own second lookup, which is the thing the protocol was supposed to spare it.
2. **`InboundMessage.customer_ref` is dropped on the way to the context.** The protocol carries
   who the transport believes the sender is - which for email is the whole point of the `From`
   header - and `conversation_for` seeds every new conversation from the single constant
   `AppConfig.new_conversation_context` instead. `customer_ref` reaches
   `conversation.customer_ref` and never reaches `ctx.customer`, which is what gates and prompts
   read. The critique names the missing CRM seam (item 5) but not that the protocol already
   carries half of what that seam needs and the hub discards it.
3. **`send` cannot fail.** It returns `None`, and `ChannelHub.deliver` swallows every exception by
   design, so an adapter has no way to say "this one is a permanent bounce, do not mark it sent".
   The critique's email item 2 sees that a buffering adapter is recorded as having delivered what
   it queued; the sharper version is that *no* adapter can refuse a delivery, so `mark_sent` is
   unconditional for every channel, not only a buffering one.
4. **The status of a turn reaches one socket, not the conversation** (W3). The critique reasons
   carefully about message delivery being per-conversation and does not notice that the *state*
   frame is per-connection - which breaks the one screen element the phase says it exists to show.
5. **A non-text frame crashes the handler** (W2). "Which tests are weak" lists the JavaScript and
   the missing crash tests; the transport's own frame types are not considered at all.
6. **Two callers on one conversation can be reordered** (W4). The critique asserts the loser's
   message is "durable, `pending` and in order"; the order is by transaction-start timestamp with
   a random tie-break, and I reversed it.
7. **The desk arrived early and unauthenticated on this surface** (W1). The critique correctly
   files the auth gap under "for a demo on 127.0.0.1 that is honest" and points at phase 7 - but
   phase 6 then mounted the desk on this app, default on, which turns a self-inflicted risk into
   somebody else's data. That deferral needs re-opening, not re-deferring.

---

## Resolution

Resolver: a fresh agent that wrote none of the phase W code, 2026-09-07 (PLAN.md step 5). **Both
must-fixes are fixed with regression tests that fail against the reviewed code**, every should-fix
is fixed, and both actionable nits are fixed; the third nit (W13) is a date-stamp on the section
it is about. Nothing was deferred whole. Three pieces that are genuinely phase 7's - per-operator
desk identity, taking the desk off the customer's listener, and carrying transport detail from an
inbound message to an outbound reply - are in BACKLOG.md's "Deferred findings" with checklist
lines in phase 7.

The review itself was committed first as `56f5847`; it was sitting uncommitted in the working
tree, so the diff that resolves it reads against what was actually reviewed.

One thing did not turn out as reported, and one commit is coarser than it should be. **W12** is
confirmed exactly as the reviewer describes it: the README never said `/health`, the verification
section claimed a defect that never existed, and that paragraph now says so instead of leaving the
claim standing. And `8720a81` carries eleven findings in one commit rather than several, because
`support_core/api/app.py` carries W1, W2, W8 and W9 and `support_core/storage/repositories.py`
carries W4, W5 and W10 - splitting by file would have split nothing.

| id | severity | action | commit |
|----|----------|--------|--------|
| W1 | must-fix | **Fixed.** Three changes, and the order is what makes it a fix rather than a hiding place. `serve_desk` now defaults to `False`, so **a deployment that configures nothing serves no desk at all** - `/desk/*` is not routed and answers 404. `serve_desk` on with no `desk_token` raises `ConfigError` in `create_app`, *before* the runtime is built, so a missing credential stops the application rather than opening a desk; a token under 16 characters is refused the same way. And every `/desk` route sits behind a bearer token compared with `secrets.compare_digest`, attached to the **router** rather than to each endpoint, so a route added later is guarded by construction. The credential is read from the `Authorization` header and nowhere else - never a query parameter, for the reason W9 gives. `tests/test_desk_auth.py` is the reviewer's attempt A12 as eight regression tests, **all eight of which fail against the reviewed code**, including one that enumerates the router and asserts every route it finds answers 401. Before the fix, reproduced on a running server: `GET /desk/handoffs` 200 with the whole queue, `GET /desk/handoffs/{id}` 200 and 1763 bytes of another customer's packet, `GET /desk/conversations/{id}/transcript` 200 with their text, `POST .../reply` 200 and the words written into their transcript. After: 401 on all four, nothing in the body. **The desk stays on this application rather than moving to its own port, and the reasoning is recorded** in the `support_core.api.app` docstring and in the decisions log: DESIGN.md 4.1 makes a deployment one service and section 12 says the desk "uses the same API surface", so a second listener is a change to the design rather than a reading of it; and a desk `reply` reaches a customer's open socket only from the process holding it, so splitting the desk out today would silently stop delivering a human's reply until phase 7's fan-out exists - trading a hole that is now closed for a regression that is not. What made the exposure was the default, not the shared port. Nothing else mounted on that app has the same shape: the two channel endpoints are the customer's own conversation (their access control is the session key, still phase 7's), `/` and `/static` are the demo page, and `/healthz` returns deployment metadata - pack id, version, fingerprint, provider, channels - and no customer data. | 8720a81 |
| W2 | must-fix | **Fixed.** The socket reads raw frames with `WebSocket.receive()` and branches on whether a text frame arrived. A binary frame is answered with an `error` frame saying what was wrong and closed with 1003 ("unsupported data"), rather than raising `KeyError: 'text'` out of the handler. `test_a_binary_frame_is_refused_rather_than_crashing_the_handler` sends the reviewer's exact input, asserts the error frame and the close code, and then opens a second connection and holds a whole conversation on it, because "the server is still serving" is half of what a clean refusal means. It fails against the reviewed code with the reviewer's own traceback. The opening frame gets the same treatment: a binary hello is refused before any row exists. | 8720a81 |
| W3 | should-fix | **Fixed as the reviewer suggested.** The `turn` frame is broadcast to every connection watching the conversation, from `AppRuntime.deliver_inbound`, so it reaches a second tab and a customer whose reply arrived on another transport - and it reaches them whoever ran the turn: this socket, another socket, the `POST` handler, or the drain worker. `queued` is out of the frame and into a per-caller `queued` frame on the socket, and stays in the HTTP response where it always was. Re-run of the reviewer's attempt A3: the watching tab received `['message', 'turn']` with the same `awaiting` the sender got, where it previously received `message` only. | 8720a81 |
| W4 | should-fix | **Fixed in the durable path.** `message.queue_seq` is claimed from `conversation.inbound_seq` by an `UPDATE ... RETURNING` inside `enqueue_inbound`, which takes the conversation's row lock, so concurrent callers serialise at the counter and leave with distinct increasing numbers; `peek_next_pending` and `pending_inbound` order by it. Migration `0009`, with a backfill that numbers existing rows in the order the old query would have produced them. This is the shape of phase 2's R1 and R2 fixes rather than a tighter clock: the order is *decided* when the row is written instead of reconstructed afterwards from `created_at`, which is the transaction-start timestamp and identical for a simultaneous pair, with a random UUID breaking the tie. `tests/test_inbound_order.py` has three tests and **all three fail against the reviewed code**; the first is deterministic - two rows written in one transaction, so they share `created_at` exactly, with their ids forced into the wrong sort order, which is the reviewer's reordering with the coin toss removed. What this does not claim: which of two genuinely simultaneous requests *should* be first is not a question the database can answer. What it claims is that there is exactly one order, that every caller has a place in it, that the place is decided when the row is written, and that the drain follows it. Re-run of attempt C6: the two posts issued together took `queue_seq` 1 and 2 in the order they were issued and ran in that order. `tests/verify_phase_2_resolution.py` reads arrival order from `queue_seq` now instead of from `created_at`; the property it asserts is unchanged. | 8720a81 |
| W5 | should-fix | **Fixed as the reviewer suggested.** `pending_outbound` selects `FOR UPDATE SKIP LOCKED`, so a desk `reply` concurrent with a turn's `_flush_outbound` cannot read the same `pending_send` rows in both transactions and deliver each message twice. Skipping rather than waiting is right here: a row another transaction is already delivering is a row this one has nothing to do about. | 8720a81 |
| W6 | should-fix | **Fixed.** `Executor._handoff` emits the same sentence a `handoff` node emits, chosen by the same rule - `DEFAULT_HANDOFF_MESSAGE` when the hook says a human was told, `HANDOFF_UNDELIVERED_MESSAGE` when nobody was - so the turn that raises a handoff is no longer silent. It says nothing about *what* failed: the reason, the rejected decision and the detail belong in the packet a person reads, and three tests assert they are absent from the customer's transcript. `test_a_turn_that_fails_still_says_something_to_the_customer` is the reviewer's attempt A8 over a real socket. Re-run of A8 on a fresh process: the customer is told "I am passing this conversation to one of our people...", where they previously received nothing at all. | 8720a81, cba859c |
| W7 | should-fix | **Fixed as the reviewer suggested.** `conversation_for` catches `IntegrityError` explicitly instead of suppressing it, and reports `created` from whether its own insert committed. Re-run of attempt C1: twelve simultaneous posts on one key produced one conversation and `created: true` on **one** of twelve, where it was previously true on all twelve. Nothing branches on the flag today; phase 7's email adapter is the caller that would, to decide whether to send a greeting. | 8720a81 |
| W8 | should-fix | **Fixed as the reviewer suggested, and a little further.** Opening a socket resolves the session key without creating anything and sends `ready` with a null conversation and an empty history; the first *message* creates the row. The extra part is what the one-line version of the fix would have broken: a connection with no conversation yet waits in the registry under its channel key, and `ConnectionRegistry.attach` moves every waiter across when somebody's message creates the row - *before* the turn runs - so two tabs opened together on a fresh key, and a tab watching while the first message arrives by `POST`, both keep receiving what the conversation says. `test_a_client_that_brings_no_session_is_given_one` counts the rows: zero after connecting, one after speaking. | 8720a81 |
| W9 | should-fix | **Fixed by the reviewer's second option.** The session key arrives in an opening frame - `{"type": "hello", "session": "<key>"}`, or `{"type": "hello"}` to be given one - and the query string is gone from the client, the endpoint and the test helper. Verified in the server log of the manual run: `WebSocket /channels/web_chat/ws [accepted]`, and a count of `session=` across the whole log returns **0**. `Sec-WebSocket-Protocol` was the reviewer's first suggestion and was rejected on a detail: a subprotocol value is an HTTP token and `SESSION_KEY` permits `:`, which is a separator. A connection that says nothing is closed after 15 seconds, since the key no longer arrives with the handshake. | 8720a81 |
| W10 | should-fix | **Fixed as the reviewer suggested.** `transcript` returns an outbound row only when it is `sent`, so the reconnect path cannot show a customer text that phase 7's send-time guardrail refused to deliver. It changes nothing today - delivery is the only thing that can leave a row `pending_send` - and it puts the seam in the right place before the guardrail arrives, which is the reviewer's argument and it is correct. | 8720a81 |
| W11 | should-fix | **Fixed as the reviewer suggested.** The client renders three authors: `agent`, `customer`, and anything else as a person at the desk - on the agent's side of the transcript, labelled, with its own style. Only an `agent` message updates `state.lastAgentMessage`, so an approval panel raised after a desk reply still quotes the proposal the approval is bound to. | 8720a81 |
| W12 | nit | **Fixed - the claim is struck.** The verification section's finding "the README's own instructions say `/health`" is not reproducible and never was: `git show 032fb6a:README.md` says `/healthz` and so does the current README. The paragraph now carries a note saying the claim was false and is withdrawn, rather than leaving a defect in the record that this project never had and never fixed. | cba859c |
| W13 | nit | **Fixed as the reviewer's first option.** The verification table is date-stamped and prefaced with a note that its 1259 tests, 9 pack warnings and head `0007` are phase W's own numbers, that phase 6 moved all three to 1361, 18 and `0008`, and that none of the three is a regression. The numbers for the tree as it stands are below. | cba859c |
| W14 | nit | **Fixed.** `RESOLVED` deleted. Unused anywhere in the tree and wrong besides - it did not list `glm`, which `resolve_provider` returns. | 8720a81 |
| W15 | nit | **Fixed.** A `fatal` error frame stops the client reconnecting, so a malformed key no longer reproduces its own rejection every five seconds for ever. "Restart" clears the flag, because that is the customer's way out. | 8720a81 |
| W16 | nit | **Fixed by dropping it, with the real gap written down.** `InboundMessage.metadata` is gone. Nothing wrote it, nothing read it, and - decisively, as the reviewer says - it was not reachable from `send`, which is given a `ConversationRef` built from the conversation *row*, so the one job it named was the one job it could not do. A field that looks like a seam and is not is worse than no field, because the next phase builds against it and finds out late. Carrying an inbound message's transport headers to an outbound reply is a real gap and is now a phase 7 checklist line, beside the two neighbouring ones the reviewer's "missed by self-critique" section raises: `customer_ref` never reaching `ctx.customer`, and `send` being unable to refuse a delivery. | 8720a81 |

### Verification, on 2026-09-07

| Command | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 163 files already formatted |
| `mypy` (strict) | Success: no issues found in 163 source files |
| `pytest -q` | **1374 passed, 2 deselected, 565 s** |
| `pytest -m live -q` | 2 skipped, 1374 deselected (no `ANTHROPIC_API_KEY`) |
| `pytest tests/verify_phase_2_resolution.py -q` | 39 passed, 146 s |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (18 warning(s))`, exit 0 |
| `alembic downgrade base`, `upgrade head`, `check`, `current` | clean; "No new upgrade operations detected"; head is **0009** |

### The conversation, driven by hand

`SUPPORT_APP_CONFIG` pointing at the demo configuration with `serve_desk` on, a `SUPPORT_DESK_TOKEN`
in the environment, a private `support_w_resolution` database, and `uvicorn app:app --port 8100`.
The demo servers on 8000 and 8001 were not touched and were still answering afterwards; the
server started here was stopped and its database dropped.

`GET /healthz` returned 200 with `provider: "replay"`, `channels: ["web_chat"]`, `database: "ok"`
and the pack fingerprint. `GET /` served the client page. Then, over a real WebSocket:

1. `hello` with no key gave `ready`, a server-assigned session, **`conversation_id: null`** and an
   empty history. Nothing was written (W8).
2. *"I got charged twice for the Pro Plan this month; can I have one of them back?"* produced
   **"I have sent a six-digit code to me@example.com. What is it?"** and `waiting_customer` on
   `ask_code`. The identity gate before the account, as always.
3. *"The code is 581139. Also, can you change my address while we are at it?"* produced the
   deferral sentence, then **"I can refund 29.00 USD for Pro Plan - September (2026-09-03) to your
   original payment method. Shall I go ahead?"** with `awaiting {kind: confirm, node:
   confirm_refund, tool: issue_refund}`. Nothing moved.
4. A **reconnect on a second connection** at that point returned the same conversation id, five
   messages of transcript and the same `awaiting` - so the approval panel comes back after a
   reload.
5. *"Yes please, go ahead and refund it."* produced **"That is refunded. It takes five to seven
   business days to show on your statement."**, then *"Is there anything else I can help you
   with?"*
6. *"Yes please - it is 4 Elm Row, Edinburgh, EH7 4AH, United Kingdom."* produced the address
   workflow's own confirmation, which is phase 6's deferred intent coming back.

In the database afterwards: one conversation, one `issue_refund` `tool_call` (`high`, `succeeded`,
carrying an `approval_id`), exactly one `action_approval` - `customer`, `confirm_refund`, consumed -
and no other WRITE or HIGH call carrying one. In the uvicorn access log: `WebSocket
/channels/web_chat/ws [accepted]`, with **zero** occurrences of `session=` anywhere in the log
(W9).

On a second, fresh process on port 8101 - because the sample pack's billing system is an in-memory
fake seeded at startup, so only one refund conversation per process can succeed, which the README
says - the four-message recorded refund ran to `done`, and then a message on that finished
conversation failed a node and the customer was told about it (W6, attempt A8).

### The reviewer's matrices, re-run

Same shape as the review's own logs, against a real uvicorn server on 8100 with the demo
configuration and a private database. A8 and A13 were re-run on the fresh process on 8101 for the
reason above; both need the refund to work, and the first run had spent it.

**Channel boundary: 13 attempts. Before: 9 held, 2 broke, 2 as designed. After: 11 held, 0 broke,
2 as designed and still deferred.**

| # | before | after |
|---|--------|-------|
| A1 forged session key | held, but created a durable row | **held**, and creates nothing (W8) |
| A2 another client's key | as designed - the key is a bearer token | unchanged, and the key is no longer in the access log (W9) |
| A3 two sockets on one key | only the sender got the `turn` frame (W3) | **held**: the watcher got `message` *and* `turn`, with the same `awaiting` |
| A4 frame naming another session | held | held; the victim's transcript unchanged |
| A5 seven malformed frame shapes | held | held, 7 of 7 non-fatal errors, socket still usable |
| A6 4 001 and 400 000 characters | held | held |
| A7 binary frame | **broke** - unhandled `KeyError`, ASGI traceback | **held**: error frame, close 1003, next connection served (W2) |
| A8 message after the conversation is `done` | **broke** - zero outbound messages | **held**: the customer is told (W6) |
| A9 speaking into a parked run | held and correct | held, acknowledged once, run not resumed |
| A10 every frame read for leakage | held | held; `awaiting` carries `kind`, `node`, `tool` only |
| A11 cross-conversation `POST`, hostile `Origin` | writes; known, deferred | unchanged, still deferred to phase 7 |
| A12 unauthenticated `/desk/*` | **broke badly** - the whole database | **held**: 401 on all seven endpoints and on a wrong token, nothing leaked (W1) |
| A13 the confirm chain | held | held: one approval, consumed, one refund |

**Concurrency: 7 attempts. Before: 6 held, 1 broke. After: 7 held.**

| # | before | after |
|---|--------|-------|
| C1 twelve simultaneous posts on one key | one conversation; `created` wrong on all twelve (W7) | one conversation, 11 queued, **0.32 s** wall, `created: true` on **1** of 12; `queue_seq` 1 to 12, all distinct |
| C2 six conversations at once | 0.44 s against a 0.10 s baseline | **0.42 s** against a 0.10 s baseline - still no serialising |
| C3 disconnect 50 ms into the turn | held | held; transcript on reconnect intact |
| C4 eight back-to-back on one socket | held, in order | held, 8 of 8 in order |
| C5 reconnect racing an in-flight turn | held | held; the new socket got the turn's `message` and its `turn` frame |
| C6 two posts together, known order | **reordered**, and parked the conversation for good (W4) | **held**: `queue_seq` 1 and 2 in the order issued, processed in that order |
| C7 drain worker with a socket watching | held | held |

### What is still open on this surface

The customer channel's own access control is unchanged and still phase 7's: the session key is a
bearer token, there is no origin check on the socket, no CSRF defence on the webhook, no rate limit
and no webhook signature verification. A2 and A11 are that gap, and they are recorded above as
deferred rather than as passes. What changed is that the *desk* is no longer part of it, and that
the one secret a customer holds is no longer written into every log between the browser and the
application.
