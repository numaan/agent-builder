# Cross-cutting security review — 2026-09-07

Scope: the whole repository at `master` (`7596c60`), read-only. Prompted by phase W's finding
that an unauthenticated human-agent desk was mounted on the customer's port and served other
customers' transcripts; the question asked was what else of that shape is present.

Method: DESIGN.md 3, 4.1, 8.1–8.4, 10, 11.2, 12, 13, 14, 20 and `reviews/phase-0.md` through
`phase-6.md` and `phase-w.md` (Independent review and Resolution sections) were read first, so
that a finding already fixed is not reported again. Code was read across `support_core/`,
`packs/` and `tests/`. Where a finding was testable it was run: against a private database
(`secreview`, created and dropped for this review) and a private port (8200). Nothing in the
repository was modified; the throwaway scripts under `reviews/scratch-security/` were deleted.

**Phase 5 (knowledge, retrieval, citations) is in flight in this working tree.** `support_core/
knowledge/*`, `support_core/guardrails/outbound.py`, `support_core/storage/knowledge_repo.py`
and migration `0010` are uncommitted. They were read for context only and nothing from them is
recorded as a finding; where they touch a finding it is said so.

---

## Verdict

The structural safety property this project is built around — that no WRITE or HIGH tool runs
without a live, single-use approval bound by `sha256(tool + canonical_json(args))` to the frame,
the run and the confirm node that proposed it — holds. Twenty-four bypass attempts, fourteen
adversarial approval tests and the desk's own eight authentication tests pass, and reading the
code found no route to a guarded action that skips the approval: not through the desk, not
through a resume, not through a pack-registered node type, not through the MCP adapter, and not
through the model's read-only tool loop. The desk hole phase W closed is genuinely closed and
does not have a relative on the desk surface. What it does have relatives of is elsewhere: the
customer channel still has no authentication beyond a bearer session key, and the demo client
shipped in core takes that key from the page URL, which is both the unfixed half of finding W9
and a working session-fixation attack that hands one customer's live conversation to whoever
sent them a link. Beside it, the service has no rate limit, no request size limit and no
database connection budget, so ten concurrent anonymous requests take the whole deployment —
desk included — offline for thirty seconds. Nothing leaks a credential to a customer and nothing
leaks internal state into an outbound message; but every durable store keeps customer text
verbatim, the `redacted_text` column has never been written to, and there is no retention
mechanism at all and none scheduled.

---

## Routes the app mounts, and what guards each

Enumerated from the app object under every configuration `AppConfig` supports (script:
`reviews/scratch-security/routes.py`, since deleted). `docs_url`, `redoc_url` and `openapi_url`
are all `None`, so there is no schema or docs route under any configuration.

| Route | Served when | Authenticated by | Exposes |
|---|---|---|---|
| `GET /healthz` | always | **nothing** | pack id, version, pack fingerprint, entry graph, provider name, channel list, database reachability |
| `POST /channels/web_chat/messages` | always | **nothing** — the `session` key in the body is the only access control | writes into any conversation whose key the caller knows; creates one otherwise. Returns conversation id, run status, `created` (an existence oracle for a key) |
| `WS /channels/web_chat/ws` | always | **nothing** — the `session` key in the opening `hello` frame | the full transcript, run status, and which confirm node and tool a pending approval names; writes into the conversation |
| `GET /` | `serve_client` (default **on**) | nothing | the demo page |
| `/static/*` | `serve_client` (default **on**) | nothing | `app.js`, `index.html`, css. No directory listing |
| `GET /desk/handoffs` | `serve_desk` (default **off**) | bearer token, router-level dependency | the whole deployment's handoff queue |
| `GET /desk/handoffs/{id}` | `serve_desk` | bearer token | the full handoff packet and messages queued since |
| `POST /desk/handoffs/{id}/reply` | `serve_desk` | bearer token | writes a message into the customer's transcript |
| `POST /desk/handoffs/{id}/resume` | `serve_desk` | bearer token | resumes the graph with a validated state patch |
| `POST /desk/handoffs/{id}/close` | `serve_desk` | bearer token | ends the run |
| `POST /desk/handoffs/{id}/approve` | `serve_desk` | bearer token | the human half of `requires_human_approval` |
| `GET /desk/conversations/{id}/transcript` | `serve_desk` | bearer token | any conversation's transcript in the deployment |

Verified: the bearer dependency is attached to the **router**, so all seven desk endpoints carry
it (`support_core/api/desk.py:172`); `serve_desk=True` with no token is a startup refusal, not an
open desk (`support_core/api/config.py:229-250`, called before the router is built at
`support_core/api/app.py:135`); and `serve_client=False` removes both `/` and `/static`.
Unauthenticated probes returned 401 on every desk route and 200 with the token.

---

## Findings

Ranked by severity. "Demonstrated" means it was reproduced against a running server or database
in this review; "inferred" means it was established by reading the code only.

| id | sev | location | finding | evidence | suggested fix |
|---|---|---|---|---|---|
| **S1** | **high** | `support_core/api/static/app.js:53-63`, `:65-73`, `:163`; `support_core/api/app.py:374-377`; `support_core/channels/web_chat.py:44` | **Session fixation, and the unfixed half of finding W9.** `readSession()` takes the session key from the *page* URL query string and prefers it over `localStorage`, then `rememberSession()` persists it. Two consequences. (a) A link `https://support.example/?session=<key the attacker chose>` makes the victim's browser adopt that key and keep it; every word they then type — identity answers, card references, addresses — lands in a conversation the attacker can read and write. (b) The key is written verbatim into the access log, the browser history and any `Referer`, which is exactly the defect W9 fixed for the WebSocket URL and left in place here. The server accepts it because `_session_of` returns whatever the client asked for and `SESSION_KEY` permits any 8–128 character token, so a client-chosen key never has the 96 bits `new_session_key()` gives. | **Demonstrated.** With `ATTACKER_KEY = "attacker-fixed-key-0001"`: a WS `hello` before use returns `conversation_id: null`; a victim POST on that key creates conversation `b7858287-…`; a second, unrelated connection using the same key read back the customer's message verbatim. Separately, `GET /?session=SECRET-KEY-IN-URL-123456` appears in full in the uvicorn access log. | Delete the `?session=` branch from `readSession()`; take the key only from `localStorage` and only from a `ready` frame the server issued. Server side, ignore a client-proposed key that is not one the server minted — or at minimum require the server's own length and alphabet, so a key is always 96 bits. |
| **S2** | **high** | `support_core/storage/session.py:19`; `support_core/engine/locks.py:56-91`; `support_core/api/app.py:200-225` | **Ten concurrent anonymous requests deny service to the whole deployment.** `make_engine` takes SQLAlchemy's default pool (5 + 10 overflow, 30 s checkout timeout) and nothing sizes it. A turn holds a dedicated connection for the advisory lock for its whole duration *plus* a connection per checkpoint transaction, so roughly seven concurrent turns saturate the pool. Past that, every request blocks for the full 30 s and then raises `QueuePool limit … timed out` as an **unhandled exception** — a 500 with a full traceback, not a routed failure or a 503. The desk and `/healthz` are on the same pool and stall with it. The inbound message rows are already durable and `pending` when the turn dies, and nothing drains them: the scheduled `recover_stalled` is phase 7's, so a failed flood leaves permanently unprocessed customer text. | **Demonstrated.** 6 concurrent POSTs: all 200, 2.1 s. **10 concurrent POSTs: 7 of 10 return 500**, and `/healthz` went from 0.1 s to **28.5 s** during it. 16 concurrent: 15 of 16 fail. After the runs, `secreview` held **42 inbound messages stuck `pending` against 45 idle runs**. | Size the pool explicitly and derive it from expected turn concurrency; cap in-flight turns with a semaphore and answer over the cap with 503 rather than blocking for 30 s; catch `sqlalchemy.exc.TimeoutError` at the handler and return 503; and give the failed inbound message to the drain worker rather than leaving it. |
| **S3** | **high** | `support_core/api/app.py:200`, `:227`; `support_core/graph/manifest.py:147`; `support_core/engine/executor.py:2112-2113`, `:720-721` | **No rate limit anywhere, and the one spend control the design specifies is a field nothing reads.** `max_llm_cost_per_conversation_usd` is declared on the manifest and referenced by no other line in the repository; `llm/types.py:87` records per-call usage for metrics and nothing sums it. Every other limit (`max_nodes_per_turn`, `max_tool_calls_per_turn`, `max_tool_iterations`) is reset per turn, and turns are free to create from an unauthenticated endpoint. So one anonymous client can drive unbounded model spend and unbounded row growth in a loop, and DESIGN.md 7.3's `limit_exceeded`-on-cost and DESIGN.md 20's "per-conversation cost cap enforced by the engine" are both unimplemented. | **Demonstrated** for the unauthenticated row growth and turn creation (30 anonymous POSTs created 31 conversations and 31 runs with no throttle of any kind); **inferred** for the cost cap, from a repo-wide grep returning the declaration alone. | Accumulate `Usage` per conversation into a column and enforce `max_llm_cost_per_conversation_usd` in the turn loop, routing to `limit_exceeded`. Add a per-IP and per-session-key rate limit in front of both channel endpoints, and cap conversation creation per source. |
| **S4** | **medium** | `support_core/storage/models.py:148`; `support_core/observability/__init__.py`; `support_core/storage/repositories.py:248-256`, `:813-856`; `support_core/handoff/builder.py:91-116` | **Nothing is redacted, and nothing is ever deleted.** `message.text` (raw customer words), `tool_call.args` and `tool_call.result` (addresses, card references, refund amounts), `trace_step.state_patch` (slots extracted from customer text), `trace_step.error`, `handoff.packet` (transcript excerpt, `ctx.customer`, tool args and results) and `conversation.context` / `conversation.summary` all hold customer data verbatim. `message.redacted_text` exists in the model and the initial migration and is written by no code path. This is against DESIGN.md 14 and 20. Redaction *is* scheduled (BACKLOG.md phase 7, "Structured JSON logs with PII redaction applied before emission" and "Inbound guardrails: PII tagging and redaction in traces"). **Retention is not**: no `DELETE` touches `conversation`, `message`, `trace_step`, `tool_call`, `handoff` or `customer_memory`, and no backlog item schedules DESIGN.md 20's "conversation data retention configurable per pack". | Inferred (code and schema read; backlog checked). | Redaction is correctly phase 7's. Add a retention item to the backlog now, with the per-pack knob DESIGN.md 20 promises, before the first deployment accumulates data nobody can lawfully delete. |
| **S5** | **medium** | `support_core/llm/service.py:528-532`; `support_core/engine/runners.py:521`; `support_core/graph/nodes.py:277` | **One place where customer-derived text reaches prompt layer 4 instead of a data block.** An `ask` node's `prompt` is a Jinja template rendered against frame state, and `AskRunner.resume` passes the rendered string to `extract_slots`, which interpolates it into `node_instructions` — layer 4, the trusted instruction layer — rather than into a fenced slot. A question like `Hi {{ state.name }}, what is your order number?` therefore carries a value the customer supplied on an earlier turn into instruction context. This is precisely the escape `LlmRunner` asserts cannot happen for `llm` nodes. Two things bound it: the text is still `neutralise`d, so it cannot forge a fence or a header (phase 3's nonce work holds), and the call's output is a closed slot model that `AskRunner` filters against the node's declared slots — so the blast radius is influencing the *extracted value of the slots being asked for*, not arbitrary state. | Inferred (every other of the fifteen text entry points was traced and is fenced). | Move the question into `state={"question_asked": …}`, the way `read_confirmation` and `read_interrupt` already do. Add a caller-level assertion to `test_prompt_injection_matrix.py`: the existing matrix proves the assembler, not which caller may write which slot, which is the gap this sits in. |
| **S6** | **medium** | `support_core/tools/mcp.py:171` → `support_core/tools/runtime.py:735-742` | **A remote MCP server's tool `description` reaches the model as instruction context.** Result *content* is correctly fenced with the turn's nonce and a sanitised label; the description is taken verbatim into `Tool.description`, copied into `ModelToolSpec.description` and rendered into the provider's tool-definition block, which is not a data block. A hostile or compromised MCP server writes directly into the model's instruction surface. Latent: nothing in `support_core/`, `packs/` or `app.py` constructs a `McpToolAdapter` today. | Inferred. | Neutralise and fence the remote description the way passage text is, or refuse a description that fails the reserved-line check. Also note the doc/code split: DESIGN.md 8.3 specifies `McpToolAdapter(server_url, risk_map)`; the implementation takes a live client and never parses a URL. |
| **S7** | **medium** | `support_core/api/app.py:210` | **No request body size limit.** `await request.json()` reads and parses the entire body before `MAX_TEXT` (4000 characters) is consulted, and `MAX_TEXT` is checked against `payload.text` only. Neither Starlette nor uvicorn caps body size by default and `create_app` sets none, so a multi-gigabyte POST is buffered and parsed before it is rejected. WebSocket frames are bounded only by uvicorn's 16 MB `ws_max_size` default, which `app.py` does not set. Cheap amplifier for S2. | Inferred. | Reject on `Content-Length` before reading, cap the stream read, and set `ws_max_size` explicitly. |
| **S8** | **medium** | `support_core/channels/web_chat.py:84-96`, `:117-130`, `:138-152` | **Unbounded WebSocket connections per client.** `ConnectionRegistry.add` and `wait` append to lists with no cap; `count()` exists and is called by nothing. One client can open as many sockets as the OS allows, each holding a connection object and an `asyncio.Lock`. `broadcast` is O(connections) per outbound message, so N sockets on one conversation multiply every message N times, and `forget` walks the whole registry on each disconnect. | Inferred. | Cap connections per conversation and per source, and refuse past it. |
| **S9** | **medium** | `pyproject.toml:14-40` | **Every dependency is a lower bound and there is no lockfile.** Installed versions have already drifted far past the floors the code was written against — `anthropic 1.4.0` against `>=0.40`, `mcp 2.1.1` against `>=1.0`, two major versions. `support_core/tools/mcp.py:106-118` reads `.isError` / `.structuredContent` / `.content` off SDK objects with `getattr` and silent fallbacks, so an SDK shape change degrades to a stringified blob rather than failing loudly. The `colbert` extra pulls `fastembed` → `onnxruntime` and downloads model weights from Hugging Face at first use, which is a third-party runtime fetch; it is correctly an extra. | Inferred (`pip list` compared against the declared floors). | Add a lockfile and pin the runtime set with upper bounds, and make the MCP result-shape reads explicit rather than `getattr`-with-fallback. |
| **S10** | **low** | `support_core/api/desk.py:154` | **A non-ASCII bearer token is a 500, not a 401.** `secrets.compare_digest` raises `TypeError: comparing strings with non-ASCII characters is not supported` for `str` arguments outside ASCII, and nothing catches it, so an anonymous caller gets an ASGI traceback per request instead of a refusal. It is not an auth bypass — nothing gets through — but the module's own claim that a wrong token is a 401 is not true for all wrong tokens, and it is a free anonymous log-flood and error-rate spike against a deployment that has the desk on. | **Demonstrated.** `curl -H "Authorization: Bearer ü" …/desk/handoffs` → 500, with the `TypeError` traceback in the server log. | Compare bytes: `secrets.compare_digest(presented.encode(), token.encode())`, or reject a non-ASCII credential as a 401 before comparing. |
| **S11** | **low** | `support_core/storage/config.py:34` | **The database URL, password included, goes into an exception message.** `_require_asyncpg` interpolates the raw URL into a `ValueError` on a driver-prefix mismatch, and `database_url()` passes `SUPPORT_DATABASE_URL` straight through it, so a malformed URL puts `postgresql://user:password@host/db` into the startup traceback and hence into container and CI logs. Not reachable over HTTP. `test_database_url` is fine — it interpolates only the database name. | Inferred. | Interpolate `make_url(url).render_as_string(hide_password=True)`, or just the scheme. |
| **S12** | **low** | `support_core/api/config.py:140`, `:210` | **The desk token is an ordinary field on an ordinary `BaseModel`.** No `SecretStr`, no `repr=False`. Any `repr(config)`, `print(config)` or `model_dump()` emits the bearer token verbatim, and `from_env` wraps a pydantic `ValidationError` into a message — pydantic v2 includes `input_value`, so a validation failure on `desk_token` would echo it. Latent: nothing logs the config today. | Inferred. | Make it a `SecretStr`, and do not include the pydantic error body in the `ConfigError` message for secret-bearing fields. |
| **S13** | **low** | `support_core/api/app.py:177-198` | **`/healthz` is unauthenticated and describes the deployment**: pack id, version, pack fingerprint, entry graph, provider name and channel list. It is careful about the one thing that matters — the database failure is reported as `f"unavailable: {type(exc).__name__}"`, with no SQLAlchemy message and no URL — but the rest tells an anonymous caller which pack and which model vendor a deployment runs. | Inferred (response body captured). | Split it: an unauthenticated liveness probe that returns status only, and the descriptive body behind the desk credential. |
| **S14** | **low** | `support_core/api/app.py:227-229`, `:200` | **No `Origin` check on the WebSocket handshake and no CSRF defence on the webhook.** Any third-party page a customer visits can open sockets and POST to the service from their browser. Because authentication is a key in a frame rather than a cookie, this does not by itself reach an existing conversation — but it makes S1 delivered and S2 driven from a victim's browser. Recorded as open in phase W's "What is still open on this surface"; repeated here only because S1 and S2 change what it is worth. | Inferred. | Check `Origin` against a configured allow-list on the handshake. |
| **S15** | **low** | `support_core/handoff/sinks.py:133-145` | **`WebhookSink` posts the full handoff packet — customer name, email, reference, transcript excerpt, tool arguments and results — to an operator-configured URL with `headers={}` by default.** No signature, no authentication on the outbound call, no redaction. The URL is operator-supplied, so this is a configuration hazard rather than an attack, but a mistyped host receives a customer's data in the clear. | Inferred. | Require a signing secret (HMAC over the body) or at least an `Authorization` header, and refuse a non-HTTPS URL. |
| **S16** | **low** | `support_core/api/desk.py:71`, `:94`, `:111`; `support_core/api/config.py:140-150` | **One shared desk token and a self-asserted `human_id`.** Every desk action takes `human_id` from the request body and records it unverified, so an operator can attribute a reply, a resume, a close or a countersignature to anyone. The module says so and puts real operator identity with phase 7; noted here because `approve` is a second signature, and a second signature nobody can attribute is a weaker one than it looks. | Inferred. | Per-operator credentials, with `human_id` derived from the credential rather than the body. |
| **S17** | **low** | `support_core/tools/mcp.py:141`, `:178-179` | **Two remote-influenced knobs on the MCP adapter.** `confirm_exempt` is keyed on the *remote* tool name, so a server that renames a tool to a name the pack marked exempt inherits the exemption; and `prefix` defaults to `""`, so a remote tool can claim a local pack tool's exact name. That fails closed only because `ToolRegistry` rejects duplicate names — which holds if and only if both go into one registry, and nothing enforces that they do. Latent with the adapter unwired. | Inferred. | Default `prefix` to something non-empty, and key the exemption on the local (prefixed) name. |
| **S18** | **low** | `support_core/engine/executor.py:1812-1837` | **No frame-stack depth limit.** `_push` appends without checking `len(turn.frames)`, and each frame is serialised into the run row on every checkpoint. Graph recursion is caught statically by the validator (`graph/rules.py:285`), so this is defence-in-depth rather than a live bug — a pack that reached a cycle the validator missed would grow the stack until `max_nodes_per_turn` stopped it. | Inferred. | Cap stack depth in `_push` and route the overflow to `limit_exceeded`. |
| **S19** | **low** | `support_core/tools/mcp.py:66`, `:111-118` | **MCP tool output is not size-bounded at the adapter.** Content blocks are joined with no cap, so a hostile server can put an arbitrarily large string into memory, into graph state and into a `trace_step` row. It *is* bounded before it reaches a model — layer 8 has a 3000-token budget and truncates — so this is a storage and memory concern, not a prompt one. | Inferred. | Cap the joined content and record the truncation. |

---

## Already fixed, checked and confirmed

These are the protections that were verified to still hold, by reading the code and by running
the relevant suites against a private database (521 passed, 1 failed — see the note below).

**The approval and risk chain (DESIGN.md 8.2).** No route to a WRITE or HIGH tool without a
matching live approval was found.

- The risk policy is enforced twice, independently: `ReadOnlyToolGateway` refuses a non-READ tool
  before the runner is spoken to, and `ToolRuntime.invoke` refuses it again from a copy of the
  allow-list the gateway never touched (`support_core/tools/runtime.py:159-174`).
- `consume_approval` (`support_core/storage/repositories.py:721-781`) binds on conversation, run,
  `frame_seq`, confirm node id, tool name and `args_hash` together, requires `consumed_at IS
  NULL`, and claims the row with `FOR UPDATE SKIP LOCKED` so two callers cannot both win.
- The idempotency key and the approval are taken in **one** transaction
  (`support_core/tools/runtime.py:326-374`), so there is no state in which an approval is spent
  with no call, or a key claimed with a spendable approval left over.
- Re-entering a key with different arguments is refused rather than replayed
  (`runtime.py:478-485`); a non-idempotent tool whose previous attempt recorded no outcome is
  refused, not repeated (`runtime.py:517-524`).
- Only a node the graph declares as `type: confirm` may produce an approval; a pack-registered
  custom node type returning one is a node error, not a silently ignored field
  (`support_core/engine/executor.py:1433-1445`). A node that is not a `ToolNode` holds an
  `invoke` that refuses everything (`executor.py:1290-1332`).
- The desk's `approve` copies the customer's own row — same tool, arguments, hash, run, frame and
  confirm node — and supplies no arguments of its own, and it resolves the approval *from the
  packet the human was shown*, refusing a handoff that is no longer open
  (`support_core/api/desk.py:290-376`).
- A desk `resume` patch is validated against the suspended frame's own state model before
  anything is written, may never set `identity_verified`, and is refused outright while an
  approval is live on that frame (`executor.py:987-1051`).
- `new_conversation_context` may not set `customer.identity_verified`, so a deployment cannot
  open every gate from a config file (`support_core/api/runtime.py:119-140`).
- `tests/test_approval_bypass_matrix.py` (24 cases), `tests/test_adversarial_approvals.py` (14)
  and `tests/test_double_payment_matrix.py` all pass. The one case that executes by design —
  a row forged directly in the database with every binding correct — is correctly documented as
  the trust boundary rather than a hole.

**The desk (phase W finding W1).** Off by default; unserveable without a credential, checked
before the router is built; the bearer dependency is on the router so a route added later is
behind it by construction; the credential is read from `Authorization` and nowhere else.
`tests/test_desk_auth.py` passes and enumerates the router to assert exactly that. Every
unauthenticated probe of every desk route returned 401.

**The prompt boundary (phase 3).** The nine layers are rendered in a fixed enum order that
callers cannot change; `data_block()` requires a nonce; content is line-folded (NFKC, `Cf`/`Cc`
deleted, dashes normalised) and any line matching a reserved marker — including any line
containing the nonce — is kept but prefixed `[neutralised] `, so a leaked token cannot be used as
a delimiter; labels are stripped of everything `str.splitlines` breaks on. Fourteen of the
fifteen places untrusted text enters a prompt go through a fenced slot, including the in-loop
tool result, whose label comes from the resolved spec and never from the model. The exception is
S5. `tests/test_prompt_injection_matrix.py` passes.

**Identifiers.** Every primary key is `gen_random_uuid()` (`support_core/storage/models.py:45`,
`:61`) — conversation, run, handoff, approval and tool-call ids are all random v4 and not
enumerable. Server-minted session keys are `secrets.token_hex(12)` — 96 bits. The one identifier
that is *not* reliably unguessable is a client-proposed session key, which is S1.

**What reaches a customer.** No error path puts internal state into an outbound message. Every
routed failure ends at `_handoff`, which sends one of two fixed sentences
(`support_core/engine/runners.py:995`, `:1009`) while the exception text goes to
`trace_step.error` and the handoff packet. `/healthz` reports a database failure as an exception
*type* only. `AwaitingSummary` deliberately tells the client which confirm node and tool are
waiting but never the argument hash. The web-chat payload model is `extra="forbid"`, and
`_handle_frame` overwrites `payload["session"]` with the connection's own key, so a socket
cannot speak for a conversation it did not open (`support_core/api/app.py:398-400`).

**No dynamic code execution.** No `eval`, `exec`, `pickle`, `marshal`, `subprocess`, `os.system`,
`__import__` or `compile` anywhere in `support_core/` or `packs/`. Every YAML read is
`safe_load`. The expression language is a real sandbox: attribute access is allow-listed to
declared pydantic fields or mapping keys, filters are a fixed table of four with literal-only
arguments, and depth, length and token caps are enforced at parse. Jinja runs in a
`SandboxedEnvironment` with globals, filters and tests cleared, calls and subscripts rejected at
load, and rendered output is never re-parsed. Pack code *is* arbitrary Python loaded from a
filesystem path (`support_core/tools/loading.py:39-66`), but that path comes only from operator
configuration or environment, never from a request — the boundary holds, and it means "install a
pack" is "run code as the service", which deployment guidance should say plainly.

**Secrets.** API keys are read from the environment and handed straight to the SDK client; they
are never logged, and cassettes record request and response objects only, with no headers.
`git grep` for the usual key shapes across tracked files found nothing; `docker-compose.yml` and
CI use the throwaway `support:support` local credential, and `.env` is git-ignored.

**Per-turn limits.** `max_nodes_per_turn`, `max_tool_calls_per_turn` and `max_node_errors` are
enforced in the turn loop and persisted in columns, so a crash does not reset them; the model
tool loop is bounded by `max_tool_iterations` and does not count a refusal once the budget is
spent. The drain worker coalesces per conversation, backs off exponentially and has a deadline.
None of these can be bypassed. What they do not do is bound anything *per conversation*, which
is S3.

*Note on the suite run:* 521 of 522 passed. The single failure,
`test_web_chat_socket.py::test_the_refund_conversation_runs_through_the_socket_with_the_confirmation`,
fails at `awaiting.kind == 'handoff'` where it expects `'question'`, **after** correctly asserting
the confirm node, the bound tool `issue_refund` and the £29.00 proposal. It is the phase-5
knowledge and citation work in flight in this tree, not a security regression, and it is the
other agent's to resolve.

---

## Owed to a later phase

Separating what the backlog already schedules from what it does not.

**Already scheduled, and correctly so — not findings:**

- Inbound guardrails: PII tagging, the prompt-injection flag, language routing (BACKLOG.md
  phase 7). DESIGN.md 14's inbound list is entirely unbuilt and `support_core/guardrails/
  __init__.py` says so honestly rather than stubbing it.
- Outbound guardrails other than the citation check: forbidden promises and the leakage scan
  (phase 7). The citation half is the phase-5 work in flight.
- Structured JSON logs with PII redaction before emission (phase 7). This is the redaction half
  of S4.
- The tracing, metrics and `conversation_replay` endpoint of DESIGN.md 15 (phase 7). There is no
  replay or debug route on the app today, which is why none appears in the routes table.
- The scheduler that calls `recover_stalled` and `sweep_timeouts` (phase 7). Its absence is what
  makes S2's orphaned `pending` rows permanent rather than merely delayed.
- Real operator accounts, rotation and an audit of who did what at the desk (phase 7) — S16.
- Cross-process fan-out of a desk reply to a customer's open socket (phase 7), which is the
  stated reason the desk stays on the customer's listener rather than its own.
- The OTP attempt cap in the sample pack (phase 4 finding R3), already recorded.

**Genuine gaps — not scheduled anywhere, and they should be:**

- **Conversation data retention.** DESIGN.md 20 requires it "configurable per pack". No code
  deletes conversation data and no backlog item schedules building it; BACKLOG.md mentions
  retention only as a precondition for evaluating mem0 in phase 10. This is the half of S4 that
  is not owed to phase 7 because nobody owes it yet.
- **The per-conversation cost cap.** `max_llm_cost_per_conversation_usd` is a manifest field that
  reads as implemented and is not (S3). Either implement it or mark it clearly as declared-only,
  because a limit that exists in configuration and not in code is worse than an absent one.
- **Rate limiting.** Phase W recorded "no rate limit" as open on the customer channel, which is
  right, but nothing schedules building it, and S2 shows it is not only an abuse concern — it is
  the difference between a service that degrades and one that stops.
- **Dependency pinning and a lockfile** (S9). No phase owns it.

---

*Database `secreview` was created for this review and dropped at the end. The demo servers on
8000 and 8001 were not touched; everything here ran on 8200. No file in the repository was
modified, and `reviews/scratch-security/` was deleted.*
