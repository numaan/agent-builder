# Backlog

Status values: `todo`, `in-progress`, `self-critique`, `in-review`, `resolving`, `done`, `blocked`.
Each phase follows the five-step workflow in [PLAN.md](PLAN.md). Design references point at [DESIGN.md](DESIGN.md).

| Phase | Title | Status | Review file |
|-------|-------|--------|-------------|
| 0 | Skeleton, tooling, database | self-critique | reviews/phase-0.md |
| 1 | Graph model, loader, validator, expression language | todo | reviews/phase-1.md |
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
- [x] CI config (GitHub Actions) running ruff, mypy, pytest with a Postgres service. (Workflow file written and mirrors the local commands; it has not yet executed on GitHub because nothing has been pushed.)

Exit criterion: `support pack validate packs/acme_billing` runs and reports the pack is empty but well-formed; `pytest` runs one database smoke test green. Met locally on 2026-09-05 (52 tests green, see reviews/phase-0.md).

## Phase 1: Graph model, loader, validator, expression language

Design: sections 5.2, 6.1 to 6.4, 6.7.

- [ ] Pydantic schema for graph files: id, description, inputs, outputs, state, start, nodes, edges.
- [ ] Node type registry with core types `router`, `say`, `end`, `subgraph`; `llm`, `ask`, `tool`, `gate`, `confirm`, `handoff` registered as declared-but-not-executable stubs so the validator can check them now.
- [ ] Sandboxed expression language: attribute access on `state`, `ctx`, `result`; comparisons; `and`, `or`, `not`; literals; a fixed filter set (`money`, `lower`, `len`, `default`). Parser plus evaluator plus type inference against Pydantic models. No `eval`.
- [ ] Jinja2 templates for `say`, `ask`, and `confirm` prompts with a sandboxed environment.
- [ ] Loader: `load_pack(path)` reads `pack.yaml`, all graphs, persona, policies; resolves sub-graph references.
- [ ] Validator implementing every rule in section 5.2, including the confirm-on-all-paths rule (implemented now against declared tool risk tiers, tools may be stubs).
- [ ] Graph version pinning data structure (section 6.7) recorded on the pack object.
- [ ] `support pack validate` prints findings with file and node locations.
- [ ] Tests: expression language property tests, validator tests with one failing fixture per rule, loader round-trip.

Exit criterion: a deterministic graph using only `router`, `say`, `subgraph`, `end` executes in a unit test through a minimal in-memory stepper, and the validator rejects each malformed fixture with the right rule name.

## Phase 2: Execution engine and durability

Design: sections 6.3, 7.1 to 7.3, 17.

- [ ] `Run`, `Frame`, `SuspendReason`, `ResumeEvent`, `NodeResult`, `NodeRuntime` types.
- [ ] Executor: turn loop from section 7.1 without the LLM-dependent parts (interrupt check is a pluggable hook that defaults to `continue`).
- [ ] Frame stack push, pop, output mapping, and gate re-evaluation on frame entry.
- [ ] Checkpoint after every node in one transaction with the `trace_step` row; deterministic step ids.
- [ ] Postgres advisory lock per conversation; pending-message queue for messages that arrive while locked.
- [ ] Suspend and resume for `waiting_customer`, `waiting_human`, `waiting_async_tool`, `waiting_timer`; per-status timeouts.
- [ ] Per-turn limits (`max_nodes_per_turn`) with handoff fallback hook.
- [ ] Crash recovery: re-execute the current step on resume; replay completed steps from trace.
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
- [ ] `confirm` node computing `sha256(tool_name + canonical_json(args))`, storing `action_approval`, yes/no edges.
- [ ] `tool` node with `args` expressions, `into` mapping, `on_error`, `requires_approval` hash check.
- [ ] `gate` node pushing the redirect graph and re-evaluating.
- [ ] Async tools completing via callback (`waiting_async_tool`).
- [ ] MCP adapter with mandatory risk map, unknown tools default HIGH.
- [ ] Sample pack tools: `list_recent_charges`, `get_charge`, `check_refund_eligibility`, `issue_refund`, `send_otp`, `verify_otp`, backed by an in-memory fake billing system.
- [ ] `verify_identity.yaml` and `refund.yaml` graphs from section 6.4.
- [ ] Adversarial tests: approval hash mismatch refused; WRITE tool from llm loop refused; gate bypass by direct sub-graph entry refused; confirmed amount changed before execution refused.

Exit criterion: adversarial approval tests pass and the refund graph runs with the fake provider through confirm and issue_refund.

## Phase 5: Knowledge layer and citations

Design: sections 9.1 to 9.3, 14 (citation guardrail).

- [ ] `Passage`, `Retriever` protocol, `CompositeRetriever`.
- [ ] `DocumentRetriever`: chunking by headings, embeddings via provider abstraction (fake embedder in tests), pgvector plus tsvector hybrid search, optional model reranking.
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

---

## Decisions log

- 2026-09-05: Single repository for core and sample pack until phase 9, to keep early iteration fast. Split at phase 9.
- 2026-09-05: Tests use real Postgres via docker-compose. Provider calls use a fake with recorded responses; live calls only in an opt-in group.
