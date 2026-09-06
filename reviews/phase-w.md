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
