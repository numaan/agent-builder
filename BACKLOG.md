# Backlog

Status values: `todo`, `in-progress`, `self-critique`, `in-review`, `resolving`, `done`, `blocked`.
Each phase follows the five-step workflow in [PLAN.md](PLAN.md). Design references point at [DESIGN.md](DESIGN.md).

| Phase | Title | Status | Review file |
|-------|-------|--------|-------------|
| 0 | Skeleton, tooling, database | done | reviews/phase-0.md |
| 1 | Graph model, loader, validator, expression language | done | reviews/phase-1.md |
| 2 | Execution engine and durability | todo | reviews/phase-2.md |
| 3 | LLM layer and prompted nodes | todo | reviews/phase-3.md |
| 4 | Tool runtime and safety nodes | todo | reviews/phase-4.md |
| 5 | Knowledge layer and citations | todo | reviews/phase-5.md |
| 6 | Interrupts, root graph, handoff | todo | reviews/phase-6.md |
| 7 | Channels, observability, replay | todo | reviews/phase-7.md |
| 8 | Evaluation harness | todo | reviews/phase-8.md |
| 9 | Knowledge graph, customer memory, cost controls, packaging | todo | reviews/phase-9.md |
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

Exit criterion: `support pack validate packs/acme_billing` runs and reports the pack is empty but well-formed; `pytest` runs one database smoke test green. Met locally on 2026-09-05 and re-confirmed after review resolution (77 tests green, see reviews/phase-0.md "Resolution").

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

- [ ] `Run`, `Frame`, `SuspendReason`, `ResumeEvent`, `NodeResult`, `NodeRuntime` types.
- [ ] Executor: turn loop from section 7.1 without the LLM-dependent parts (interrupt check is a pluggable hook that defaults to `continue`).
- [ ] Frame stack push, pop, output mapping, and gate re-evaluation on frame entry.
- [ ] Checkpoint after every node in one transaction with the `trace_step` row; deterministic step ids.
- [ ] `trace_step.seq` (the run's `checkpoint_seq` at write time) with a unique `(run_id, seq)` so replay has a total order per run; the executor sets `started_at` from the application clock, not the `now()` default (phase 0 deferred finding F5).
- [ ] Postgres advisory lock per conversation; pending-message queue for messages that arrive while locked.
- [ ] Suspend and resume for `waiting_customer`, `waiting_human`, `waiting_async_tool`, `waiting_timer`; per-status timeouts.
- [ ] Per-turn limits (`max_nodes_per_turn`) with handoff fallback hook.
- [ ] Crash recovery: re-execute the current step on resume; replay completed steps from trace.
- [ ] Hot reload and `PackPin` coherence: `load_pack` reads and parses every graph twice, so a pack edited on disk mid-load can produce a pin describing a mix of two versions; snapshot the directory or hash the bytes actually parsed (phase 1 deferred finding P1).
- [ ] Make `support_core.graph.templates.ENVIRONMENT` per-pack before anything varies per pack or two pack versions are loaded side by side (phase 1 deferred finding P2).
- [ ] Tests: kill-and-resume test that interrupts the executor between checkpoint and advance and verifies identical outcome; concurrent inbound test verifying single-writer ordering.

Exit criterion: the kill-and-resume test passes under Postgres, and two concurrent inbound messages are processed in order with no lost state.

## Phase 3: LLM layer and prompted nodes

Design: sections 11.1 to 11.3, 6.2 (`llm`, `ask`).

- [ ] `LLMProvider` protocol; `AnthropicProvider` using tool-use for structured output and prompt caching for the static prefix; `FakeProvider` that replays recorded responses keyed by prompt hash for tests.
- [ ] Prompt assembly with the fixed nine-layer order and per-layer token budgets; packs cannot reorder.
- [ ] `LlmNodeOutput` schema; decision constrained to the node's edge labels; confidence threshold routing to `unclear`.
- [ ] `llm` node with bounded READ tool loop hook (tool execution itself arrives in phase 4; here the loop calls a stub runtime).
- [ ] `ask` node: suspend, then slot extraction via structured output on resume.
- [ ] Conversation summary memory (section 10) updated every K turns.
- [ ] Tests with `FakeProvider`: llm node picks only allowed edges; malformed model output is retried once then routed to handoff hook; ask node fills slots.

Exit criterion: `packs/acme_billing` has a `root.yaml` with a classify node and a `small_talk` path, and a scripted conversation runs through it with the fake provider.

## Phase 4: Tool runtime and safety nodes

Design: sections 8.1 to 8.4, 6.2 (`tool`, `confirm`, `gate`), 6.4.

- [ ] `Tool`, `Risk`, `ToolContext`; registry with duplicate and schema checks.
- [ ] Risk policy enforcement in the runtime, not in prompts: READ only from llm loops, WRITE and HIGH only from tool nodes with approval, `confirm_exempt` reporting.
- [ ] Idempotency: `tool_call` row keyed by step id; at-most-once for non-idempotent tools.
- [ ] Migration adding `run_id` (FK, indexed) and `step_id` to `tool_call` so calls can be joined to a run and conversation without parsing the idempotency key (phase 0 deferred finding F6).
- [ ] `confirm` node computing `sha256(tool_name + canonical_json(args))`, storing `action_approval`, yes/no edges.
- [ ] `action_approval` is single-use: `consumed_by_tool_call_id`/`consumed_at`, and a consumed approval is treated as absent; covered by the adversarial tests (phase 0 deferred finding N1).
- [ ] `tool` node with `args` expressions, `into` mapping, `on_error`, `requires_approval` hash check.
- [ ] `gate` node pushing the redirect graph and re-evaluating.
- [ ] Async tools completing via callback (`waiting_async_tool`).
- [ ] MCP adapter with mandatory risk map, unknown tools default HIGH.
- [ ] Sample pack tools: `list_recent_charges`, `get_charge`, `check_refund_eligibility`, `issue_refund`, `send_otp`, `verify_otp`, backed by an in-memory fake billing system.
- [ ] `verify_identity.yaml` and `refund.yaml` graphs from section 6.4.
- [ ] The registry built from the imported `TOOLS` is authoritative for risk tiers; the validator reports drift against `tools/tools.yaml` (phase 1 deferred finding I: a HIGH tool declared `read` removes every check today).
- [ ] The engine's approval hash classifies scalars with the same `parse_value` rule the validator canonicalises with, so a static pass is not followed by a run-time refusal (phase 1 review F7, run-time half).
- [ ] Reject a state write, between a `confirm` and the call it approves, to any field the approved arguments read; today the comparison is textual and identical text that evaluates differently passes (phase 1 deferred finding H).
- [ ] Decide whether `confirm_exempt` on a WRITE tool stays an INFO or gains a required reason string now that the registry is authoritative (phase 1 deferred finding J).
- [ ] Adversarial tests: approval hash mismatch refused; WRITE tool from llm loop refused; gate bypass by direct sub-graph entry refused; confirmed amount changed before execution refused.

Exit criterion: adversarial approval tests pass and the refund graph runs with the fake provider through confirm and issue_refund.

## Phase 5: Knowledge layer and citations

Design: sections 9.1 to 9.3, 14 (citation guardrail).

- [ ] `Passage`, `Retriever` protocol, `CompositeRetriever`.
- [ ] `DocumentRetriever`: chunking by headings, embeddings via provider abstraction (fake embedder in tests), pgvector plus tsvector hybrid search, optional model reranking.
- [ ] Migration fixing `doc_chunk.embedding` to `vector(N)` for the chosen embedding model (while the table is empty) and adding the HNSW index; decide whether downgrade should keep the `vector` extension (phase 0 deferred findings N12, N2).
- [ ] `LiveLookupRetriever` routing to READ tools.
- [ ] Ingestion CLI `support pack knowledge sync` for `markdown_dir` and `html_crawl` sources, with `source_version` and stale-chunk marking.
- [ ] `knowledge:` block on llm nodes; passages inserted as delimited data with ids.
- [ ] Outbound citation guardrail: factual-claim classifier (rule-based first), re-prompt once, then handoff.
- [ ] Sample pack knowledge: refund policy and processing-time documents.
- [ ] Test: change a policy document, re-sync, and show the trace of a new answer cites the new `source_version` while an old trace still names the old one.

Exit criterion: the wrong-answer-to-source-version trace test passes.

## Phase 6: Interrupts, root graph, handoff

Design: sections 6.5, 6.6, 13, 7.3.

- [ ] Interrupt check as a structured LLM call producing `continue`, `new_intent`, `cancel`, `unclear`; honours `interrupts.allowed_from` and `blocked_in`; records secondary intents.
- [ ] Return-to-interrupted-workflow prompt after the interrupting workflow ends.
- [ ] Root graph pattern in the sample pack with `refund`, `update_address`, `small_talk`, `unknown`, `handoff`.
- [ ] `HandoffPacket` builder using the escalation model; `handoff` node; `HandoffSink` protocol with webhook and Postgres queue sinks.
- [ ] Desk API: list handoffs, reply, resume with state patch, close.
- [ ] All failure paths from section 7.3 route to handoff with the right reason.
- [ ] Revisit the confirm-coverage control-flow graph for interrupts: model the interrupt push and the return-and-resume, and make `graph.subgraph_cycle` check the cycle path rather than the whole graph (phase 1 deferred finding N3).
- [ ] `update_address.yaml` graph in the sample pack.

Exit criterion: the section 19 worked example runs end to end with the fake provider as an integration test, including the interrupt at step 7 and the secondary intent at step 15.

## Phase 7: Channels, observability, replay

Design: sections 12, 15, 4.1.

- [ ] `ChannelAdapter` protocol; web chat over WebSocket with final-message streaming; email adapter mapping `In-Reply-To` to conversations and batching outbound per turn.
- [ ] FastAPI app factory `create_app(pack)` wiring channels, desk API, health.
- [ ] OpenTelemetry spans for turn, node, llm_call, tool_call, retrieval, guardrail with the attributes in section 15.
- [ ] Metrics listed in section 15.
- [ ] Structured JSON logs with PII redaction applied before emission.
- [ ] `conversation_replay` endpoint rendering the frame stack over time.
- [ ] Inbound guardrails: PII tagging and redaction in traces, injection flag, language detection.
- [ ] Tests: web chat round trip; email thread with a two-day gap resumes the same run; replay output matches trace.

Exit criterion: the service starts with the sample pack, a web chat client completes the refund flow, and the replay endpoint shows the path.

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
- [ ] Per-conversation cost cap and per-turn tool call cap enforced by the engine; cheap model for interrupt checks and summaries.
- [ ] Graph version migration hook (section 6.7) with a test that a changed state shape triggers handoff when no hook exists.
- [ ] Split into two distributable packages: `support-core` and the sample pack depending on a pinned version; Dockerfile for the pack.
- [ ] Sample pack knowledge graph: plans, features, regions.

Exit criterion: the sample pack builds as its own image, starts, and answers "what does my plan include" with a knowledge-graph citation.

## Phase F: Final integration review

- [ ] A fresh agent reviews the whole repository against DESIGN.md sections 3 and 14: every guiding principle and every structural guardrail must be traceable to code and a test.
- [ ] Run the full suite including live-provider golden conversations if `ANTHROPIC_API_KEY` is present.
- [ ] Record open items in "Deferred findings" and write `reviews/final.md`.

---

## Deferred findings

Populated by phase reviews. Format: `- [phase N] finding, severity, reason deferred`.

- [phase 0] F5: `trace_step` has no ordering column; `started_at` defaults to `now()`, which is identical for every step written in one transaction, so replay (DESIGN 7.3) cannot order steps by it, should-fix, deferred to phase 2 which owns the checkpoint transaction and its migration (checklist line added there).
- [phase 0] F6: `tool_call` has no `run_id`, `conversation_id` or `step_id`; the only link to a conversation is the idempotency key string, should-fix, deferred to phase 4 which owns the `tool_call` migration (checklist line added there).
- [phase 0] N1: `action_approval` has no single-use marker or expiry, so one approval row could satisfy two tool calls with the same args hash, nit (a design gap: DESIGN 8.2 does not demand single use), deferred to phase 4 (checklist line added there).
- [phase 0] N2 (downgrade half): the initial migration's downgrade drops the `vector` extension, which fails or removes a shared extension if an administrator pre-installed it, nit, deferred to phase 5 which owns pgvector; editing the initial migration for a shared-instance concern is not a risk-free few lines. The superuser requirement is documented in README.
- [phase 0] N9: two concurrent `pytest` processes against one database corrupt each other's fixtures (reviewer measured 47 passed, 8 errors); README warns but nothing enforces it, nit, deferred because a session-long advisory lock needs a connection that outlives the per-test event loops, which the fixture design avoids on purpose. The dedicated `support_test` database (F2) removes the developer-database half of the risk.
- [phase 1] N3: `graph.subgraph_cycle` fires only when *no* graph in the cycle contains a suspending node anywhere, not one on the cycle path, so mutual recursion through a graph with an unrelated `ask` on an untaken branch is missed (reviewer's hostile case T, reproduced after resolution), nit, deferred to phase 6, which owns interrupts and has to rebuild the cross-graph control-flow model for the push and resume edges anyway; a path-sensitive cross-graph cycle check is not a few safe lines (checklist line added there).
- [phase 1] H: the approval-argument comparison is textual, so a `tool` node that rewrites a field the approved arguments read, between the confirm and the call, passes `graph.approval_mismatch` (reviewer's hostile case H, still accepted after resolution), should-fix, deferred to phase 4, which owns the run-time hash check DESIGN.md 8.2 says exists for exactly this and can enforce the static half beside it (checklist line added there).
- [phase 1] I: risk tiers come from the pack-authored `tools/tools.yaml`, so declaring `issue_refund` as `read` removes every confirm check and makes it callable from an `llm` loop (reviewer's hostile case I), should-fix, deferred to phase 4, which replaces the manifest with the registry built from the imported `TOOLS` and must report drift; phase 1 cannot import pack code (checklist line added there).
- [phase 1] J: a WRITE tool marked `confirm_exempt` is reported as INFO only, which does not fail `--strict` and forces no human to look (reviewer's hostile case J), nit (DESIGN.md 8.2 asks for exactly this reporting), deferred to phase 4, which owns the registry and can require a reason string (checklist line added there).
- [phase 1] P1: `load_pack` reads and parses every graph twice, so a pack edited on disk while it runs can produce a `PackPin` whose hashes describe a mix of two versions, nit, deferred to phase 2, which owns hot reload (checklist line added there).
- [phase 1] P2: `templates.ENVIRONMENT` is a process-wide Jinja environment with a shared template cache; harmless while nothing varies per pack, a concurrency bug the moment a pack supplies a filter or two pack versions load side by side, nit, deferred to phase 2 (checklist line added there).
- [phase 0] N12: `doc_chunk.embedding` is dimensionless, so no HNSW index is possible and mixed-dimension rows fail only at query time, nit (admitted by the implementer, measured by the reviewer), deferred to phase 5 which picks the embedding model (checklist line added there).

---

## Decisions log

- 2026-09-05: Single repository for core and sample pack until phase 9, to keep early iteration fast. Split at phase 9.
- 2026-09-05: Tests use real Postgres via docker-compose. Provider calls use a fake with recorded responses; live calls only in an opt-in group.
- 2026-09-05: A `confirm` that satisfies a WRITE or HIGH `tool` node must live in the **same graph** as that tool node (phase 1 review finding F1). DESIGN.md 5.2 asks for "a confirm on all paths" and 8.2 binds an approval to `sha256(tool_name + canonical_json(args))`; the two were ambiguous together, because the interprocedural coverage analysis accepted a caller-side confirm while `requires_approval` could only name a confirm in the tool node's own graph, leaving a write inside a sub-graph unvalidatable. Resolved in favour of the same-graph rule: a caller-side confirm cannot see the argument values the callee computes, so a cross-graph approval could never be verified at run time and accepting it statically would produce packs that fail at execution. The coverage analysis now tracks which *graphs* certainly confirm on every path and discharges a tool node only when its own graph is among them; the rejection messages state the requirement. DESIGN.md section 8.2 records the rule.
