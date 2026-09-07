# Bookkeeping audit, 2026-09-07

Auditor: an agent that wrote none of the code and none of the reviews. Read-only pass over
`BACKLOG.md`, `PLAN.md`, `DESIGN.md`, `README.md`, `reviews/phase-0.md` to `phase-6.md` and
`reviews/phase-w.md`, the git history, and the committed source. No file in the repository was
modified except this one, nothing was committed, no test was run and no database was touched.
Phase 5 was in flight throughout (`support_core/knowledge/`, `guardrails/outbound.py`,
`storage/knowledge_repo.py`, migration `0010`, the Qdrant service) and is treated as work in
progress, not as a finding.

## Verdict

The numbered bookkeeping works and the review chain is intact: all seven closed phases have all
five PLAN.md sections, every independent review states it was written by an agent that did not
write the code, every resolution states the same, and **no numbered deferred finding has been
lost to a phase that closed without addressing it** — the three deferred to phase 6 are struck
through and genuinely fixed, the three routed to phase 5 are all named in that phase's live plan,
and nothing was ever deferred to phase W. What fails is everything the numbered system does not
cover. Four concerns raised in *self-critique and forward-compatibility prose* were never promoted
to a numbered entry, have no owner and no checklist line, and one of them — a `handoff` does not
clear the approval set, raised in the phase-1 self-critique and pointed squarely at the phase that
would introduce the human — is still true in `rules.py` after phase 6 built the desk, the
`approve` endpoint and the human-in-the-loop it warned about. A second structural hole is the GLM
provider: a whole second provider, a deployment-level model override and two edits to the
structured-output parsing path shipped "outside the phase structure" with no plan, no
self-critique, no independent review and no resolution — the only change in the project to skip
all five steps. DESIGN.md, meanwhile, has been amended twice (not once), but drifts from reality
in fourteen identifiable places, worst among them section 17's data model, which now misdescribes
every table the engine writes. Two decision-log entries dated the same day reach opposite
conclusions about DESIGN.md 7.3 and neither is marked superseded.

---

## 1. Deferred-findings trace

Source: `BACKLOG.md` "Deferred findings" (lines 389–436) cross-referenced against the Resolution
section of each review and against the owning phase's checklist.

| # | Finding | Deferred by | Owner now | Checklist line in owner? | Status in code | Verdict |
|---|---------|-------------|-----------|--------------------------|----------------|---------|
| F5 | `trace_step` has no ordering column | phase 0 | phase 2 | yes, `BACKLOG.md:64` | `trace_step.seq` + `uq_trace_step_run_seq`, migration `0002` | **Closed, correctly struck** |
| F6 | `tool_call` has no `run_id`/`step_id` | phase 0 | phase 4 | yes, `BACKLOG.md:99` | migration `0006` adds `run_id`, `step_id`, `node_id` | **Closed, correctly struck** |
| N1 | `action_approval` has no single-use marker | phase 0 | phase 4 | yes, `BACKLOG.md:101` | `consumed_by_tool_call_id`/`consumed_at`, migration `0006` | **Closed, correctly struck** |
| N2 | downgrade drops the `vector` extension | phase 0 | phase 5 | yes, `BACKLOG.md:183` | open; named in `reviews/phase-5.md:6` | Open, owned, in flight |
| N9 | two concurrent pytest processes corrupt fixtures | phase 0 | *(none)* | **no owning phase named** | half fixed in phase 6 (`lock_timeout` + retry in `tests/conftest.py`); the session-long-lock half open | Open, **no owner** — the entry names no phase, only a reason |
| N12 | `doc_chunk.embedding` is dimensionless | phase 0 | phase 5 | yes, `BACKLOG.md:183` | open; named in `reviews/phase-5.md:6` | Open, owned, in flight |
| N3 | `graph.subgraph_cycle` is not path-sensitive | phase 1 | phase 6 | yes, `BACKLOG.md:204` | path-sensitive in `rules.py:305`; hostile case T now an error | **Closed, correctly struck** |
| H | approval-argument comparison is textual | phase 1 | phase 4 | yes, `BACKLOG.md:110` | `graph.approval_args_mutated`, `rules.py:678,689` | **Closed, correctly struck** |
| I | risk tiers read from pack-authored `tools.yaml` | phase 1 | phase 4 | yes, `BACKLOG.md:108` | `tools.registry_drift` (ERROR), `tools_source.py:108` | **Closed, correctly struck** |
| J | `confirm_exempt` reported INFO only | phase 1 | phase 4 | yes, `BACKLOG.md:111` | `confirm_exempt_reason` required, WARNING, `rules.py:1360` | **Closed, correctly struck** |
| P1 | `load_pack` parses every graph twice | phase 1 | phase 2 | yes, `BACKLOG.md:70` | `report.graph_sources`, `loader.py:58` | **Closed, correctly struck** |
| P2 | process-wide Jinja `ENVIRONMENT` | phase 1 | phase 2 | yes, `BACKLOG.md:71` | `Pack.environment`, `pack.py:152` | **Closed, correctly struck** |
| R7 | connection-per-waiter on the conversation lock | phase 2 | phase 7 | yes, `BACKLOG.md:216` | **customer half delivered in phase W** (`api/drain.py`, `BACKLOG.md:122`); scheduler half open | Open — but the entry at `BACKLOG.md:404` is **stale**: it still reads as wholly deferred and never records that phase W closed half of it |
| R6 | node-type registry is process-wide | phase 2 | phase 9 | yes, `BACKLOG.md:270` (verify) | open | Open, owned |
| — | LLM calls not replayed from the trace | phase 3 (critique, endorsed by review) | phase 7 | yes, `BACKLOG.md:236` | open; `trace_step.llm_response` written, never read | Open, owned. **DESIGN.md 7.3 still asserts this works** — see drift D9 |
| — | web chat endpoints have no authentication | phase W | phase 7 | yes, `BACKLOG.md:245` | desk half closed (`desk.py:164`, `tests/test_desk_auth.py`); customer key, origin check, rate limit, webhook signature open | Open, owned. Entry correctly re-opened and annotated |
| — | one shared desk token is not per-operator identity | phase W | phase 7 | yes, `BACKLOG.md:245` | open | Open, owned |
| — | W16's replacement: transport detail, `customer_ref`, refusable delivery | phase W | phase 7 | yes, three lines `BACKLOG.md:239–241` | open | Open, owned |
| — | live delivery is process-local | phase W | phase 7 | yes, `BACKLOG.md:238` | open | Open, owned |
| — | outbound message identity and turn boundary | phase W | phase 7 | yes, `BACKLOG.md:242` | open | Open, owned |
| R8 | `state.charge: Charge` types as `Any` | phase 4 | phase 9 | yes, `BACKLOG.md:271` (verify) | open | Open, owned |
| R9 | per-turn tool budget counts only model-loop calls | phase 4 | phase 9 | yes, `BACKLOG.md:272` (verify) | open | Open, owned |
| — | `graph.confirm_exempt` names the tool, not the arguments | phase 4 | phase 6 | yes, `BACKLOG.md:205` | `rules.py:1370` lists every call site with its argument expression | **Closed, correctly struck** |
| — | `on_error` edge returning to its own `tool` node | phase 4 | phase 6 | yes, `BACKLOG.md:206` | `graph.on_error_repeats_side_effect` (`rules.py:1417`) + `limits.max_node_errors` (`manifest.py:148`, enforced `executor.py:1655`) | **Closed, correctly struck** |
| P2 | handoff nobody took, durable half | phase 6 | phase 7 | yes, `BACKLOG.md:222` | open | Open, owned |
| P6 | queued message not counted against SLA | phase 6 | phase 7 | yes, `BACKLOG.md:228` | open | Open, owned |
| — | spans and a cheap model for the two out-of-node LLM calls | phase 6 | phase 7 | yes, two lines `BACKLOG.md:219,232` | open | Open, owned |
| — | retriever seam through `gather()` / `HandoffSummaryRequest` | phase 6 | phase 5 | yes, `BACKLOG.md:186` | open; named in `reviews/phase-5.md:6` | Open, owned, in flight. Never entered "Deferred findings" — only the checklist |

**Headline:** twelve deferrals closed and correctly struck through; sixteen open with a named
owner and a checklist line. **Nothing numbered was lost to a closed phase.** One entry (N9) has no
owning phase at all, and one (R7) is materially out of date.

### 1a. Deferrals that never became entries — the genuinely orphaned four

These were raised in self-critique or forward-compatibility prose, are not in "Deferred findings",
have no owner and appear in no checklist line anywhere.

| # | Item | Raised in | Still true? | Why it matters |
|---|------|-----------|-------------|----------------|
| O1 | **A `handoff` does not clear the approval set.** "A human agent can act between the confirm and the call… 'last customer input' is arguably the wrong boundary once a human is involved." | `reviews/phase-1.md:314–317` (self-critique item 5) | **Yes.** `rules.py:957–965` `covered_out` kills coverage only for `ConfirmNode` and `AskNode`; `HandoffNode` passes it through unchanged | Phase 6 introduced the human this item was waiting for — the handoff node, the desk `resume` with state patch, and `approve` — and neither its plan nor its review revisits the confirm boundary. It lands on the one rule PLAN.md calls "the most valuable in the system" |
| O2 | **`customer_input_types()` is dead and its docstring is false.** `support_core/graph/nodes.py:328` says "The confirm-on-all-paths analysis in `.rules` treats these as the 'last customer input' points"; `git grep customer_input_types` returns the definition and nothing else | not raised anywhere | Yes | The mechanical form of O1. `register_node_type` (`runners.py:1146`) places no restriction on `suspends`, so a pack-registered node type suspending into `waiting_customer` is a customer-input point that the confirm analysis does not treat as one |
| O3 | **`doc_chunk.tsv` hard-codes `english`**; a per-source language needs a column and a regenerated expression | `reviews/phase-0.md:301` (forward compatibility, "Phase 5") | Yes | Phase 5 is being written right now and does not carry this line. DESIGN.md 21 open question 3 is multi-language packs; this is the concrete blocker and nobody owns it |
| O4 | **No CHECK constraints on `status`, `direction`, `author`, `risk`, `approved_by`** — "any string is accepted until the owning phases define vocabularies" | `reviews/phase-0.md:138–139` (self-critique) | Yes | Phases 2, 4 and 6 each defined one of those vocabularies and none added the constraint. The deferral was conditional on an event that has now happened three times |

A fifth, lower-value one: `updated_at` is maintained client-side and goes stale after a raw
`UPDATE`, "deferred until something reads the column" (`reviews/phase-0.md:141`). Nothing reads it
yet, so the condition still holds, but there is no entry to catch it when something does.

### 1b. An obligation the decisions log took on and never discharged

`BACKLOG.md:702` (2026-09-06) records the fenced-transcript trade and ends: "a phase with a real
API key should measure it before it ships." A real key arrived the same day — `BACKLOG.md:642`,
"First live model run of the project, against GLM" — and no measurement was made, recorded, or
turned into a checklist line.

---

## 2. DESIGN.md drift

DESIGN.md has been amended **twice**, not once: `72825d7` (phase 1, section 8.2 same-graph
confirm rule) and `b8e2c8e` (phase 6, section 7.3 two failure tiers, plus two new rules in 6.6 and
an inline comment in 5.1). Everything below is undocumented drift.

| # | Section | Line | Design says | Reality |
|---|---------|------|-------------|---------|
| D1 | **17** Data model | 711–723 | Column lists for every table | Every table has grown and none of it is here: `conversation` + `channel_key`, `inbound_seq`, `turn_count`, `summary_turn`; `message` + `ordinal`, `queue_seq`; `run` + `pack_fingerprint`, `turn_nodes`, `turn_tool_calls`, `awaiting`, `secondary_intents`; `trace_step` + `seq`, `error`; `tool_call` + `run_id`, `step_id`, `node_id`, `error`, `attempts`, `finished_at`, `context_patch`; `action_approval` + `run_id`, `frame_seq`, `node_id`, `step_id`, `args`, `consumed_by_tool_call_id`, `consumed_at`; `handoff` + `run_id`, `reason`, `graph_id`, `node_id`, `step_id`. Ten migrations, `0002`–`0010`, none reflected. Section 17 is now the least accurate page in the document |
| D2 | **20** Availability | 785 | "Postgres is the single stateful dependency" | False since `7596c60`. Qdrant is a second stateful service in `docker-compose.yml:34` and in CI. The relaxation is accepted knowingly in the decisions log (`BACKLOG.md:616`) but the design still asserts the old claim |
| D3 | **4.1** Topology | 115 | "one service, one Postgres database" | Same as D2 — one Postgres *and* one Qdrant per domain |
| D4 | **4.1** Topology | 115 | "Horizontal scaling is safe because all execution state is in Postgres" | True of execution, not of delivery. The web chat connection registry is a dictionary in one process (`channels/web_chat.py`), so a second replica delivers only to sockets it holds. Recorded as a phase-7 deferral (`BACKLOG.md:410`); the design still states it flatly |
| D5 | **9.1** Backends | 520 | "Three backends implement one `Retriever` protocol"; DocumentRetriever is "pgvector embeddings plus Postgres full-text" | Four backends, and the dense half moves out of Postgres: `ColbertRetriever` over Qdrant multivectors with MaxSim, dense vectors in Qdrant, lexical in Postgres, merged by `CompositeRetriever`, versioned by collection alias. Decided `BACKLOG.md:612,616`; section 9.1 unchanged |
| D6 | **11.1** Provider | 578 | "`AnthropicProvider` is the first implementation"; "Model choice is per pack with per-node override" | GLM is a second provider (`support_core/llm/glm_provider.py`), and model choice now has a third, higher level: `AppConfig.models` (`api/config.py:88`) overrides a pack's model ids per deployment. `ProviderChoice` is `auto | replay | anthropic | glm | none` (`config.py:30`) |
| D7 | **11.2** Prompt assembly | 582–592 | A flat list of nine layers; "Layers 1 to 3 are identical across turns and are cached" | Two additions. A per-render delimiter token — a sha256 commitment over the prompt's slots — is stated in a **dynamic system block** that sits outside the nine, deliberately not in layer 1 (`llm/prompt.py:41–43,453`), and layer 9 is rendered as the single closing *user* message rather than as a system layer. And the caching claim is conditional, not automatic: the `cache_control` breakpoint is skipped when the measured prefix is under the model minimum, which is the sample pack's actual case (676 estimated tokens against 1024) — `BACKLOG.md:717` |
| D8 | **11.3** / **6.2** | 600, 202 | `state_updates: dict # validated against node output_schema`; nothing says the schema is required | `output_schema` is mandatory. `graph.llm_output_schema_absent` (`rules.py:452`) refuses a node without one at load; `output_schema: {}` is how a node declares it writes nothing (`nodes.py:116`). This was review finding V2, a safety property, and it is not in the design |
| D9 | **7.3** Failure handling | 433 | "LLM calls replay from the trace if the step already completed" | Not implemented. `trace_step.llm_response` is written and never read back; deferred to phase 7 (`BACKLOG.md:406`). The design describes it in the present tense as a property the engine has |
| D10 | **7.1** Turn loop | 398, 407 | `guardrails.inbound(message)` and `guardrails.outbound(result.outbound)` in the loop | Neither exists yet. `support_core/guardrails/` was a docstring stub until the in-flight phase 5 began adding `outbound.py`; inbound is phase 7 |
| D11 | **12** ChannelAdapter | 616 | `async def send(self, conversation: Conversation, msg: OutboundMessage)` | `send` takes a frozen `ConversationRef` (`channels/base.py:102`), not the ORM row. Decided deliberately (`BACKLOG.md:723`); the protocol in the design is now wrong as written |
| D12 | **12** Web chat | 620 | "Streaming of the final message only" | There is no streaming. The adapter sends a committed message after the checkpoint; token streaming is explicitly refused until outbound guardrails run before delivery (`BACKLOG.md:121`) |
| D13 | **12** Human desk | 622 | "not a customer channel but uses the same API surface to inject human replies and to resume or close" | Silent on the two things that now govern it: the desk is **off by default** and **refuses to start without a bearer token** (`api/config.py:123,140,237`). It also does more than "reply, resume, close" — it has `approve` and a transcript endpoint (`api/desk.py:290,378`). And it is mounted on the customer's own listener, which section 12's own wording argues against |
| D14 | **22** Roadmap | 803–813 | Nine phases; phase 7 delivers "Channels: web chat, email"; phase 9 is the last | Web chat is its own Phase W, closed before 5 and 6 (`BACKLOG.md:723`, decisions log). The roadmap now runs 0–4, W, 5–10, 12, F: phase 10 (mem0), phase 12 (`support pack` authoring + DSPy) and phase F (final integration review) do not appear, phase 11 was created and then folded away, and phase 9 gained packaging and the core/pack split |

Two smaller ones, worth a line to a later editor: **8.1**'s `Tool` class (447–467) omits
`patches_context`, the frozenset added by phase 4 finding R4 that is the whole identity gate
(`tools/base.py`, decision at `BACKLOG.md:659`); and **13**'s `HandoffPacket` (631–646) declares
`citations: list[Passage]`, which is unconditionally empty until phase 5 threads a retriever
through `gather()`, while **6.2**'s `handoff` row (211) gives the node a `reason` and two edges
where it now also has optional `message` and `next_steps` (`graph/nodes.py:190,201`).

`PLAN.md` drifts the same way in three places: its Goal still says "all nine phases of section 22"
(now fourteen), its repository layout describes docker-compose as "Postgres 16 + pgvector" only,
and its Environment needs table lists an Anthropic key with no mention of GLM.

---

## 3. Checklist honesty

### 3a. Ticked but not built as written

Nothing is fabricated. Of the 49 file paths named across the seven closed phases, 47 resolve.
Four naming inaccuracies:

| # | Location | Claim | Actually |
|---|----------|-------|----------|
| H1 | `BACKLOG.md:200` (phase 6) | "Root graph pattern in the sample pack with `refund`, `update_address`, `small_talk`, `unknown`, `handoff`" | `packs/acme_billing/graphs/root.yaml:42–50` declares `refund`, `update_address`, `small_talk`, `account_question`, `refused`, `finished`, `unclear`. There is no `unknown` edge and no `handoff` edge — handoff is reached through the `no_workflow` node (root.yaml:83). Two real paths, `refused` and `finished`, are not recorded at all. This is the only ticked line whose named artefacts do not exist |
| H2 | `BACKLOG.md:60` (phase 2) | "`Run`, `Frame`, `SuspendReason`, `ResumeEvent`, `NodeResult`, `NodeRuntime` types (`engine/types.py` and `runners.py`)" | Five of six are there. There is **no `Run` type** in either module; the only `Run` is the ORM row (`storage/models.py:173`) and the executor's equivalent is the private `_RunRow` (`executor.py:230`) |
| H3 | `BACKLOG.md:96` (phase 4) | "`Tool`, `Risk`, `ToolContext` (`tools/base.py`, `registry.py`)" | `Risk` lives in `tools/risk.py:20` and is only imported into `base.py:29`. Cosmetic |
| H4 | `BACKLOG.md:54` (phase 1 exit criterion) | names `tests/test_stepper.py` and `tests/stepper.py` | Both deleted. Disclosed at `BACKLOG.md:74` ("`tests/stepper.py` is deleted: the executor replaced it"), so the record is honest, but line 54 still points at two files that are gone |

Everything else verifies. Spot-checked in substance rather than by name where a claim asserted
behaviour: phase 6's P3 fix genuinely reads `decision.confidence` against the threshold
(`executor.py:793`); phase 4's `context_patch` is read back, not write-only (`tools/runtime.py:543`);
phase W's W3 fix genuinely broadcasts to every watching connection (`api/runtime.py:190`);
`tests/verify_phase_2_resolution.py` is genuinely a CI step of its own.

One long-standing caveat is still accurate and still unowned: `BACKLOG.md:36` admits CI "has not
yet executed on GitHub because nothing has been pushed". `git remote -v` is empty after seven
closed phases. Every "N tests green" number in this project is one local Windows box. No backlog
line anywhere asks for this to change.

### 3b. Built but unrecorded

| # | What | Added by | Recorded where |
|---|------|----------|----------------|
| U1 | **The GLM provider** — `support_core/llm/glm_provider.py`, `demo/acme_web_chat_glm.json`, `tests/test_glm_provider.py`, plus 80 changed lines in `anthropic_provider.py`, the `AppConfig.models` override, and two edits to `llm/schemas.py` | `d9eb09e`, `5711ac2`, `04b8a4f` | Decisions log only (`BACKLOG.md:637–646`) and README. **No checklist line in any phase**, and no `reviews/` file — see §5 |
| U2 | **The sample pack's address tools** `get_address` and `set_address` (`packs/acme_billing/tools/address.py:129,138`), and the `account_question`/`refused`/`finished` root paths | `3d27d98` | Nowhere. Phase 4's sample-tools line (`BACKLOG.md:106`) enumerates exactly six tools and stops; phase 6 records only the *graph* |
| U3 | **Modules no checklist line names.** `graph/routing.py` (the cross-graph CFG that phase 6's lines 204–205 claim without naming), `channels/hub.py` (`ChannelHub`), `api/runtime.py` (`AppRuntime`, including the W3 broadcast), `graph/tools_manifest.py`, `tools/approval.py`, `tools/loading.py`, `memory/summary.py`, `llm/recording.py` | phases 1, 3, 4, 6, W | Covered in substance by a ticked line, named by none. A reader auditing module-by-module finds no entry |
| U4 | **Desk authentication** is recorded under phase W (`BACKLOG.md:128`), not phase 6, which built the desk. Phase 6's line 202 describes the desk with no mention of auth, and phase 6 closed before it had any | `750c35f` then `8720a81` | Both halves recorded, in different phases. Cross-referenced in phase 6's exit-criterion prose, so not lost — but phase 6's checklist reads as complete when it was not |

---

## 4. Contradictions in the Decisions log

**C1 — DESIGN.md 7.3's middle tier, decided both ways on the same day, neither marked superseded.**

- `BACKLOG.md:541` (2026-09-06): "**DESIGN.md 7.3 is amended to two failure tiers**, dropping
  'otherwise the frame's `on_error` graph'… Adding the tier later is additive and needs a use case
  first."
- `BACKLOG.md:596` (2026-09-06): "**DESIGN.md 7.3's middle failure tier… is still not
  implemented**, for the third phase running… Recorded again rather than invented."

The second entry's whole point is to keep the tier in the design as an unimplemented note; the
first removes it from the design outright. The first is what actually happened (commit `b8e2c8e`
edits DESIGN.md:433), so the second is now false and reads, to anyone scanning the log, as the
current position. Same date, no supersession marker, 55 lines apart.

**C2 — the desk's port, argued in opposite directions inside 24 hours.**

- `BACKLOG.md:452–457` (2026-09-07): the desk "stays on the same application rather than moving to
  its own port… Section 4.1 makes a deployment one service and section 12 says the desk 'uses the
  same API surface', **so a second listener is a change to the design rather than a reading of
  it**."
- `BACKLOG.md:250` (phase 7 checklist, written in the same resolution): "Take the desk off the
  customer's listener… a `create_desk_app` on its own port costs nothing and **matches what
  section 12 means by 'not a customer channel'**."

Both readings of section 12 cannot be right. The engineering decision (keep it here until
`LISTEN/NOTIFY` fan-out exists) is sound and well argued; the *design reading* offered to justify
it is contradicted by the very next thing the same author wrote.

**C3 — phase 11 was created and quietly dissolved, and the creating entry still stands.**

`BACKLOG.md:630` (2026-09-06) adds "mem0 and DSPy… as phases 10 and 11". `BACKLOG.md:602` (same
day) folds DSPy into phase 12. Phase 11 no longer exists anywhere — the status table skips from 10
to 12 — but the entry that created it is unannotated. A reader hits it and looks for a phase 11.

**C4 — `provider: "auto"` is described in the log in terms the code outgrew the same day.**

`BACKLOG.md:719`: "`provider: 'auto'` is a live Anthropic account where there is an API key and
the recorded cassettes where there is not." Since `d9eb09e`, `resolve_provider` (`api/config.py:262–267`)
falls through Anthropic → GLM → replay → none. Phase W's exit criterion (`BACKLOG.md:140–144`)
inherits the stale reading and still says the live half is unproven "because there is no
`ANTHROPIC_API_KEY`" — while `BACKLOG.md:642` records that a live model run happened, against the
same provider class through a different base URL, and found two real defects. Not a contradiction
of intent, but two entries in one file that cannot both be current.

---

## 5. Review-chain completeness

PLAN.md requires five steps per phase, with step 4 written by an agent that did not write the code.

| Phase | 1 Plan | 2 Implementation | 3 Self-critique | 4 Independent review | 5 Resolution | Independence stated | Gap |
|-------|--------|------------------|-----------------|----------------------|--------------|---------------------|-----|
| 0 | ✓ `phase-0.md:5` | ✓ `:76` | ✓ `:116` | ✓ `:212` | ✓ `:371` | `:214` "independent agent… did not write any of the phase-0 code"; resolver fresh `:373` | Plan has **no standalone commit** — folded into scaffolding `55f2cb0`. Only closed phase without one |
| 1 | ✓ `:6` | ✓ `:124` | ✓ `:225` | ✓ `:405` | ✓ `:693` | `:407` "wrote none of the phase-1 code"; resolver fresh `:695` | none |
| 2 | ✓ `:7` | ✓ `:151` | ✓ `:250` | ✓ `:435` | ✓ `:671` | `:437` "an agent that did not write the code"; resolver fresh `:673` | none |
| 3 | ✓ `:7` | ✓ `:159` | ✓ `:254` | ✓ `:469` | ✓ `:799` | `:471` "a fresh agent that did not write this code"; resolver fresh `:801` | One orchestrator snapshot commit inside the reviewed range, disclosed at `:473` |
| 4 | ✓ `:9` | ✓ `:120` | ✓ `:247` | ✓ `:469` | ✓ `:697` | `:471` "a separate agent that did not write this code"; resolver fresh `:699` | none |
| 6 | ✓ `:8` | ✓ `:151` | ✓ `:290` | ✓ `:435` | ✓ `:607` | `:437` "an agent that did not write the code"; resolver fresh `:609` | Verification numbers re-run because two runs collided with a concurrent reviewer; disclosed `:613` |
| W | ✓ `:6` | ✓ `:107` | ✓ `:225` | ✓ `:444` | ✓ `:587` | `:446` "a separate agent that did not write this code"; resolver fresh `:589` | **Step 2's verification run was done by the orchestrator**, not the implementer — disclosed in a sixth section, `phase-w.md:384–389`: "The implementing agent was interrupted by a session rate limit… The orchestrator ran the verification and committed the phase." Corroborated by git: every phase-W commit is co-authored `Claude Fable 5.1` except `5173eee`, co-authored `Claude Opus 5` |
| 5 | ✓ `phase-5.md:13` | — | — | — | — | — | In progress. Status cell at `BACKLOG.md:13` still reads `todo` although a plan is committed (`d7c49c8`) and code has landed (`7596c60`, `2a28eaf`) |

**No closed phase skipped a step, and no independent review was written by the implementer.**
Every phase also has a distinct commit for step 4 and step 5, and five phases disclose the same
practice of committing the review first so the resolving diff reads against exactly what was
reviewed.

**The phase-W README correction was made.** The orchestrator's verification section claimed the
README said `/health` where the endpoint is `/healthz`. The independent reviewer caught it as
finding W12 (`phase-w.md:489`, "not reproducible"), it is retracted in place at
`phase-w.md:415–421` ("That was not true and is struck rather than left standing"), recorded in
the resolution table at `:620`, and committed as `cba859c`. Verified independently: no revision of
README.md in the whole history contains a bare `/health`; `032fb6a:README.md:190` and the current
`README.md:232` both say `GET /healthz`. The README needed no change because it was never wrong.
One residue: the retraction lives only in `reviews/phase-w.md`. BACKLOG.md's 2026-09-07 entries
cover W1, W4, W6, W8 and W9 and never mention W12.

**The real review-chain hole is not in a phase.** The GLM provider (`d9eb09e`, `5711ac2`,
`04b8a4f`) is a second provider, an 80-line change to `AnthropicProvider`, a new deployment-level
model override, and two coercions added to `llm/schemas.py` — the structured-output parsing path
that phase 3's review finding V2 exists to protect. It shipped with **none** of PLAN.md's five
steps: no plan, no self-critique, no independent review, no resolution, no `reviews/` file. The
decisions log excuses it as "outside the phase structure, because the deployment needs it"
(`BACKLOG.md:637`) and then states an open risk with no owner: "a third instance of the same
family should be expected" (`:646`). The claim that "neither relaxes review finding V2" is, on my
reading of the diffs, correct — but nobody independent has ever checked it, which is the point of
the rule it skipped.

---

## 6. What I would fix first

Ranked by how much a later phase suffers if it is left.

1. **Give O1 an owner and a numbered entry — a `handoff` does not clear the approval set.**
   `rules.py:957–965` treats only `confirm` and `ask` as customer-input boundaries. Phase 6 shipped
   the human agent, the desk `resume` with state patch and `approve`; the boundary question phase 1
   flagged for exactly that moment was never revisited. Fix O2 at the same time — wire
   `customer_input_types()` into `covered_out` or delete it and correct its docstring, because a
   pack-registered `waiting_customer` node type is a hole in the same rule today. Phases 7 and 8
   both build on the confirm analysis; phase 8's core adversarial suite will be written against it.
2. **Correct DESIGN.md 17, and D9's "LLM calls replay from the trace."** Phase 7 is the next phase
   and both land on it directly: it writes the replay endpoint against a data model the design
   misdescribes in every table, and it owns the replay-from-trace item the design already claims is
   done. A phase-7 agent reading section 7.3 in good faith will believe a property the engine does
   not have.
3. **Resolve C1 and C2 in the decisions log, and refresh the two stale entries.** Mark
   `BACKLOG.md:596` superseded by `:541`, reconcile the two readings of section 12 at `:452` and
   `:250`, annotate the phase-11 entry at `:630`, and update the R7 entry at `:404` to record that
   phase W closed its customer half. These are cheap and they are the entries a phase-7 agent will
   read first, because phase 7 owns the scheduler, the desk surface and the fan-out all three
   describe.

Then, in a second pass: bring O3 to the phase-5 agent while that phase is still open (the
hard-coded `english` tsvector is a one-line checklist addition now and a migration later); give O4
an owner now that all three vocabularies are defined; add a checklist line for a first CI run, so
"green" stops meaning one Windows box; correct H1's root-graph edge names; and record the GLM
provider as work with a review owed, rather than as a decision.
