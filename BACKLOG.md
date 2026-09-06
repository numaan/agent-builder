# Backlog

Status values: `todo`, `in-progress`, `self-critique`, `in-review`, `resolving`, `done`, `blocked`.
Each phase follows the five-step workflow in [PLAN.md](PLAN.md). Design references point at [DESIGN.md](DESIGN.md).

| Phase | Title | Status | Review file |
|-------|-------|--------|-------------|
| 0 | Skeleton, tooling, database | done | reviews/phase-0.md |
| 1 | Graph model, loader, validator, expression language | done | reviews/phase-1.md |
| 2 | Execution engine and durability | done | reviews/phase-2.md |
| 3 | LLM layer and prompted nodes | done | reviews/phase-3.md |
| 4 | Tool runtime and safety nodes | done | reviews/phase-4.md |
| W | Web chat slice (pulled forward from 7) | done | reviews/phase-w.md |
| 5 | Knowledge layer and citations | todo | reviews/phase-5.md |
| 6 | Interrupts, root graph, handoff | done | reviews/phase-6.md |
| 7 | Channels, observability, replay | todo | reviews/phase-7.md |
| 8 | Evaluation harness | todo | reviews/phase-8.md |
| 9 | Knowledge graph, customer memory, cost controls, packaging | todo | reviews/phase-9.md |
| 10 | Evaluate mem0 for customer memory (after 8) | todo | reviews/phase-10.md |
| 12 | `support pack` — authoring tool, with DSPy tuning | todo | reviews/phase-12.md |
| F | Final integration review | todo | reviews/final.md |

---

## Phase 0: Skeleton, tooling, database

Design: sections 4.1, 17, 18.

- [x] `pyproject.toml` with dependencies from PLAN.md conventions; `ruff` and `mypy` configured.
- [x] `support_core/` package with the module directories from section 18, each with a docstring stub.
- [x] `docker-compose.yml` with Postgres 16 and pgvector; `make db-up` or equivalent script (`scripts/db-up.sh`).
- [x] Alembic configured; initial migration creating every table in section 17.
- [x] `support` CLI entry point with `pack validate` stub.
- [x] `packs/acme_billing/` with `pack.yaml`, empty `persona.md`, `policies.md`, `graphs/`, `tools/__init__.py`, `knowledge/`, `evals/`.
- [x] `tests/conftest.py` providing an async Postgres session fixture with per-test schema reset.
- [x] CI config (GitHub Actions) running ruff, mypy, pytest with a Postgres service. (Triggers on push to `main` or `master` and on pull requests; also runs `alembic check` and `support pack validate`. It has not yet executed on GitHub because nothing has been pushed.)

Exit criterion: `support pack validate packs/acme_billing` runs and reports the pack is empty but well-formed; `pytest` runs one database smoke test green. Met locally on 2026-09-05 and re-confirmed after review resolution (77 tests green, see reviews/phase-0.md "Resolution"). *Superseded by phase 3*, whose own exit criterion requires a `root.yaml` in that pack: it now validates **with** graphs, and the CLI contract those tests protected is asserted in `tests/test_pack_validate.py` as it stands.

## Phase 1: Graph model, loader, validator, expression language

Design: sections 5.2, 6.1 to 6.4, 6.7.

- [x] Pydantic schema for graph files: id, description, inputs, outputs, state, start, nodes, edges. (`support_core/graph/schema.py`; edges live inside nodes as DESIGN.md 6.4 writes them. `support_core/graph/types.py` turns declared type strings into real Pydantic models without `eval`.)
- [x] Node type registry with core types `router`, `say`, `end`, `subgraph`; `llm`, `ask`, `tool`, `gate`, `confirm`, `handoff` registered as declared-but-not-executable stubs so the validator can check them now. (`support_core/graph/nodes.py`; each spec records `executable_phase`.)
- [x] Sandboxed expression language: attribute access on `state`, `ctx`, `result`; comparisons; `and`, `or`, `not`; literals; a fixed filter set (`money`, `lower`, `len`, `default`). Parser plus evaluator plus type inference against Pydantic models. No `eval`. (`support_core/graph/expr/`: lexer, recursive-descent parser, evaluator, static type checker; hypothesis property tests.)
- [x] Jinja2 templates for `say`, `ask`, and `confirm` prompts with a sandboxed environment. (`support_core/graph/templates.py`; templates are translated into the expression AST and type-checked at load time.)
- [x] Loader: `load_pack(path)` reads `pack.yaml`, all graphs, persona, policies; resolves sub-graph references. (`support_core/graph/loader.py`, exported as `support_core.load_pack`.)
- [x] Validator implementing every rule in section 5.2, including the confirm-on-all-paths rule (implemented now against declared tool risk tiers, tools may be stubs). (`support_core/graph/rules.py`; risk tiers come from a declarative `tools/tools.yaml`, which phase 4 replaces with the real registry.)
- [x] Graph version pinning data structure (section 6.7) recorded on the pack object. (`support_core/graph/pack.py`: `PackPin` with a content hash and a state-shape hash per graph. Data only, no run-time behaviour.)
- [x] `support pack validate` prints findings with file and node locations. (`Finding.node`; rendered as `[graphs/refund.yaml:issue_refund]`.)
- [x] Tests: expression language property tests, validator tests with one failing fixture per rule, loader round-trip.

Exit criterion: a deterministic graph using only `router`, `say`, `subgraph`, `end` executes in a unit test through a minimal in-memory stepper, and the validator rejects each malformed fixture with the right rule name. Met on 2026-09-05: `tests/test_stepper.py` runs `tests/packs/deterministic_pack` through `tests/stepper.py` (a test utility, not the engine; phase 2 owns that), and `tests/test_graph_validator.py` asserts a rule id per malformed fixture, now for every ERROR rule id in `rules.py` with a test that keeps it that way (review finding F2). 389 tests green after review resolution; `tests/packs/refund_pack` (the DESIGN.md 6.4 refund workflow) validates with no errors. Resolution recorded in reviews/phase-1.md.

## Phase 2: Execution engine and durability

Design: sections 6.3, 7.1 to 7.3, 17.

- [x] `Run`, `Frame`, `SuspendReason`, `ResumeEvent`, `NodeResult`, `NodeRuntime` types. (`support_core/engine/types.py` and `runners.py`; DESIGN.md 6.3 verbatim where it gives a shape. `Frame.kind` is `root | subgraph | gate_redirect | interrupt` from the first commit, so phase 6's interrupt stack needs no migration of stored frames.)
- [x] Executor: turn loop from section 7.1 without the LLM-dependent parts (interrupt check is a pluggable hook that defaults to `continue`). (`support_core/engine/executor.py`; every deferred piece is a hook on `EngineHooks` with a default that does nothing surprising.)
- [x] Frame stack push, pop, output mapping, and gate re-evaluation on frame entry. (One `_push` for every reason a frame appears. `tests/test_engine_frames.py` shows a gate cannot be walked around by suspending after it and letting the precondition lapse.)
- [x] Checkpoint after every node in one transaction with the `trace_step` row; deterministic step ids. (`storage/repositories.write_checkpoint` writes the run row, the trace step and the node's outbound messages together. The step id is `run_id:frame_seq:node_id:attempt`, every field taken from durable state.)
- [x] `trace_step.seq` (the run's `checkpoint_seq` at write time) with a unique `(run_id, seq)` so replay has a total order per run; the executor sets `started_at` from the application clock, not the `now()` default (phase 0 deferred finding F5). (Migration `0002_engine_durability`, which backfills `seq` for existing rows and replaces the `started_at` index.)
- [x] Postgres advisory lock per conversation; pending-message queue for messages that arrive while locked. (`support_core/engine/locks.py`, on a connection of its own so the turn's many checkpoint transactions can commit inside it and a dead process releases the lock by dying.)
- [x] Suspend and resume for `waiting_customer`, `waiting_human`, `waiting_async_tool`, `waiting_timer`; per-status timeouts. (`pack.yaml` gains a `timeouts:` block with per-channel overrides. Each status is resumed on a different `Executor` object from the one that suspended.)
- [x] Per-turn limits (`max_nodes_per_turn`) with handoff fallback hook. (Counted in `run.turn_nodes`, so the limit survives a crash mid-turn rather than resetting.)
- [x] Crash recovery: re-execute the current step on resume; replay completed steps from trace. (A committed checkpoint has already advanced the stack, so a completed step is never re-run; an uncommitted one re-runs under the same step id. `run.awaiting.turn_event` holds the message that drove the turn until a node consumes it.)
- [x] Hot reload and `PackPin` coherence: `load_pack` reads and parses every graph twice, so a pack edited on disk mid-load can produce a pin describing a mix of two versions; snapshot the directory or hash the bytes actually parsed (phase 1 deferred finding P1). (The validator records the text it read on `report.graph_sources` and the loader parses that snapshot; `tests/test_pack_coherence.py` counts the reads.)
- [x] Make `support_core.graph.templates.ENVIRONMENT` per-pack before anything varies per pack or two pack versions are loaded side by side (phase 1 deferred finding P2). (`Pack.environment`; the validator builds one per run; the module-level environment is only a fallback for callers with no pack.)
- [x] Tests: kill-and-resume test that interrupts the executor between checkpoint and advance and verifies identical outcome; concurrent inbound test verifying single-writer ordering. (`tests/test_engine_durability.py` kills at four points in the loop at four different nodes, plus a real `os._exit` in a separate OS process; `tests/test_engine_concurrency.py` uses two OS processes on two connections, one slow while holding the lock.)
- [x] Claiming a customer message and starting the turn that consumes it are one transaction, and the node a resume event is delivered to comes from durable state rather than from the stack top (review findings R1, R2). `tests/verify_phase_2_resolution.py` re-runs the reviewer's whole crash and concurrency log, including its 28-case suspend-and-resume matrix, and is a CI step of its own.

Exit criterion: the kill-and-resume test passes under Postgres, and two concurrent inbound messages are processed in order with no lost state. Met on 2026-09-06 after review resolution: 477 tests green, including twenty-eight kill-and-resume combinations that each reproduce a byte-equal trace, frame stack, status and transcript, five more that kill during a gate re-check, two that kill in the claim window, and `test_two_processes_send_at_once_and_the_turns_run_in_order`, which reads the ordering out of the customer's transcript rather than out of timing. The reviewer judged the criterion only partly met before resolution, because a crash during a gate re-check or in the claim window lost or misrouted the customer's message; both are fixed with regression tests that fail against the reviewed code. `tests/stepper.py` is deleted: the executor replaced it, and `tests/test_engine_graph.py` carries the phase-1 exit criterion's assertions against the real engine. Resolution recorded in reviews/phase-2.md.

## Phase 3: LLM layer and prompted nodes

Design: sections 11.1 to 11.3, 6.2 (`llm`, `ask`).

- [x] `LLMProvider` protocol; `AnthropicProvider` using tool-use for structured output and prompt caching for the static prefix; `FakeProvider` that replays recorded responses keyed by prompt hash for tests. (`support_core/llm/`. `structured` is implemented once over `complete` for every provider, so the schema is enforced on the way back whatever the provider did on the way out. **The AnthropicProvider has never been called**: there is no `ANTHROPIC_API_KEY` in this environment. Its payload, its parsing and its error mapping are unit-tested; the one test that would reach the API is the opt-in `live` group.)
- [x] Prompt assembly with the fixed nine-layer order and per-layer token budgets; packs cannot reorder. (`support_core/llm/prompt.py`. Enforced structurally: layer 1 is a module constant no argument reaches, callers fill *slots* so there is no order to supply, and every pack- or customer-supplied string is neutralised so it cannot forge a section marker or a data fence. Layers 1 to 5 fail closed over budget rather than dropping a policy line; 6 to 9 truncate visibly.)
- [x] `LlmNodeOutput` schema; decision constrained to the node's edge labels; confidence threshold routing to `unclear`. (`decision` is a `Literal` over the node's declared edges in the schema the provider is given *and* in the model the answer is validated against. Below `llm.confidence_threshold` the node's `unclear` edge is taken, or - if it declares none - a human, because a node that gave the model no way to be unsure has not thereby made the guess safe.)
- [x] `llm` node with bounded READ tool loop hook (tool execution itself arrives in phase 4; here the loop calls a stub runtime). (`support_core/llm/tool_loop.py`. The node never holds the runner: it holds a `ReadOnlyToolGateway` built by `NodeRuntime.tool_gateway` around the node's own `tools:` list, which refuses anything not declared, not READ, or over budget before the runner is spoken to. Phase 4 supplies the runner and cannot opt out of the gateway.)
- [x] `ask` node: suspend, then slot extraction via structured output on resume. (The phase-2 node is unchanged; its `extract_slots` hook is replaced by `StructuredSlotExtractor`, as reviews/phase-2.md required. The hook's signature grew into a `SlotRequest` because a structured schema needs each slot's declared *type*, which a list of names cannot carry.)
- [x] Conversation summary memory (section 10) updated every K turns. (Migration `0004` adds `conversation.turn_count`, incremented in the transaction that starts the turn, and `summary_turn`, written with the summary. Nothing a turn depends on is read from either: `tests/test_memory_summary.py` runs a conversation twice, once with the summary destroyed between turns, and requires an identical trace, frame stack and transcript while proving the prompts differed.)
- [x] Tests with `FakeProvider`: llm node picks only allowed edges; malformed model output is retried once then routed to handoff hook; ask node fills slots. (Plus the injection suite: persona, policies, node instructions, edge descriptions, state values, tool results, passages and conversation history each attempt to forge prompt structure, and the assertion is that every line that reads as structure is one core wrote.)

- [x] The prompt boundary is enforced by an unforgeable delimiter rather than by detecting forgeries, and an `llm` node with no `output_schema` may write no state at all (review findings V1, V2). `tests/test_prompt_injection_matrix.py` and `tests/test_llm_decision_matrix.py` are the reviewer's two adversarial matrices, kept as tests: 0 of 420 renderings forge structure, and 25 of 25 hostile decisions are refused.

Exit criterion: `packs/acme_billing` has a `root.yaml` with a classify node and a `small_talk` path, and a scripted conversation runs through it with the fake provider. Met on 2026-09-06 after review resolution: 1057 tests green (two deselected - the `live` group), `tests/test_golden_conversation.py` replays four scenarios - one per edge the classify node declares - through `root.yaml` against `FakeProvider`, which refuses anything whose request fingerprint it has not recorded, and a further test re-records each scenario and requires the committed cassette back byte for byte. The reviewer judged the criterion met but broke two of the phase's headline claims: a zero-width space forged a data fence from a plain customer message, and a node with no `output_schema` let the model write any state field at any type, including the graph's own `outcome`. Both are fixed with regression tests that fail against the reviewed code. The sample pack is no longer "empty but well-formed": it validates *with* graphs, and the three phase-0 tests that pinned that wording now assert the CLI contract they were protecting. Resolution recorded in reviews/phase-3.md.

## Phase 4: Tool runtime and safety nodes

Design: sections 8.1 to 8.4, 6.2 (`tool`, `confirm`, `gate`), 6.4.

- [x] `Tool`, `Risk`, `ToolContext`; registry with duplicate and schema checks. (`support_core/tools/base.py`, `registry.py`; `FunctionTool` wraps a coroutine for packs that would rather not subclass. `ToolContext.patch_customer` is how DESIGN.md 19 step 9's `verify_otp` sets `ctx.customer.identity_verified` without a node writing `ctx`.)
- [x] Risk policy enforcement in the runtime, not in prompts: READ only from llm loops, WRITE and HIGH only from tool nodes with approval, `confirm_exempt` reporting. (`support_core/tools/runtime.py`. The tier check is made twice - once in phase 3's gateway, once in the runtime - so a bug in the gateway is not sufficient to execute a write, and since review finding R2 the node's declared tool list is checked in both places too.)
- [x] Idempotency: `tool_call` row keyed by step id; at-most-once for non-idempotent tools. (The row is claimed *before* the call, so a dead process leaves a `running` row - a call whose outcome nobody knows - and a non-idempotent tool is then refused rather than repeated. `tests/test_tool_crash_recovery.py` kills a real OS process inside the tool, after the side effect and before the record.)
- [x] Migration adding `run_id` (FK, indexed) and `step_id` to `tool_call` so calls can be joined to a run and conversation without parsing the idempotency key (phase 0 deferred finding F6). (Migration `0006`, which also adds `node_id`, `error`, `attempts`, `finished_at` and `context_patch`.)
- [x] `confirm` node computing `sha256(tool_name + canonical_json(args))`, storing `action_approval`, yes/no edges. (`ConfirmRunner`. The hash is taken when the proposal is *shown* and travels with the suspension, so the customer's answer is read against what they were asked; a difference re-presents rather than approving. The yes/no reading is the `confirm_decision` hook, with a conservative keyword default and `StructuredConfirmClassifier` where a provider is configured - and a third answer, `unclear`, which asks again.)
- [x] `action_approval` is single-use: `consumed_by_tool_call_id`/`consumed_at`, and a consumed approval is treated as absent; covered by the adversarial tests (phase 0 deferred finding N1). (Also bound to the run, the frame and the confirm node, so an approval from an earlier invocation of the same graph is refused before single use has to catch it.)
- [x] `tool` node with `args` expressions, `into` mapping, `on_error`, `requires_approval` hash check. (`ToolRunner`; both `into` forms, and a refusal or a failure is a `NodeError` with a distinguishable `reason`.)
- [x] `gate` node pushing the redirect graph and re-evaluating. (Executable since phase 2; `tests/test_adversarial_approvals.py` shows the re-check stopping a refund whose identity stopped holding while the confirmation was suspended.)
- [x] Async tools completing via callback (`waiting_async_tool`). (`Tool.async_`; the node suspends after the dispatch and `Executor.resume_async_tool` completes the row. The dispatch's idempotency key travels on the suspension, because the step id computed when the callback arrives names a later attempt - review finding R1, and the reason the path had never completed end to end. `tests/test_async_tool_flow.py` drives it through the executor.)
- [x] MCP adapter with mandatory risk map, unknown tools default HIGH. (`support_core/tools/mcp.py`, over a client protocol the `mcp` package's `ClientSession` satisfies; a wrapped tool is an ordinary `Tool`, so the same registry, approval binding and idempotency key apply.)
- [x] Sample pack tools: `list_recent_charges`, `get_charge`, `check_refund_eligibility`, `issue_refund`, `send_otp`, `verify_otp`, backed by an in-memory fake billing system. (`packs/acme_billing/tools/`. `issue_refund` is HIGH and *not* idempotent, which is the honest setting for money. `verify_otp` gives the customer three wrong guesses and then stops checking, because a six-digit code with unlimited attempts is not a verification and it is what opens the refund gate - review finding R3.)
- [x] `verify_identity.yaml` and `refund.yaml` graphs from section 6.4. (In `packs/acme_billing/graphs/`, reachable from `root.yaml`'s new `refund` edge.)
- [x] The registry built from the imported `TOOLS` is authoritative for risk tiers; the validator reports drift against `tools/tools.yaml` (phase 1 deferred finding I: a HIGH tool declared `read` removes every check today). (`support_core/graph/tools_source.py`: a tier that disagrees is `tools.registry_drift` (ERROR), a shape that disagrees `tools.declaration_stale` (WARNING), and a pack that declares tools but exports none `tools.declared_not_exported` (WARNING).)
- [x] The engine's approval hash classifies scalars with the same `parse_value` rule the validator canonicalises with, so a static pass is not followed by a run-time refusal (phase 1 review F7, run-time half). (Both nodes evaluate their arguments through `value_of`, which is `parse_value`, and then coerce through the tool's own input model - so `{amount: 100}` and `{amount: 100.0}` are the same call and `{amount: "100"}` is not.)
- [x] Reject a state write, between a `confirm` and the call it approves, to any field the approved arguments read; today the comparison is textual and identical text that evaluates differently passes (phase 1 deferred finding H). (`graph.approval_args_mutated`.)
- [x] Decide whether `confirm_exempt` on a WRITE tool stays an INFO or gains a required reason string now that the registry is authoritative (phase 1 deferred finding J). (Decided: a required `confirm_exempt_reason`, and the report is a WARNING so `--strict` fails until a human has looked. See the Decisions log.)
- [x] Adversarial tests: approval hash mismatch refused; WRITE tool from llm loop refused; gate bypass by direct sub-graph entry refused; confirmed amount changed before execution refused. (`tests/test_adversarial_approvals.py`, plus a replayed approval, two concurrent turns racing one approval, and a pack-registered node type trying to forge an approval, invoke a tool, or declare the customer verified. Each was mutation-tested: switching the enforcement off makes the test fail.)

Exit criterion: adversarial approval tests pass and the refund graph runs with the fake provider through confirm and issue_refund. Met on 2026-09-06: `tests/test_golden_conversation.py` replays a five-turn refund conversation through `packs/acme_billing` against `FakeProvider`, and `tests/test_refund_flow.py` asserts what the path cannot - one approval, bound to the arguments the customer was shown, consumed by the call it authorised, and one refund in the billing system rather than none or two. Met again after review resolution: 1186 tests green (two deselected - the `live` group). The reviewer judged the criterion met and the money safe - twenty-five approval-bypass attacks, none of which moved anything - but broke the async tool: a callback could never find the call it belonged to, and on a graph whose `on_error` returned to the tool node that made one customer intent produce **two** side effects. Fixed, with a regression test that fails against the reviewed code. The reviewer's two attack matrices are now tests rather than a deleted scratch directory (`tests/test_approval_bypass_matrix.py`, `tests/test_double_payment_matrix.py`), and every one of the fourteen enforcements this phase has was mutation-tested again after the changes. Resolution recorded in reviews/phase-4.md.

## Phase W: Web chat slice (pulled forward from Phase 7)

Design: sections 12, 4.1. Pulled forward on 2026-09-06 so there is a visible, demonstrable
conversation before the remaining depth is built. Scope is deliberately narrow: only what a
person needs to type at the agent in a browser and watch the refund flow work.

- [x] `ChannelAdapter` protocol per DESIGN.md section 12 (`parse_inbound`, `send`, `conversation_key`), general enough that Phase 7's email adapter implements it without changing it. (`support_core/channels/base.py`; the three methods and nothing else, pinned by a test. `send` takes a frozen `ConversationRef` rather than the ORM row - the one deliberate change, in the decisions log. Migration `0007` adds `conversation.channel_key`, unique per channel, which is what makes a mail thread and a chat session the same mechanism.)
- [x] Web chat adapter over WebSocket, streaming the final message only; intermediate node output is not streamed. (`support_core/channels/web_chat.py`. What reaches a socket is a *committed* message: the engine calls `send` after the checkpoint, so there is no path by which unsent node text reaches a customer, and no token streaming from the model - there must not be one until outbound guardrails run before delivery.)
- [x] FastAPI app factory `create_app(pack)` wiring the web chat channel and a health endpoint. The desk API and email webhook stay in Phase 7. (`support_core/api/`, with `AppConfig` choosing the pack, the provider - recorded cassettes or the live model, by configuration - and the `ctx` a new conversation starts with. Root `app.py` is DESIGN.md 4.1's file verbatim.)
- [x] Queue-and-return mode for the webhook path (`lock_wait_seconds=0` plus a caller of `Executor.drain`) so an HTTP handler never blocks a connection per waiter. This is half of phase-2 review finding R7; the scheduler half stays in Phase 7. (`support_core/api/drain.py`. Measured: a second message on a conversation whose turn holds the lock for 1.5 s is answered in milliseconds with `queued`, and the drain worker runs it afterwards, in order.)
- [x] A minimal static client page good enough to demonstrate a conversation. Not a product UI. (`support_core/api/static/`: HTML, CSS and JavaScript, no build step, no framework and no request to anything but the service. The confirmation is a bordered panel naming the tool and quoting the proposal the approval is bound to.)
- [x] Tests: web chat round trip against the sample pack; a suspend and resume across two WebSocket connections; concurrent clients on different conversations do not serialise. (`tests/test_web_chat_socket.py`, `tests/test_web_chat_api.py`, `tests/test_channels.py`, `tests/test_drain_queue.py`, `tests/test_app_config.py`: 71 tests over a real uvicorn server on an ephemeral port, driven over TCP.)
- [x] The desk API is off unless a deployment turns it on, and needs a bearer token when it is; a missing credential refuses to start (review finding W1, a must-fix, and the one that mattered because the demo was running on this build). `tests/test_desk_auth.py` is the reviewer's own attack as eight regression tests, all of which fail against the reviewed code.
- [x] A binary WebSocket frame is refused with an error frame and close 1003 rather than crashing the handler with an unhandled `KeyError` (review finding W2, a must-fix), with the reviewer's exact input as a regression test.
- [x] The `turn` frame - the only thing that raises the approval panel - reaches every connection on the conversation rather than only the one that sent the message (review finding W3), so a second tab sees the confirmation.
- [x] The pending inbound queue is ordered by `message.queue_seq`, claimed from `conversation.inbound_seq` under the conversation's row lock, rather than by a transaction-start timestamp with a random tie-break (review finding W4, migration `0009`). A reorder that dead-ends a conversation is the family of bug phase 2's R1 and R2 were, and it is fixed in durable state for the same reason.

Exit criterion: `create_app(load_pack("packs/acme_billing"))` starts, and a browser client
completes the phase-4 refund flow end to end, including the confirmation step, with the
recorded provider. With `ANTHROPIC_API_KEY` set, the same flow runs against the live model.
Met on 2026-09-06, twice over: `tests/test_web_chat_socket.py` drives the four-message refund
conversation through the real HTTP and WebSocket path and asserts one refund in the billing
system authorised by one consumed approval, and the same conversation was run by hand in a
browser against `SUPPORT_APP_CONFIG=demo/acme_web_chat.json` (reviews/phase-w.md records what
was on the screen). **The live half of the criterion is unproven**: there is no
`ANTHROPIC_API_KEY` in this environment, so what is demonstrated is that the provider is chosen
by configuration and that `provider: "auto"` resolves to `anthropic` when a key is present -
not that a live turn works. That is the same gap phase 3 recorded for `AnthropicProvider`, and
it stays open until somebody runs it with a key.

Met again after review resolution on 2026-09-07: 1374 tests green (two deselected - the `live`
group), and the four-message conversation driven by hand once more, over a real socket against a
private database, reaching the confirmation and the refund. The reviewer judged the channel
itself sound - every attack they made on the channel boundary held, and the exit criterion
reproduced independently - and broke what had been mounted *beside* it: phase 6's desk API,
served on the customer's own port, on by default and with no authentication, so an anonymous
browser listed every conversation in the deployment, read another customer's transcript and
handoff packet, and wrote into it. That is fixed three ways over - off by default, a credential
required to turn it on, and a missing credential refusing to start - with eight regression tests
that reproduce the reviewer's own attack and fail against the reviewed code. A binary WebSocket
frame, which crashed the connection handler with an unhandled `KeyError`, is refused cleanly with
its own regression test. Nine should-fixes and three nits went with them, including two the
demo depends on: only the tab that sent a message learned the conversation's status, so a second
tab never raised the approval panel; and two messages posted at the same instant could run in
the wrong order and park the conversation for good, which is now decided by a claimed
per-conversation sequence rather than by a transaction-start timestamp with a random tie-break.
Resolution recorded in reviews/phase-w.md.

## Phase 5: Knowledge layer and citations

Design: sections 9.1 to 9.3, 14 (citation guardrail).

- [ ] `Passage`, `Retriever` protocol, `CompositeRetriever`.
- [ ] `DocumentRetriever`: chunking by headings, embeddings via provider abstraction (fake embedder in tests), pgvector plus tsvector hybrid search, optional model reranking.
- [ ] **`ColbertRetriever` as a second `Retriever` implementation** (added 2026-09-06 at the user's request). Late interaction: one vector per token, scored by MaxSim, rather than one vector per chunk. Two reasons it belongs here rather than later. It runs locally, so it removes this phase's worst limitation - there is no embedding API in this deployment (no Anthropic key; the GLM endpoint is Anthropic-compatible and serves no embeddings), so without it the dense path ships with its retrieval quality unmeasured against a stand-in embedder. And late interaction is strongest on exactly this corpus shape: short policy passages where the answer turns on a phrase.
  - [ ] **Host it in Qdrant** (decided 2026-09-06 after the user raised it). Qdrant stores multivectors and scores MaxSim natively, so late interaction becomes an ordinary query against an ordinary service instead of a PLAID/FAISS index directory the deployment has to build, ship, version and back up by itself. The two alternatives were considered and rejected: ColBERT's own index makes the corpus a second artefact with none of a database's operational affordances, and MaxSim in SQL over pgvector is honest but too slow for a 4-second p95 turn.
  - [ ] Note what this does *not* break. DESIGN.md 7.1's rule is that the frame stack and the trace step are written in one transaction and resume derives from durable state; retrieval is a read outside that transaction, so a second store here cannot cost a checkpoint or a resume. That is the distinction from phase 10's mem0 question, where per-customer notes would sit in the write path.
  - [ ] Split the two halves deliberately: Qdrant owns the vector side (dense and ColBERT multivector), Postgres full-text owns the lexical side. `CompositeRetriever` merges them. This buys graceful degradation - with Qdrant unavailable the lexical half still answers, and a total retrieval failure already routes to handoff through the citation guardrail rather than to a guess.
  - [ ] Version by collection, not in place: a sync builds `<source>_v<n>` and flips an alias. Old and new genuinely coexist, which is what makes the exit criterion's "an old trace still names the old version" true rather than approximately true.
  - [ ] Add Qdrant to `docker-compose.yml` beside Postgres, and to CI. Accept the operational cost explicitly: one deployment per domain (Q12) means one Qdrant per domain, so this is a container, a backup and an upgrade path multiplied by the number of domains served.
  - [ ] `source_version` must survive it. The exit criterion is that a wrong answer names the revision that caused it, so the index is versioned with the corpus and a re-sync builds a new one rather than mutating in place.
  - [ ] Keep it behind the same `Retriever` protocol and the same `CompositeRetriever`, so a pack picks its backend in `sources.yaml` and neither the engine nor a graph knows which is in use.
  - [ ] Measure it against the pgvector path on the same corpus before making it the default. Two retrievers with no comparison between them is worse than one.
  - [ ] Passages still pass through phase 3's per-render delimiter token. A ColBERT passage is untrusted text like any other.
- [ ] Migration fixing `doc_chunk.embedding` to `vector(N)` for the chosen embedding model (while the table is empty) and adding the HNSW index; decide whether downgrade should keep the `vector` extension (phase 0 deferred findings N12, N2).
- [ ] `LiveLookupRetriever` routing to READ tools.
- [ ] Thread a retriever through `handoff/builder.py`'s `gather()` and `HandoffSummaryRequest`
      so a handoff summary can be grounded and `HandoffPacket.citations` stops being empty.
      The shape is already there - `citations` is on the packet and `Passage` is defined - and
      naming it now stops phase 5 inventing a second one (phase 6 review, forward compatibility).
- [ ] Ingestion CLI `support pack knowledge sync` for `markdown_dir` and `html_crawl` sources, with `source_version` and stale-chunk marking.
- [ ] `knowledge:` block on llm nodes; passages inserted as delimited data with ids.
- [ ] Outbound citation guardrail: factual-claim classifier (rule-based first), re-prompt once, then handoff.
- [ ] Sample pack knowledge: refund policy and processing-time documents.
- [ ] Test: change a policy document, re-sync, and show the trace of a new answer cites the new `source_version` while an old trace still names the old one.

Exit criterion: the wrong-answer-to-source-version trace test passes.

## Phase 6: Interrupts, root graph, handoff

Design: sections 6.5, 6.6, 13, 7.3.

- [x] Interrupt check as a structured LLM call producing `continue`, `new_intent`, `cancel`, `unclear`; honours `interrupts.allowed_from` and `blocked_in`; records secondary intents.
- [x] Return-to-interrupted-workflow prompt after the interrupting workflow ends.
- [x] Root graph pattern in the sample pack with `refund`, `update_address`, `small_talk`, `unknown`, `handoff`.
- [x] `HandoffPacket` builder using the escalation model; `handoff` node; `HandoffSink` protocol with webhook and Postgres queue sinks.
- [x] Desk API: list handoffs, reply, resume with state patch, close. Plus `approve`, which is what makes phase 4's `requires_human_approval` satisfiable, and a transcript endpoint, so `packet.transcript_url` is a real link rather than a plausible one.
- [x] All failure paths from section 7.3 route to handoff with the right reason.
- [x] Revisit the confirm-coverage control-flow graph for interrupts: model the interrupt push and the return-and-resume, and make `graph.subgraph_cycle` check the cycle path rather than the whole graph (phase 1 deferred finding N3).
- [x] Report the *arguments* a `confirm_exempt` tool's nodes pass it in `graph.confirm_exempt`, so a reviewer sees a model-written address differently from `ctx.customer.email` (phase 4 resolution). Needs the cross-graph model above, because a gate redirect can reach such a node from another graph.
- [x] An `on_error` edge that returns to its own `tool` node repeats the call under a fresh idempotency key. Harmless for a tool with an approval (spent) and bounded for a non-idempotent one, but a `confirm_exempt` WRITE tool repeats its side effect once per failure and the validator says nothing (phase 4 resolution, the shape behind finding R1). Decide whether it is a load-time finding, a per-node attempt cap, or both, while rebuilding the failure routing of DESIGN.md 7.3. **Both**: `graph.on_error_repeats_side_effect` (WARNING, because re-sending a passcode is this shape and is right) and `limits.max_node_errors`, counted per node per frame so it survives a crash and leaves ordinary loops alone.
- [x] `update_address.yaml` graph in the sample pack. (Landed with the address workflow before this phase; the interrupt paths that make it demonstrable are phase 6's.)

Exit criterion: the section 19 worked example runs end to end with the fake provider as an integration test, including the interrupt at step 7 and the secondary intent at step 15. **Met**: `tests/cassettes/scenarios.py::ACME_INTERRUPT_DEFERRED`, replayed by `tests/test_golden_conversation.py`, is that conversation - the interrupt is refused by `verify_identity` and recorded, and four turns later the root graph's classifier is shown it and chooses `update_address`. Met again after review resolution on 2026-09-06: 1361 tests green (two deselected - the `live` group), and the reviewer's two attack matrices re-run with 19 of 19 attempts held, up from 16. The reviewer judged the interrupt machinery sound - no route they tried reached a guarded action - and broke the *desk*: an unvalidated `resume` state patch wrote an undeclared field into durable frame state, bricked the conversation and blamed the pack for it, on an endpoint with no authentication. Fixed, with three regression tests that fail against the reviewed code. Two more of the phase's own claims went with it: a handoff told the customer a specialist had their question when no sink had taken the packet (DESIGN.md 14's forbidden promise, on the path this phase built to remove one), and the interrupt check's confidence was collected, carried through three layers and read by nobody, so a `cancel` at 0.0 unwound the whole frame stack. DESIGN.md 7.3 is amended to two failure tiers rather than recording the missing middle one for a fourth phase. Resolution recorded in reviews/phase-6.md.

## Phase 7: Channels, observability, replay

Design: sections 12, 15, 4.1.

- [ ] Email adapter mapping `In-Reply-To` to conversations and batching outbound per turn. (The `ChannelAdapter` protocol, the web chat adapter and `create_app` land in Phase W; this phase adds email on top of them.)
- [ ] Extend `create_app` from Phase W with the desk API and the email webhook.
- [ ] A queue-and-return mode for channel webhooks: `lock_wait_seconds=0` plus something that calls `Executor.drain`, made the default for the webhook path, and a scheduler for `recover_stalled` and `sweep_timeouts` (phase 2 review finding R7). Both mechanisms exist; what is missing is a caller that cannot afford to block a connection per waiter, which is the HTTP handler this phase adds.
- [ ] OpenTelemetry spans for turn, node, llm_call, tool_call, retrieval, guardrail with the attributes in section 15.
- [ ] Metrics listed in section 15. Two model calls phase 6 added are made *outside* any node -
      the interrupt check and the handoff summary - so section 15's `llm_call` span has no `node`
      parent for either and the two most-often-run and most-expensive new calls will not appear
      in the per-node attribution. Give them a `turn -> llm_call` span of their own.
- [ ] A durable record of a handoff nobody took, and a sweep for it (phase 6 review finding P2,
      the half phase 6 could not close). Today a sink that refuses is recorded in
      `HandoffService.failures`, a per-process list nothing sweeps: the customer is told the
      truth and the run is parked, but no queue row exists and no second replica can see it.
      A `handoff.status = 'undelivered'` row written before delivery and resolved after, plus a
      retry in the scheduler this phase adds, is the shape.
- [ ] A cheaper model for the interrupt check (`llm.interrupt_model`). DESIGN.md 20 asks for it
      and phase 6 could not give it one, because the per-node `model:` override exists and the
      interrupt check is not a node - and it runs on *every* reply to a suspended workflow,
      inside the conversation lock, before the claim (phase 6 review, forward compatibility).
- [ ] Structured JSON logs with PII redaction applied before emission.
- [ ] `conversation_replay` endpoint rendering the frame stack over time. It must resolve the
      reserved node id `__interrupt_return__` (phase 6's return offer, with the core-only edges
      `offer`, `resumed` and `abandoned`), which no graph declares, rather than discovering it
      against a graph that has no such node (phase 6 review finding P10).
- [ ] Replay an LLM call from the trace when a step re-executes (DESIGN.md 7.3: "LLM calls replay from the trace if the step already completed"). `trace_step.llm_response` is written and never read back, so a crash between the model answering and the checkpoint committing pays for the question again and may get a different answer. The step id on `NodeRuntime` is the documented cache key (phase 3 self-critique fragility item 1, endorsed by the phase-3 review).
- [ ] Inbound guardrails: PII tagging and redaction in traces, injection flag, language detection.
- [ ] Authentication and abuse control on the **customer** channel surface: the web chat session key is still the whole of the access control, and there is no WebSocket origin check, no CSRF defence on the webhook, no rate limit and no webhook signature verification (phase W self-critique, sharpened by its review's attempts A2 and A11). The desk half of this is **closed**: phase W's resolution turned `serve_desk` off by default and put every `/desk` route behind a bearer token, because phase 6 had mounted that API on the customer's own listener, on by default and open (finding W1). What is left here is the customer's own key and the abuse controls, plus per-operator desk identity and rotation, which one shared token is not.
- [ ] Take the desk off the customer's listener, once live delivery can cross a process. Phase W's resolution kept it on the same application deliberately: DESIGN.md 4.1 says one service, 12 says the same API surface, and a desk `reply` reaches a customer's open socket only from the process that holds that socket, so a separate desk process would silently stop delivering a human's reply until the fan-out below exists. With the fan-out, a `create_desk_app` on its own port costs nothing and matches what section 12 means by "not a customer channel".
- [ ] Live delivery beyond one process: the web chat connection registry is in-memory, so a second app process delivers only to the sockets it holds and a customer whose turn ran elsewhere sees nothing until they reconnect (phase W self-critique). Postgres `LISTEN/NOTIFY` is the obvious fan-out, since the database is already there. DESIGN.md 4.1's "horizontal scaling is safe" is true of execution and not yet of delivery.
- [ ] Carry a message's transport detail from inbound to outbound. `InboundMessage.metadata` was removed in phase W's resolution (finding W16) rather than left as a seam pointing the wrong way: nothing wrote it, nothing read it, and `send` could not reach it, because `send` is given a `ConversationRef` built from the conversation *row*. An email adapter still cannot set `In-Reply-To` from the message it is answering without a second lookup of its own, which is the thing the protocol was supposed to spare it. Design it with the item below, which is the same seam from the other end.
- [ ] Seed a new conversation's `ctx.customer` from what the transport knows. `InboundMessage.customer_ref` reaches `conversation.customer_ref` and never reaches `ctx.customer`, which is what gates and prompts read; `ChannelHub.conversation_for` seeds every new conversation from the one constant `AppConfig.new_conversation_context` instead. For email the `From` header is the whole point (phase W review, "missed by self-critique" item 2). It needs the CRM seam the phase W self-critique names, which is why it is here and not there.
- [ ] Let `ChannelAdapter.send` refuse a delivery. It returns `None` and `ChannelHub.deliver` swallows every exception by design, so `mark_sent` is unconditional for every channel and no adapter can say "this one is a permanent bounce, do not mark it sent" (phase W review, "missed by self-critique" item 3).
- [ ] Outbound message identity and the per-turn email batch: `EngineHooks.send` receives text with no message id, so a transport cannot de-duplicate the at-least-once redelivery a crash between commit and send produces; and the engine flushes after every checkpoint that spoke, so nothing tells an adapter that a turn ended (phase W self-critique, "where email will strain this protocol"). One turn of the refund conversation produces two messages, which is two emails unless this is settled. Decide between a fourth protocol method and a flush driven by the runtime, and note that `send` runs inside the transaction that marks a message `sent`, so a buffering adapter is recorded as having delivered what it has only queued.
- [ ] Tests: email thread with a two-day gap resumes the same run; replay output matches trace. (Web chat round trip is covered by Phase W.)

Exit criterion: an email thread with a multi-day gap resumes the same run, and the replay endpoint shows the path taken.

## Phase 8: Evaluation harness

Design: section 16.

- [ ] pytest plugin exposing `golden` and `node_eval` fixtures; YAML formats for both.
- [ ] Path assertions with wildcards, expected and forbidden tool calls, LLM-judge rubric hook.
- [ ] `SimulatedCustomer` driven by persona and goal with seeded, recorded runs.
- [ ] Core adversarial suite: injection, gate bypass, confirmation manipulation, identity spoofing; runs against any pack.
- [ ] Shadow mode flag: agent drafts, outbound goes to the desk instead of the customer; diff tracking.
- [ ] Sample pack: at least six golden conversations and node evals for every llm node.

Exit criterion: `pytest` in the sample pack runs node evals, golden conversations, and the adversarial suite green with the fake provider.

## Phase 9: Knowledge graph, customer memory, cost controls, packaging

Design: sections 9.1 (knowledge graph), 10, 20, 4.1.

- [ ] `KnowledgeGraphRetriever` over `kg_entity` and `kg_relation`, entity extraction via structured call, one to two hop expansion, rendered passages with locators.
- [ ] Knowledge graph ingestion from YAML in `support pack knowledge sync`.
- [ ] Customer memory as a WRITE-tier internal tool; never treated as identity.
- [ ] Per-conversation cost cap and per-turn tool call cap enforced by the engine; cheap model for interrupt checks and summaries. Includes phase-4 finding R9: `max_tool_calls_per_turn` counts model-loop calls only, so a graph that walks five `tool` nodes spends none of it. Widening the counter changes what an existing manifest key means, so record the compatibility decision with it.
- [ ] Let a graph's `state:` name a model the pack's tools export, so `state.charge: Charge` type-checks instead of degrading to `Any` (phase-4 finding R8). The loader and the validator both have to carry the pack's registry to do it, which is the same plumbing the registry scoping below needs.
- [ ] Graph version migration hook (section 6.7) with a test that a changed state shape triggers handoff when no hook exists.
- [ ] Scope the node type registry per `Pack` (a `Pack.node_types` consulted by `build_runner` and by the loader that parses node YAML), so two packs loaded side by side cannot fight over one name (phase 2 review finding R6). Refusing a conflicting duplicate registration is already in place; the registry itself is still process-wide, and `NODE_TYPES` is what parses YAML, so the loader and validator have to carry it too.
- [ ] Split into two distributable packages: `support-core` and the sample pack depending on a pinned version; Dockerfile for the pack.
- [ ] Sample pack knowledge graph: plans, features, regions.

Exit criterion: the sample pack builds as its own image, starts, and answers "what does my plan include" with a knowledge-graph citation.

## Phase 10: Evaluate mem0 for customer memory

Depends on Phase 9 (which builds customer memory by hand) and on Phase 8 (which gives a way to
tell whether a change helped). Added 2026-09-06 at the user's request.

**Only the fourth memory layer is in scope.** DESIGN.md section 10 has four: the verbatim turn
window, the rolling summary, typed frame state, and durable per-customer notes. The first three
are not candidates and must not be handed to a library. Frame state in particular is what the
audit trail, the replay endpoint and the crash recovery are built on, and phase 2's review found
two message-loss bugs caused by state living outside durable storage; a second store with its own
idea of conversation state would reintroduce exactly that class of bug.

- [ ] Establish the baseline first: hand-built `customer_memory` from phase 9, scored on the phase
      8 golden conversations. Without a number, "mem0 is better" is unfalsifiable.
- [ ] Check the durability constraint before anything else. DESIGN.md 7.1 requires the frame stack
      and the trace step to be written in ONE transaction, and resume to be derived entirely from
      durable state. If per-customer notes cannot be read and written inside that transaction, or
      an outage of the memory service can wedge a turn, that is a rejection on its own.
- [ ] Check data residency and retention. DESIGN.md 20 makes retention configurable per pack and
      requires PII redaction; a hosted memory store moves customer data out of the deployment's
      own Postgres, which section 4.1 assumes is the only stateful dependency.
- [ ] Decide what recall would actually improve. The candidate wins are a preferred name, a
      recurring issue, and a channel preference. If the wins are only those, a table is enough.
- [ ] Behind the existing WRITE-tier internal tool either way, so a memory write stays subject to
      the same risk policy and the same trace as any other side effect.
- [ ] Nothing in customer memory may ever set `identity_verified`; that is only ever set by the
      `verify_identity` sub-graph via a tool (DESIGN.md section 10).

Exit criterion: a written recommendation with the baseline and the measured comparison, and either
an implementation behind the memory tool or a recorded decision not to adopt, with reasons.

## Phase 12: `support pack new` - a tool for building a pack

Added 2026-09-06 at the user's request. Independent of phases 5 to 11; it needs the validator
(phase 1), the tool contract (phase 4) and the knowledge source format (phase 5) settled.

The point is not scaffolding for its own sake. A pack is the whole product surface for a new
domain, and today writing one means reading DESIGN.md section 5 and copying `packs/acme_billing`.
The tool should make the validator the teacher: a generated pack passes `support pack validate`
from its first minute, and every later mistake is caught with a rule id and a file location rather
than at run time in front of a customer.

- [ ] `support pack new <id>` - scaffolds DESIGN.md section 5's layout: manifest, persona,
      policies, a root graph with a classify node and the `small_talk` / `unclear` / `refused`
      edges the sample pack has learned it needs, an empty tools module and knowledge directory.
      Validates clean immediately.
- [ ] `support pack add-workflow <name>` - a workflow with the shape the two existing ones share:
      identity gate, read step, slots from the customer, confirm, and the single guarded write.
      That shape is not a template flourish; it is what the confirm-on-every-path rule and the
      same-graph approval rule force, so generating it teaches the constraint.
- [ ] `support pack add-tool <name> --risk read|write|high` - a typed tool stub with its input and
      output models and its risk tier declared. **Generate a stub, never an implementation**: the
      body talks to a real system and is the one part a human must write and review.
- [ ] Guided mode: draft a workflow from a description using the configured model, then hand the
      draft to the validator and iterate against its findings rather than against a human. The
      validator is a real scoring function, which is what makes generation defensible here.
- [ ] Guided mode must refuse to generate what review has already shown a model gets wrong: it may
      not mark a tool `confirm_exempt` (the reason has to be written by a person), may not set a
      risk tier without stating why, and may not write `policies.md`.
- [ ] `support pack doctor` - runs the validator plus the phase 8 adversarial suite against a pack
      and reports what a release would fail on.

### Tuning the drafted prompts with DSPy

Folded in from what was phase 11, at the user's request, because it belongs to the same job: the
tool that drafts a workflow's instructions is the tool that should improve them. Still gated on
phase 8, which is what produces a score - a tuner with no metric fits noise.

- [ ] `support pack tune <pack> [--node ...]` - optimise the `instructions` of `llm` nodes and
      nothing else. DSPy signatures map cleanly onto what a node already declares: the output
      schema is the output fields, and `decision` is a constrained choice over the node's own edge
      labels, which is the decision constraint restated rather than relaxed.
- [ ] **Layers 1 to 3 are frozen against the optimiser, structurally.** The core system prompt, the
      persona and the policies are a compliance surface (DESIGN.md 11.2); an optimiser that
      rewrites a policy line to win a score has broken the thing the design protects. Enforce it in
      code, not by convention, and test that a tuning run cannot reach them.
- [ ] **The training signal must come from outside the generator.** Tuning drafted prompts against
      evals the same tool drafted is circular and measures nothing. Accept only human-approved
      golden conversations or real transcripts from the queue the pack is replacing, and refuse to
      run against a set the tool generated and nobody reviewed.
- [ ] **Treat compiled few-shot demonstrations as content, not configuration.** DSPy compiles
      examples into the prompt; a demonstration drawn from a real conversation puts that customer's
      words into every future prompt. Redact before compiling, review the demonstrations a human
      would never otherwise read, and pass them through phase 3's delimiting like any other text.
- [ ] Baseline per node before tuning, and diff what changed for a human to approve. A prompt
      nobody read is a prompt nobody owns.
- [ ] Re-run the adversarial suite after tuning. A prompt optimised for task accuracy can quietly
      lose its refusal behaviour, and phase 3's review showed injection resistance is not something
      to take on trust.
- [ ] Commit tuned instructions as ordinary pack data, versioned and reviewable like any other pack
      change, never regenerated at deploy time. A pack must be the same thing on every machine.
- [ ] DSPy is an optional dependency of the tool, not of `support-core`. A deployment that serves
      packs must not have to install an optimiser.
- [ ] Tests: a generated pack validates; its generated workflow passes the adversarial approval
      suite; a guided draft that violates a rule is rejected rather than written out.

Exit criterion: `support pack new acme_airline` followed by `support pack add-workflow
cancel_booking` produces a pack that validates clean and runs an end-to-end conversation against
the recorded provider; and `support pack tune` measurably improves a node against a human-approved
set without touching the persona or policy layers and without losing an adversarial test.

## Phase F: Final integration review

- [ ] A fresh agent reviews the whole repository against DESIGN.md sections 3 and 14: every guiding principle and every structural guardrail must be traceable to code and a test.
- [ ] Run the full suite including live-provider golden conversations if `ANTHROPIC_API_KEY` is present.
- [ ] Record open items in "Deferred findings" and write `reviews/final.md`.

---

## Deferred findings

Populated by phase reviews. Format: `- [phase N] finding, severity, reason deferred`.

- ~~[phase 0] F5: `trace_step` has no ordering column; `started_at` defaults to `now()`, which is identical for every step written in one transaction, so replay (DESIGN 7.3) cannot order steps by it, should-fix, deferred to phase 2 which owns the checkpoint transaction and its migration.~~ **Closed in phase 2**: migration `0002` adds `trace_step.seq` with a unique `(run_id, seq)`, and the engine writes both timestamps from an injectable clock instead of the server default.
- ~~[phase 0] F6: `tool_call` has no `run_id`, `conversation_id` or `step_id`; the only link to a conversation is the idempotency key string, should-fix, deferred to phase 4 which owns the `tool_call` migration.~~ **Closed in phase 4**: migration `0006` adds `run_id` (FK, indexed), `step_id` and `node_id`, and `repositories.tool_calls_for_run` is the join.
- ~~[phase 0] N1: `action_approval` has no single-use marker or expiry, so one approval row could satisfy two tool calls with the same args hash, nit (a design gap: DESIGN 8.2 does not demand single use), deferred to phase 4.~~ **Closed in phase 4**: `consumed_at`/`consumed_by_tool_call_id`, taken by an atomic update that is also the claim, so two racing callers cannot both win. The approval is bound to the run, the frame and the confirm node besides.
- [phase 0] N2 (downgrade half): the initial migration's downgrade drops the `vector` extension, which fails or removes a shared extension if an administrator pre-installed it, nit, deferred to phase 5 which owns pgvector; editing the initial migration for a shared-instance concern is not a risk-free few lines. The superuser requirement is documented in README.
- [phase 0] N9: two concurrent `pytest` processes against one database corrupt each other's fixtures (reviewer measured 47 passed, 8 errors); README warns but nothing enforces it, nit, deferred because a session-long advisory lock needs a connection that outlives the per-test event loops, which the fixture design avoids on purpose. The dedicated `support_test` database (F2) removes the developer-database half of the risk. **Half closed in phase 6** (its reviewer hit the same thing and asked for a line): the per-test `TRUNCATE ... RESTART IDENTITY CASCADE` takes an `AccessExclusiveLock` on every mapped table while another connection holds a `RowShareLock` on some of them, so under load the two deadlock and the rest of the file fails behind them with `no conversation <uuid>` and foreign-key violations that reproduce for nobody. The fixture now sets `lock_timeout` and retries the truncate on `deadlock_detected`/`lock_not_available`, which is the documented answer and was verified against a held reader; what is still open is the other half - another process truncating rows out from under a *running* test, which no retry can fix and which still needs the session-long lock this entry was deferred for.
- ~~[phase 1] N3: `graph.subgraph_cycle` fires only when *no* graph in the cycle contains a suspending node anywhere, not one on the cycle path, so mutual recursion through a graph with an unrelated `ask` on an untaken branch is missed (reviewer's hostile case T, reproduced after resolution), nit, deferred to phase 6.~~ **Closed in phase 6**: each leg of a cycle is judged on its own path - can this graph reach the call that continues the cycle from its start without passing a suspending node? - and the cycle is refused only when every leg can. Hostile case T is now an error. The interprocedural CFG also gained the interrupt push and return edges of DESIGN.md 6.6, which can only shrink the confirm-coverage sets and makes the phase-1 self-critique's item 4 honest.
- ~~[phase 1] H: the approval-argument comparison is textual, so a `tool` node that rewrites a field the approved arguments read, between the confirm and the call, passes `graph.approval_mismatch` (reviewer's hostile case H), should-fix, deferred to phase 4.~~ **Closed in phase 4**, both halves: `graph.approval_args_mutated` refuses it at load, and the run-time hash check refuses it at execution even in a pack that never met the validator.
- ~~[phase 1] I: risk tiers come from the pack-authored `tools/tools.yaml`, so declaring `issue_refund` as `read` removes every confirm check and makes it callable from an `llm` loop (reviewer's hostile case I), should-fix, deferred to phase 4.~~ **Closed in phase 4**: nothing at run time reads that file. The registry built from the imported `TOOLS` is what the validator type-checks against and what the runtime enforces, and a declared tier that disagrees is `tools.registry_drift` (ERROR).
- ~~[phase 1] J: a WRITE tool marked `confirm_exempt` is reported as INFO only, which does not fail `--strict` and forces no human to look (reviewer's hostile case J), nit, deferred to phase 4.~~ **Closed in phase 4**: `confirm_exempt_reason` is required, and `graph.confirm_exempt` is a WARNING quoting it.
- ~~[phase 1] P1: `load_pack` reads and parses every graph twice, so a pack edited on disk while it runs can produce a `PackPin` whose hashes describe a mix of two versions, nit, deferred to phase 2, which owns hot reload.~~ **Closed in phase 2**: the validator snapshots the text it read onto the report and the loader parses that snapshot, so the pin hashes the bytes that were parsed.
- ~~[phase 1] P2: `templates.ENVIRONMENT` is a process-wide Jinja environment with a shared template cache; harmless while nothing varies per pack, a concurrency bug the moment a pack supplies a filter or two pack versions load side by side, nit, deferred to phase 2.~~ **Closed in phase 2**: `Pack.environment` per pack, one per validation run; the module-level environment is only a fallback.
- [phase 2] R7: a caller that cannot take the conversation lock waits, holding a connection each, so N+1 concurrent inbound messages on one conversation with a bounded pool is a stall (reviewer measured five OS processes, four of them blocked for a whole turn), should-fix, deferred to phase 7. The mechanism for the fix already exists (`lock_wait_seconds=0` returns `queued=True`, `drain` is the poller's entry point); what R7 asks for is a *default* for channel webhooks and a poller to make it safe, and phase 2 has neither a channel adapter nor a scheduler - the same reason `sweep_timeouts` and `recover_stalled` are methods rather than a daemon (checklist line added there).
- [phase 2] R6 (registry-scoping half): `register_node_type` writes into one process-wide table, so DESIGN.md 6.7's two pack versions side by side, and phase 9's core/pack split, can still collide over a node type name - a conflicting duplicate is now refused loudly rather than silently overwriting, which is the safe half, nit, deferred to phase 9. Scoping the registry per `Pack` means the loader and the validator carry it too, because `NODE_TYPES` is what turns node YAML into models (checklist line added there).
- [phase 3] LLM calls are not replayed from the trace, so a crash between the model answering and the checkpoint committing re-asks and re-pays for the question and may get a different answer, should-fix (named by the phase-3 self-critique as fragility item 1 and endorsed by its review; every review finding V1 to V11 is fixed, this is the one open item promoted from the critique), deferred to phase 7, which owns replay and the trace endpoint; the cache key - the step id - is already on `NodeRuntime` (checklist line added there).
- [phase W] The web chat endpoints have no authentication: a session key is an unguessable token and is the whole of the access control, with no WebSocket origin check, no CSRF defence on the webhook, no rate limit and no webhook signature verification, should-fix, deferred to phase 7, which owns the channel surface. **Re-opened and half closed by the phase W review**: the deferral's stated premise was that phase 7 "adds a desk API where the same gap would expose somebody else's conversation", and phase 6 had already shipped that desk on this listener, on by default and open (finding W1). The desk half is fixed - off by default, bearer token required, missing credential refuses to start. The customer's own key, the origin check, the rate limit and the webhook signature are still phase 7's. The key at least no longer travels in the URL (finding W9).
- [phase W] W1's remainder: one shared desk token is not per-operator identity, rotation, or an audit of who did what, should-fix, deferred to phase 7, which owns this surface. The desk already takes a `human_id` on every action, so the audit half has a place to go; what is missing is anything that makes the `human_id` believable (checklist line added there).
- [phase W] W16's replacement: an inbound message's transport detail (`Message-ID`, `References`) still cannot reach `send`, and `InboundMessage.customer_ref` still does not reach `ctx.customer`, and an adapter still cannot refuse a delivery, should-fix, deferred to phase 7, which writes the email adapter that needs all three. The misleading `metadata` field is gone rather than left as a seam that could not work (three checklist lines added there).
- [phase W] Live delivery is process-local - the connection registry is a dictionary in one process - so with two app processes a customer sees a turn run elsewhere only when they reconnect, should-fix, deferred to phase 7; nothing is lost (the messages are rows and a reconnecting client is sent the transcript) but DESIGN.md 4.1's horizontal scaling claim is not yet true of delivery (checklist line added there).
- [phase W] Outbound delivery has no message identity and no turn boundary: a crash between the checkpoint and the send re-offers every pending row with nothing a transport could de-duplicate on, and an adapter is handed messages several times per turn, so DESIGN.md 12's "batched per turn into one email" cannot be expressed, should-fix, deferred to phase 7, which writes the email adapter that needs both (checklist line added there).
- [phase 0] N12: `doc_chunk.embedding` is dimensionless, so no HNSW index is possible and mixed-dimension rows fail only at query time, nit (admitted by the implementer, measured by the reviewer), deferred to phase 5 which picks the embedding model (checklist line added there).
- [phase 4] R8: `refund.yaml`'s `state.charge: Charge` names a model the pack's tools export, which core cannot resolve, so it is typed `Any` and `state.charge.amount` is unchecked in both the confirm action and the tool args (`graph.state_type_unresolved`), nit, deferred to phase 9. No money risk - the two expressions are identical, `graph.approval_args_mutated` covers the path between them, and the hash is taken over arguments coerced through the tool's input model at both ends. Both fixes are bigger than a resolution pass should carry: flattening the state rewrites the arguments of the one HIGH-risk call in the pack, and letting a graph name a pack-exported model is loader work that belongs with phase 9's core/pack split (checklist line added there).
- [phase 4] R9: the per-turn tool budget counts only model-loop calls, so a graph that walks five `tool` nodes spends none of `max_tool_calls_per_turn` and only `max_nodes_per_turn` bounds it, nit, deferred to phase 9 on the reviewer's own recommendation: widening the counter changes what an existing manifest key means, which is a compatibility decision rather than a fix, and phase 9 owns the cost caps (checklist line added there).
- ~~[phase 4] The `graph.confirm_exempt` warning names the tool but not the *arguments* the pack's nodes pass it ... Nit, deferred to phase 6.~~ **Closed in phase 6**: the warning lists every call site of the exempt tool, across every graph, with the argument expression each one passes - so `verify_identity.send_code(email: 'ctx.customer.email')` reads differently from a node passing `state.email`.
- ~~[phase 4] An `on_error` edge that returns to its own `tool` node re-enters at the next attempt, claims a fresh idempotency key, and calls the tool again ... Should-fix, deferred to phase 6.~~ **Closed in phase 6, both ways**: `graph.on_error_repeats_side_effect` reports it at load (a WARNING, because re-sending a one-time passcode is this exact shape and is right), and `limits.max_node_errors` bounds it at run time - consecutive failures per node, counted in the frame so a crash does not reset them and an ordinary loop does not trip them, and a handoff with reason `limit_exceeded` when the bound is reached.

- [phase 6] P2 (durable half): a handoff no sink would take is visible only in
  `HandoffService.failures`, a per-process list nothing sweeps, so a second replica cannot see it
  and no queue row exists to find later, should-fix, deferred to phase 7. The customer-facing half
  is closed - the run is parked, the pack's promise is replaced with one that is true, and
  `suspend_detail.queued` records that nobody was told - but a durable `undelivered` row and a
  retry need the scheduler phase 7 owns (checklist line added there).
- [phase 6] P6 (SLA half): a customer message queued behind a `waiting_human` run is now
  acknowledged once and shown on the desk's handoff view, but nothing counts it against the
  handoff's SLA or re-prioritises the queue, nit, deferred to phase 7, which owns the metrics and
  the scheduler that would do the counting.
- [phase 6] The interrupt check and the handoff summary are model calls made outside any node, so
  section 15's `llm_call` span has no `node` parent for them and the two most expensive new calls
  in the system will not appear in per-node cost attribution; and the check has no cheaper model
  to run on because the per-node `model:` override cannot reach it, should-fix, deferred to phase
  7, which owns tracing, metrics and DESIGN.md 20's cost controls (two checklist lines added
  there).

---

## Decisions log

- 2026-09-07: **the human desk is off unless a deployment turns it on, and cannot be turned on
  without a credential** (phase W review finding W1, a must-fix). It was mounted on the customer
  chat's own application and port, on by default, with no authentication - so an anonymous
  browser listed every conversation in the deployment, read another customer's transcript and
  handoff packet, and wrote into it, and could countersign its own `requires_human_approval`
  action. Three parts, and the order is what makes it a fix rather than a hiding place: the
  default is off, so a deployment that configures nothing serves no desk; `serve_desk` on with no
  `desk_token` raises at `create_app`, so a missing credential stops the application rather than
  opening a desk; and the token is checked on the *router*, so a route added later is behind it
  by construction rather than by whoever writes it remembering.

  It stays on the same application rather than moving to its own port, and that is the part worth
  writing down, because DESIGN.md section 12 calls the desk "not a customer channel". Three
  reasons. Section 4.1 makes a deployment one service and section 12 says the desk "uses the same
  API surface", so a second listener is a change to the design rather than a reading of it. The
  connection registry is a dictionary in one process, so a desk `reply` reaches a customer's open
  socket only from the process holding it - splitting the desk out today would quietly stop
  delivering a human's reply, trading a hole that is now closed for a regression that is not. And
  the exposure was never the shared port: it was a default that served customer data to anyone
  who asked, and a shared port with a credential is what every admin API on a service port is.
  Phase 7 owns both the `LISTEN/NOTIFY` fan-out and this surface and can split it then, with a
  checklist line saying so.

  One shared token, not operator accounts. It is the smallest thing that makes the failure mode a
  refusal, which is what the finding asks for; identity, rotation and an audit of who did what
  are phase 7's, and the desk already takes a `human_id` on every action for the last of those.

- 2026-09-07: **the pending inbound queue is ordered by a number the database hands out, not by a
  timestamp** (phase W review finding W4). `message.queue_seq` is claimed from
  `conversation.inbound_seq` by an `UPDATE ... RETURNING` that takes the conversation's row lock,
  so concurrent callers serialise there and leave with distinct, increasing numbers (migration
  `0009`, with a backfill). The old ordering was `created_at, id`: `created_at` is the
  *transaction start* timestamp, identical to the microsecond for two callers who arrive
  together, and the tie then fell to a random UUID - the reviewer posted two messages at once in
  a known order, got them back in the other one, and watched the second run against a
  conversation the first had not started, which parked the run for good.

  What this does and does not claim. It does not claim to know which of two simultaneous requests
  "should" be first; they were simultaneous, and no clock in the database can answer that. It
  claims there is exactly *one* order, that every caller has a place in it, that the place is
  decided when the row is written rather than reconstructed afterwards from a clock, and that the
  drain follows it. Phase 2's review found two message-loss bugs of this family and fixed both in
  durable state rather than in timing; a reorder that dead-ends a conversation is the same class
  of problem and this is the same kind of fix. The cost is that concurrent messages on *one*
  conversation serialise for the length of a transaction that writes one row.

- 2026-09-07: **a turn that fails tells the customer so** (phase W review finding W6). Every
  DESIGN.md 7.3 failure path already parked the run and paged a person; the turn that did it said
  nothing at all, so a web chat client had a status pill to render and an email customer had
  silence after asking a question. `Executor._handoff` now emits the same sentence a `handoff`
  node emits, chosen by the same rule - `DEFAULT_HANDOFF_MESSAGE` when a human was told,
  `HANDOFF_UNDELIVERED_MESSAGE` when nobody was - and no failure detail, because what went wrong
  belongs in the packet a person reads and not in the customer's transcript.

- 2026-09-07: **the web chat session key travels in the socket's first frame, and connecting
  creates nothing** (phase W review findings W9 and W8). The key was in the query string, where
  uvicorn writes it verbatim into the access log and so would any proxy, CDN or APM in front of
  it - and the key is the whole of this channel's access control, so the one secret a customer
  holds was being written down by four systems with no use for it. The client now opens with
  `{"type": "hello", "session": "<key>"}`, or `{"type": "hello"}` to be given one. The
  `Sec-WebSocket-Protocol` header was the other candidate and was rejected because a subprotocol
  value is an HTTP token and a session key may contain `:`.

  Connecting no longer creates a `conversation` and a `run` before the customer has said
  anything: a connect resolves the key and reports an empty conversation, and the first *message*
  creates the row. A connection with no conversation yet waits in the registry under its channel
  key and is attached when somebody's message creates it, which is what keeps two tabs opened
  together on a fresh key - and a tab watching while the first message arrives by `POST` -
  receiving what the conversation says.

- 2026-09-06: **when no sink takes a handoff packet, the customer is told so**, in core's words
  rather than the pack's. The handoff hook now answers whether a human was actually told, and a
  `handoff` node whose packet nobody took has its message replaced by
  `HANDOFF_UNDELIVERED_MESSAGE` (phase 6 review finding P2). What that sentence claims is what
  the engine can still guarantee with every sink refusing: the run is parked `waiting_human` and
  checkpointed, so the conversation, its stack and its transcript are durable and a later attempt
  resumes exactly here. What it does not claim is that anybody will come, because the thing that
  would have paged a person is the thing that failed - and DESIGN.md 14 forbids the promise the
  pack's own sentence makes. A `CompositeSink` partial failure counts as delivered: a webhook
  down while the Postgres row was written is still a page a desk can find, which is why
  `default_sink` composes the two.

- 2026-09-06: **a desk `resume` state patch is validated before it is written**, against the
  declared state model of the frame the run is suspended in, and refused with the fields that are
  wrong and the fields that exist (phase 6 review finding P1, the must-fix). Writing first and
  validating on the next node entry made one mistyped field unrecoverable through any API call
  and reported it as `pack_incompatible`, which is the wrong DESIGN.md 7.3 reason because the
  pack was never at fault. Two further rules the patch obeys, both already settled elsewhere: it
  may never set `identity_verified` (DESIGN.md 10 gives that to `verify_identity` alone, through
  a tool, and `AppConfig` is held to the same rule), and it is refused while an approval is live
  on that frame, because the proposal the customer agreed to was computed from the state it would
  edit and the desk's one route to an action is `approve`.

- 2026-09-06: **`llm.confidence_threshold` gates the interrupt check's decision too** (phase 6
  review finding P3). The confidence was collected, carried through three layers and read by
  nobody, so a `cancel` at 0.0 unwound the whole frame stack and a `new_intent` at 0.0 discarded
  a workflow. Those are the only two answers here the engine acts on and both are destructive,
  which is the same argument the threshold already wins for an `llm` node's decision and a
  `confirm` node's yes. Below it the engine does not act and does not pretend it understood: one
  core sentence, a hint on the resume event so a slot extractor does not read the reply as an
  answer, and the node that asked the question asks it again.

- 2026-09-06: **DESIGN.md 7.3 is amended to two failure tiers**, dropping "otherwise the frame's
  `on_error` graph" (phase 6 review finding P7). Three phases running recorded it as
  unimplemented and argued nothing needed it; the reviewer's point was that the argument had to
  end somewhere. Graphs have no `on_error` key, no error `FrameKind` exists, nothing the engine
  can raise at frame level is beyond a node-level edge, and the fall-through now reaches a real
  packet carrying the failure's own reason rather than a bare status. Adding the tier later is
  additive and needs a use case first.

- 2026-09-06: **the root frame is never parked by an interrupt.** DESIGN.md 6.6 step 4 pushes a
  workflow over a suspended frame and offers the old one back afterwards; when the suspended
  frame is the *root* frame, that would mean asking "shall we go back to: is there anything
  else?". Section 6.5 already gives the root graph an intent classifier it "loops back to after
  each sub-graph returns", and that classifier knows every edge the root declares while the
  interrupt check knows only the ones leading to a workflow - so a topic change there is the
  root graph's own business and no check is run at all. `interrupts.allowed_from` may still list
  the root graph, as DESIGN.md 5.1's example does; it is inert.

- 2026-09-06: **`interrupts.allowed_from` is an allow-list, not a default.** A graph in neither
  list is treated exactly as one in `blocked_in`: the request is recorded and offered back rather
  than acted on. DESIGN.md 6.6 says "if the current graph allows interrupts", and the safe
  reading of *allows* is *says so* - a pack that wants a workflow interrupted writes one line,
  which is a decision somebody made rather than one a default made for them. `cancel` is the
  exception and is honoured from every graph including a blocked one: refusing to let a customer
  stop is worse than any workflow it interrupts, and unlike an interrupt it reaches nothing new.

- 2026-09-06: **core writes the four sentences an interrupt needs**, and the deferral is one of
  them. DESIGN.md 19 step 8 has the *node* say "I'll come back to the address change once the
  refund is sorted", but the two node types that suspend into `waiting_customer` are `ask` and
  `confirm` and neither speaks on resume, so leaving it to the pack would mean every pack that
  wanted a working interrupt writing the same four messages into every graph. The *hint* reaches
  the model as well - it is on the `ResumeEvent` and in the `SlotRequest`, which is what stops an
  extractor reading "and change my address" as a passcode - but the sentence the customer sees is
  core's, and each one is a promise the engine keeps.

- 2026-09-06: **the return offer is a core step on the parked frame**, written to the trace under
  the reserved node id `__interrupt_return__`. No graph declares it and no pack should have to; a
  pack node id must match `^[a-z][a-z0-9_]*$`, so the name cannot collide. It advances its
  attempt counter on every checkpoint like any other node, so the offer, the answer and a
  re-offer after an unreadable reply are three steps rather than one id written three times.

- 2026-09-06: **a handoff packet is delivered before the checkpoint that parks the run**, and
  delivery is idempotent under `(run_id, step_id)`. The other order has a window in which the run
  says a human is needed and no human has been told - and nothing sweeps for that, because a
  suspended run is not a stalled one. This way a crash in the window re-executes the node, which
  delivers again, which the unique index makes a no-op. The same device an `ActionApproval` uses
  against a re-executed `confirm`.

- 2026-09-06: **`HandoffNode` gains an optional `message` and `next_steps`**, and
  `pack.yaml` gains `limits.max_node_errors`. DESIGN.md 6.2 gives the handoff node a `reason` and
  two edges and no way to say anything, and a node that parks a customer in silence is worse than
  one that does not exist; `next_steps` lets a pack override core's reason-keyed checklist in
  section 13's packet, because a pack knows its own desk. `max_node_errors` is the bound
  DESIGN.md 7.3's retry story needs and section 5.1 has nowhere to put - same precedent as phase
  2's `timeouts:` and phase 3's `memory:`.

- 2026-09-06: **DESIGN.md 7.3's middle failure tier - "otherwise the frame's `on_error` graph" -
  is still not implemented**, for the third phase running. Phase 2 recorded it as a divergence
  and phase 2's reviewer endorsed the omission; nothing phase 6 adds can raise a frame-level
  error that a node-level edge could not catch, and the fall-through now reaches a real packet on
  a real queue rather than a bare `waiting_human` status. Recorded again rather than invented.

- 2026-09-06: DSPy folded into phase 12's pack tool rather than kept as its own phase, at the
  user's suggestion. It is the same job: the tool that drafts a workflow's instructions is the one
  that should improve them, and both are authoring-time concerns that a serving deployment never
  runs. The constraints carry over unchanged - only node instructions are tunable, layers 1 to 3
  are frozen in code, and the adversarial suite must still pass afterwards. Two constraints were
  added on folding: the training set may not be one the tool itself generated, because tuning a
  draft against its own draft measures nothing; and compiled few-shot demonstrations are content
  rather than configuration, so a demonstration drawn from a real conversation is a customer's
  words in every future prompt unless it is redacted and reviewed.

- 2026-09-06: ColBERT folded into phase 5 as a second retriever rather than added as a later phase,
  because it changes that phase's design rather than following it. It runs locally, which removes
  phase 5's worst limitation: no embedding API is available in this deployment, so the dense path
  alone would ship with its retrieval quality unmeasured.
- 2026-09-06: Qdrant chosen as ColBERT's home, replacing the earlier open question about where a
  late-interaction index should live. It stores multivectors and scores MaxSim natively, so the
  corpus stays a service rather than becoming an index directory the deployment must build and
  version by hand, and collection aliases make "a re-sync produces a new version, old traces keep
  the old one" exact instead of approximate. This does relax DESIGN.md 4.1's "Postgres is the
  single stateful dependency", and the relaxation is accepted knowingly: retrieval is a read
  outside the checkpoint transaction, so it cannot cost a checkpoint or a resume, which is the
  property 4.1 existed to protect. Postgres keeps the lexical half, so retrieval degrades rather
  than fails when Qdrant is down. The real price is operational - one Qdrant per domain.
- 2026-09-06: A pack authoring tool added as phase 12. Its principle is that the validator is the
  teacher: generated packs validate from the first minute, and guided generation iterates against
  the rules rather than against a reviewer. Tool bodies are never generated, only stubs, and a
  model may not write policies or grant a confirm exemption.

- 2026-09-06: mem0 and DSPy added to the roadmap as phases 10 and 11, both sequenced after phase 8
  and both scoped narrowly. Neither is used today. The reason for the ordering is the same in both
  cases: phase 8 is what produces a score, and adopting either without one is a change nobody can
  tell the sign of. The scope limits differ - mem0 may only touch per-customer notes, never frame
  state, because durability depends on one transaction; DSPy may only touch node instructions,
  never the persona or policy layers, because those are a compliance surface.

- 2026-09-06: GLM added as a second model provider, outside the phase structure, because the
  deployment needs it. Z.ai serves GLM through an Anthropic-compatible endpoint, so it is the
  same provider with a different base URL and two optional fields (prompt caching, `strict` tool
  schemas) turned off; neither is load-bearing. `AppConfig` gained a `models` override so a pack
  that names Claude model ids can serve a different vendor without being edited. Commit d9eb09e.
- 2026-09-06: First live model run of the project, against GLM. Two defects found and fixed, both
  the same shape: GLM spells absent and nested values as text rather than as JSON. A nested
  object arriving as a JSON string is now parsed and then validated exactly as before (5711ac2);
  a message whose whole text is the word "null" is read as no message (04b8a4f). Neither relaxes
  review finding V2. Recorded here because a third instance of the same family should be expected.

- 2026-09-06: the approval hash is taken over arguments **coerced through the tool's own input
  model**, and the approval is bound to more than DESIGN.md 8.2 asks for. The design says
  `sha256(tool_name + canonical_json(args))` and does not say which `args`; coercing first means
  `29` and `29.0` are one call when the input is a `float` - which they are - while any
  difference the model does not erase is still a mismatch, and the hash then covers exactly what
  the tool receives, because `model_validate` drops anything the input model does not declare.
  The row is additionally bound to the run, the frame sequence and the `confirm` node that
  recorded it, and is single-use: `frame_seq` is monotonic and never reused (7.1), so an
  approval from an earlier trip through the same graph is refused before single use has to catch
  it, and neither binding can be satisfied by getting the arguments right.

- 2026-09-06: a tool **declares which `ctx.customer` fields it may change**, in a
  `patches_context` frozenset on `Tool`, and a patch outside the declaration fails the call
  (phase-4 review finding R4). This adds a field DESIGN.md 8.1's class does not have, which is
  why it is written down here: the decision below establishes that *a tool* is the thing that
  may change the customer, and this narrows it from "any WRITE or HIGH tool may set any field,
  `identity_verified` included" to "the tool that checks the passcode may set
  `identity_verified`". The difference is the whole identity gate, because a WRITE tool may also
  be `confirm_exempt`, so nothing else stood between a careless tool and a verified customer.
  Empty is the default and the names are checked against `CustomerContext` when the tool is
  built, so a typo is a load error rather than a patch that silently never lands.

- 2026-09-06: an async `tool` node **carries the idempotency key its dispatch claimed on the
  suspension**, and `complete_async` takes it explicitly (phase-4 review finding R1). The step
  id is `run_id:frame_seq:node_id:attempt` and the checkpoint that records a suspension
  increments the attempt, so a key derived from the site as it stands when the callback arrives
  names a call nobody claimed - which stranded the `tool_call` row and, on a graph whose
  `on_error` returned to the tool node, dispatched a second time. The same device the `confirm`
  node already uses for its argument hash: what the resuming pass needs is what the suspending
  pass knew, and durable state is the only place the two meet. The key is checked rather than
  trusted - the row it names must belong to this run, this node and this tool.

- 2026-09-06: a tool **may change `ctx.customer`**, and nothing else may. DESIGN.md 19 step 9
  has `verify_otp` set `ctx.customer.identity_verified` while 6.1 says `ctx` is read-only *to
  nodes*; both hold if the node does not write it and the tool does, through
  `ToolContext.patch_customer`, with the engine committing the patch in the checkpoint
  transaction. Only a WRITE or HIGH tool may (READ means no side effects) and only a node the
  graph declares `type: tool` may carry one out, which is what stops a pack-registered node type
  declaring its own customer verified.

- 2026-09-06: `confirm_exempt` requires a written reason and is reported at **WARNING**
  (phase-1 deferred finding J). DESIGN.md 8.2 asks for exemptions to be "reviewed deliberately";
  an INFO line in a report that exits 0 is noticed rather than reviewed, and a required sentence
  makes the author write down the argument a reviewer would otherwise have to reconstruct. The
  cost is that `--strict` now fails on any pack with an exemption until somebody has read it,
  which is the intended cost.

- 2026-09-06: a `confirm` node's yes/no reading has **three answers**, and `unclear` asks again.
  DESIGN.md 6.2 gives the node `yes` and `no` edges and requires "an explicit yes"; a model asked
  for a boolean has to put "hmm, how much was it?" somewhere, and both places are wrong. The hook
  (`EngineHooks.confirm_decision`, default a closed keyword list, `StructuredConfirmClassifier`
  where a provider is configured) may return `unclear`, and the node re-presents the proposal -
  which costs a turn and cannot loop, because each iteration needs a new customer message.

- 2026-09-06: **`issue_refund` is not idempotent**, and a non-idempotent call whose outcome is
  unknown is refused rather than retried. DESIGN.md 8.1 defaults `idempotent` to true and its
  example is silent. The `tool_call` row is claimed before the call, so a crash in the window
  leaves a `running` row that means "this may already have happened"; retrying it is a second
  refund and pretending it succeeded is a lie, so the call fails, the graph routes to `on_error`
  and, in the sample pack, tells the customer a person will check.


- 2026-09-06: `ChannelAdapter.send` takes a frozen `ConversationRef` rather than the `Conversation` ORM row DESIGN.md section 12's signature names. The protocol's other two methods are unchanged, and the method set is pinned by a test, because phase 7's email adapter has to implement it without changing it. Two reasons for the one change: an adapter holding a live ORM instance can write through it, which is not what a transport is for; and delivery can outlive the session that loaded the row - trivially for email, which may send minutes after the turn - so a detached instance would be the ordinary case. The snapshot carries what a transport can legitimately need (id, channel, channel key, customer ref, status, and the stored `ctx` as JSON) and nothing else.

- 2026-09-06: a conversation is named by its channel, in `conversation.channel_key`, unique per channel among the rows that have one (migration `0007`). DESIGN.md section 12 identifies a conversation by "thread id, session id", and section 4.1 runs several processes against one database, so that name has to be durable rather than a property of a connection. Unique, because two browser tabs opening together or a webhook delivered twice must not produce two conversations for one thread - the loser of the race reads the winner's row back. Nullable and partially indexed, because a conversation opened by the API rather than by a channel has no such name.

- 2026-09-06: the HTTP path takes the conversation lock with `lock_wait_seconds=0` and hands a conversation it could not lock to an in-process drain worker (phase 2 review finding R7, customer half). Waiting would hold a connection per waiter, which phase 2's reviewer measured and phase 3's seconds-long turns made expensive. Giving up after a budget is safe because the message is already durable and `pending` and whoever holds the lock drains the queue in order; the scheduler that would sweep a message nobody comes back for is still phase 7's.

- 2026-09-06: which pack, which model provider, and what a new conversation starts out knowing are `AppConfig` - a JSON file named by `SUPPORT_APP_CONFIG` with environment overrides - rather than code. `provider: "auto"` is a live Anthropic account where there is an API key and the recorded cassettes where there is not, so the same image demonstrates and deploys. Secrets are unchanged: the database URL and the API key are still read from the environment (DESIGN.md 20). The configured context is validated as a `ConversationContext` at startup and may not set `customer.identity_verified`: DESIGN.md section 10 gives that to the verification workflow alone, and a deployment that could grant it in a file would open every identity gate in the pack quietly.

- 2026-09-06: Web chat pulled forward out of Phase 7 into a new Phase W, sequenced after Phase 4. Reason: nothing is demonstrable until a person can type at the agent, and every later phase then improves something visible. Phase 7 keeps email, observability, replay, guardrails and the scheduler. Target for a demo is Phases 4, W, 5 and 6 complete.

- 2026-09-06: untrusted data blocks are delimited by a **per-render token** rather than by a fixed marker string, and an `llm` node with no `output_schema` may write **no** state (phase 3 review findings V1 and V2). The first inverts the trust model on the prompt boundary: detecting forged delimiters is an enumeration a Unicode-literate attacker wins, so the delimiter carries a value the writer of the data cannot know. The token is a sha256 commitment over the prompt's own slots rather than `secrets.token_hex`, because the fake provider replays by request fingerprint and identical inputs must render identically; the unpredictability then rests on a 128-bit fixed point being infeasible rather than on randomness. It is stated in the dynamic system block, never in the cached layer 1 to 3 prefix. The second changes what an absent declaration means, so absence is no longer allowed to be silent: `graph.llm_output_schema_absent` refuses it at load and `output_schema: {}` is how a node says it writes nothing.

- 2026-09-06: the `cache_control` breakpoint is conditional on the measured static prefix and a per-model minimum, and logs when it is skipped (phase 3 review finding V3). Anthropic ignores a breakpoint under the model's minimum cacheable prefix without an error, and the sample pack's prefix is about 676 estimated tokens against 1024 for its default model, so DESIGN.md 11.1's prompt caching was not happening. Padding layer 1 to reach the threshold was rejected: it pads a compliance surface to win a cache and would need re-padding whenever the threshold moved.

- 2026-09-05: Single repository for core and sample pack until phase 9, to keep early iteration fast. Split at phase 9.
- 2026-09-05: Tests use real Postgres via docker-compose. Provider calls use a fake with recorded responses; live calls only in an opt-in group.
- 2026-09-05: A `confirm` that satisfies a WRITE or HIGH `tool` node must live in the **same graph** as that tool node (phase 1 review finding F1). DESIGN.md 5.2 asks for "a confirm on all paths" and 8.2 binds an approval to `sha256(tool_name + canonical_json(args))`; the two were ambiguous together, because the interprocedural coverage analysis accepted a caller-side confirm while `requires_approval` could only name a confirm in the tool node's own graph, leaving a write inside a sub-graph unvalidatable. Resolved in favour of the same-graph rule: a caller-side confirm cannot see the argument values the callee computes, so a cross-graph approval could never be verified at run time and accepting it statically would produce packs that fail at execution. The coverage analysis now tracks which *graphs* certainly confirm on every path and discharges a tool node only when its own graph is among them; the rejection messages state the requirement. DESIGN.md section 8.2 records the rule.
- 2026-09-05: One `run` per conversation, for the life of the conversation (phase 0 left this open for phase 2). DESIGN.md 7.1 says "load Run" for the conversation being locked, and a stable run id is what keeps the `run_id:frame_seq:node_id:attempt` step ids of 7.1 stable across turns; a run that reaches the end of its root frame goes to `done`, and the next inbound message pushes a fresh root frame onto the same run. Enforced by `uq_run_conversation` in migration `0002`.
- 2026-09-05: `gate` and `ask` became executable in phase 2 rather than in 4 and 3. DESIGN.md 6.6's "gates fire on every entry to a frame" and 7.2's suspension are phase 2's subject, and neither can be tested without running one; a gate only evaluates an expression and pushes a graph, and an `ask` node's slot extraction - the one part that needs a model - is the `extract_slots` hook, which phase 3 replaces without touching the node. `executable_phase` was corrected to 2 for both.
- 2026-09-05: `support_core.engine.register_node_type` delivers DESIGN.md 6.2's "custom node types are Python classes registered by name in the pack" early. Two of DESIGN.md 7.2's four statuses (`waiting_async_tool`, `waiting_timer`) have no core node type - they belong to phase 4's async tools and to a scheduler - so without it the only way to exercise those rows was to hand-write `run` rows and call it a test. Core node types cannot be replaced through it.
- 2026-09-06: A customer message is claimed and its turn started in **one** transaction, and the node a resume event is delivered to is read from `run.awaiting` rather than from the frame stack (phase 2 review findings R1, R2). Two transactions where the invariant needs one left a window in which a dead process lost the message for good, and no recovery sweep can close it after the fact: a `message` row marked `received` because a node consumed it is indistinguishable from one marked `received` by a claim that never got anywhere, so any sweep keyed on that predicate either misses the orphan or redelivers a consumed message. The claim is therefore also the turn start, and the pre-claim work that needs the message text - the resume-or-new-root-frame decision, and the interrupt check, which from phase 6 is a model call - reads it without claiming it. In the same spirit, "which node is waiting for this event" is written down at suspend time (`awaiting.frame_seq` and `awaiting.node`) and carried on the event, because the stack moves inside a turn whenever a gate re-check or (phase 6) an interrupt pushes a frame.
- 2026-09-06: `pack.yaml` gains a `memory:` block (`summarize_every_turns`, `window_messages`, `max_summary_chars`) and three `llm:` keys (`confidence_threshold`, `max_tool_iterations`, `retries`, plus `max_output_tokens` and `prompt_budget`). DESIGN.md 10 requires "every K turns", 11.3 requires "a pack threshold" and 8.4 a loop bound, and section 5.1's example manifest has nowhere to put any of them. Same precedent and same argument as phase 2's `timeouts:`; the defaults are inert (summarising is off unless a pack asks for it, because it costs a model call per K turns).
- 2026-09-06: the `extract_slots` hook takes a `SlotRequest` rather than `(slots, reply, ctx)`. DESIGN.md 6.2 says an `ask` node "extracts slots via structured output", and a structured schema needs each slot's declared *type*, which a list of names cannot carry; the request also carries the question that was asked, the frame state and the recent window, none of which a name-only signature could express. The `ask` node itself is unchanged, which is what reviews/phase-2.md required of phase 3.
- 2026-09-06: prompt layers 1 to 5 (core rules, persona, policies, node instructions, allowed decisions) **fail closed** when they exceed their token budget rather than truncating. DESIGN.md 10 asks for explicit per-layer budgets and does not say what happens at the limit; silently dropping a policy line is the exact failure the prompt surface exists to prevent, so it is a node error that hands off. Layers 6 to 9 are data and truncate with the truncation stated inside the block.
- 2026-09-06: the conversation (layer 9) is rendered as **fenced data inside the prompt** rather than replayed as native chat turns, and the whole prompt closes with one core-written user message. It is the strongest available reading of principle 7 - a customer message cannot even become a turn boundary - and it is what makes the injection tests meaningful. The cost is conversational fidelity: models are trained on chat structure, and a fenced transcript is not what they saw most of in training. Recorded in reviews/phase-3.md as the trade it is; a phase with a real API key should measure it before it ships.
- 2026-09-05: `pack.yaml` gains a `timeouts:` block, per status with per-channel overrides. DESIGN.md 7.2 requires timeouts "per status and configurable per pack" and contrasts web chat with email; section 5.1's example manifest has nowhere to put them. The defaults never close a conversation unless the pack asks for it.
