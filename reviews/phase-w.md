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

Commands run from the repository root with `.venv/Scripts/python.exe`, on 2026-09-06:

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
Note the README's own instructions say `/health`; the endpoint is `/healthz` (recorded as a
finding for the review).

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
