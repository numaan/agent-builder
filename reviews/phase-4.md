# Phase 4 review: Tool runtime and safety nodes

Design references: DESIGN.md sections 8.1 to 8.4, 6.2 (`tool`, `confirm`, `gate`), 6.4, 7.2, 7.3,
17, and principles 1, 3 and 7 of section 3.
Backlog: BACKLOG.md "Phase 4" plus the deferred findings assigned to it (phase 0 F6 and N1,
phase 1 H, I and J, and the run-time half of phase 1 F7). Inherits every settled decision in
reviews/phase-0.md, phase-1.md, phase-2.md and phase-3.md.

## Plan

Written before any code, per PLAN.md step 1.

### What this phase is really about

This is the phase where the system can move money. Every other phase can be wrong and produce a
bad conversation; this one can be wrong and produce a refund nobody asked for, or two refunds
where the customer asked for one. So the plan is ordered by blast radius, not by checklist
order, and three properties come before everything else:

1. **Nothing executes a WRITE or HIGH tool without a matching, live approval.** Enforced inside
   the one code path that can invoke a tool, so no node type - core or pack-registered - can
   route around it. The validator's static proof (phase 1) is the first half; this is the second,
   and it assumes nothing about whether the first ran.
2. **A retry cannot refund twice.** The `tool_call` row is claimed under the deterministic step
   id *before* the tool runs, and a non-idempotent tool whose outcome is unknown is never run
   again.
3. **The read-only gateway stays read-only.** Phase 3 built it so phase 4 could not widen it;
   phase 4's job is to supply a runner that reports the truth and to add a second lock inside
   the runtime itself, so a bug in the gateway is not sufficient to execute a write.

### Task breakdown

1. **`support_core/tools/`** - the runtime, in five small modules.
   - `base.py`: `Tool` (DESIGN.md 8.1's model, verbatim where it gives a shape), `ToolContext`,
     `FunctionTool` for packs that would rather write a function than a class, and the error
     vocabulary (`ToolError`, `ToolRefused`, `ToolTimeout`).
   - `registry.py`: `ToolRegistry` built from a pack's `TOOLS`; rejects duplicate names, rejects
     a tool whose input or output model is not JSON-schema serializable, and is the *only*
     source of a risk tier at run time.
   - `loading.py`: import a pack's `tools/` package and build the registry; turn an import
     failure into a validation finding rather than a stack trace.
   - `approval.py`: `canonical_args` and `approval_hash` = `sha256(tool_name +
     canonical_json(args))`, one function used by the `confirm` node and by the `tool` node.
   - `runtime.py`: `ToolRuntime` - risk policy, approval check and consumption, idempotency,
     execution with a timeout, and the `tool_call` row. Plus `RegistryToolRunner`, the
     `ModelToolRunner` phase 3 left a socket for.
   - `mcp.py`: `McpToolAdapter(client, risk_map)` per DESIGN.md 8.3.
2. **Migration `0006_tool_runtime`.** `tool_call` gains `run_id` (FK, indexed), `step_id`,
   `node_id`, `error`, `context_patch`, `attempt_count` and `finished_at`; `action_approval`
   gains `run_id`, `frame_seq`, `node_id`, `args`, `consumed_by_tool_call_id` and `consumed_at`.
   That is phase-0 deferred finding F6 and phase-0 N1 in one migration.
3. **`confirm` node.** Evaluate `action.args` from the frame's state through the same
   `value_of`/`parse_value` path the `tool` node uses, coerce through the tool's input model,
   hash, render the prompt, suspend `waiting_customer`. On resume, classify the reply; on `yes`
   record the `ActionApproval` and take the `yes` edge; on `no` take the `no` edge and record
   nothing; on an unclear reply, re-ask rather than guessing in either direction.
4. **`tool` node.** Evaluate `args`, invoke through the per-node capability, map `into` (both
   forms), route failures to `on_error`, suspend `waiting_async_tool` for an async tool and
   complete it on the callback.
5. **The seam.** `NodeRuntime.tools` is a capability object built by the executor per node,
   holding a closure that already knows this node's id, step id, frame, its single declared
   tool name (if it is a `tool` node) and its `requires_approval`. A node cannot widen it and
   cannot reach the runtime under it - the same shape phase 3's review finding V4 forced on the
   model-loop gateway.
6. **Validator changes.** The registry becomes authoritative for risk tiers (phase-1 finding I);
   drift against `tools/tools.yaml` is reported; `confirm_exempt` requires a reason and is a
   WARNING (finding J); a new rule refuses a state write between a `confirm` and the call it
   authorises (finding H).
7. **The sample pack becomes real.** An in-memory fake billing system, six tools, and the
   `verify_identity.yaml` and `refund.yaml` graphs of DESIGN.md 6.4, reachable from `root.yaml`.
8. **Adversarial tests**, as a suite of their own, each written so that removing the enforcement
   makes it fail.

### Intended deviations from DESIGN.md and why

- **The approval hash is taken over arguments coerced through the tool's input model**, not over
  the raw evaluated values. DESIGN.md 8.2 says `sha256(tool_name + canonical_json(args))` and
  does not say which `args`. Coercing first means `29` and `29.0` for a `float` input hash alike
  - which is right, because they *are* the same call - while a difference the model does not
  erase is still a mismatch. It also means the hash covers exactly the values the tool will
  receive, since `model_validate` drops anything the input model does not declare. Both the
  `confirm` node and the `tool` node use one function, so the two cannot drift.
- **The approval is bound to more than the design says.** DESIGN.md 8.2 binds it to
  `(tool, args_hash)`; this phase also binds it to `(conversation, run, frame_seq, confirm node
  id)` and makes it single-use. That is phase-0 deferred finding N1 plus the observation that a
  frame sequence is never reused (phase 2), so an approval from an earlier invocation of the
  same graph is refused even before single-use gets a chance to matter.
- **Tools may patch the conversation context.** DESIGN.md 19 step 9 says `verify_otp` "sets
  `ctx.customer.identity_verified = true`", and section 6.1 says `ctx` is *read-only to all
  nodes*. Both hold: the node does not write it, the tool does, through its `ToolContext`, and
  the executor commits the patch in the same transaction as the step. Only a WRITE or HIGH tool
  may patch, and only a node the graph declares as `type: tool` may carry a patch out.
- **`confirm_exempt` gains a required reason and is reported at WARNING**, not INFO (phase-1
  deferred finding J). DESIGN.md 8.2 asks for exemptions to be "reviewed deliberately"; an INFO
  line in a report that exits 0 is not review, and a required sentence makes the author write
  down the argument the reviewer would otherwise have to reconstruct.
- **A `confirm` node's yes/no reading is a hook**, defaulting to a conservative keyword matcher
  and replaced by a structured model call when a provider is configured - the same shape as
  phase 3's `extract_slots`. DESIGN.md 6.2 says "require an explicit yes" and does not say who
  decides; putting it behind the same seam keeps the engine's own tests model-free.
- **The MCP adapter is written against a client protocol** (`list_tools`, `call_tool`) that the
  `mcp` package's `ClientSession` satisfies structurally, rather than owning a transport. There
  is no MCP server in this environment, and an adapter tested against a fake transport is honest
  about what it has and has not proved.
- **`issue_refund` is declared `idempotent: false`.** DESIGN.md 8.1 defaults to true and its
  example is silent. At-most-once is the honest setting for money: a crash whose outcome is
  unknown must reach a human, not a retry.
- **`root.yaml` gains a `refund` edge and a `subgraph` node.** The exit criterion needs the
  refund graph to run inside the sample pack. This changes every prompt the classify node
  produces, so all four phase-3 cassettes are re-recorded by their own generator; the test that
  compares the committed cassette with a fresh recording is what proves the re-recording is
  faithful.

### What is deliberately not in this phase

The web chat channel (Phase W), retrieval and citations (phase 5), the interrupt check and the
`handoff` node (phase 6), the desk API that would create a *human* approval, LLM replay from the
trace (phase 7), and a per-conversation cost cap (phase 9).

## Implementation notes

Environment: Windows 11, Python 3.13.14, pydantic 2.13.5, SQLAlchemy 2, asyncpg, PyYAML 6.0.3,
`mcp` 1.x (already a declared dependency; no new one was added). Postgres 16 in the
`customer-support-agent-db-1` container, database `support_test`. `ANTHROPIC_API_KEY` is not set,
so every model call in this phase went through phase 3's `ScriptedProvider` or `FakeProvider`.

Seven commits, `e65e4a6..b66f3be`: the plan, the runtime, the engine wiring, the refund
conversation, the adversarial suite, the two node-type defences that mutation testing found, and
the documentation.

### Shape of the code

```
support_core/tools/
  base.py        Tool, FunctionTool, ToolContext, the error vocabulary (8.1)
  risk.py        the tiers, plus needs_confirm(), the one place that decision is made
  approval.py    canonical_args, canonical_json, approval_hash (8.2)
  registry.py    ToolRegistry: duplicate names, JSON-schema check (8.3)
  loading.py     importing a pack's tools/ package under a path-derived module name
  runtime.py     ToolRuntime: policy, approval, idempotency, execution, the tool_call row
  mcp.py         McpToolAdapter over a client protocol; HIGH for an undeclared tier (8.3)
support_core/graph/tools_source.py   which source a pack's tools come from, and drift
support_core/engine/runners.py       NodeToolAccess, ConfirmRunner, ToolRunner
support_core/storage/migrations/versions/0006_tool_runtime.py
packs/acme_billing/tools/            billing.py, identity.py, and TOOLS
packs/acme_billing/graphs/           refund.yaml, verify_identity.yaml, root's refund edge
tests/tool_support.py                countable tools, and unvalidated_pack
tests/test_tool_runtime.py           the runtime, driven directly (32 tests)
tests/test_tool_registry.py          registry, pack import, drift, MCP (19 tests)
tests/test_adversarial_approvals.py  the suite (17 tests)
tests/test_refund_flow.py            the exit criterion's assertions about the money (5)
tests/test_tool_crash_recovery.py    a real OS process killed inside the tool (2)
tests/packs/hostile_pack/            five graphs the validator refuses, run anyway
tests/packs/crash_pack/              one tool that can stop the process mid-side-effect
```

### The three properties, and where each one is enforced

**Nothing executes a WRITE or HIGH tool without a matching approval.** Three checks, in three
places, none of which relies on another:

1. `ToolRuntime._authorise` consumes an `action_approval` matching the tool, the argument hash,
   the conversation, the run, the frame sequence and the confirm node the graph named - or
   raises, inside the transaction that claimed the idempotency key, so a refused call has not
   even spent its key. This is the check that holds when the validator did not run.
2. The executor grants an *invoking* capability only to a node the validated graph declares as
   `type: tool`, and only over that node's own declared tool name. Everything else - an `llm`
   node, a `say` node, a node type the pack registered - holds a closure that refuses.
3. Only a node the graph declares as `type: confirm` may produce an approval at all, checked in
   the loop where a violation is routed like any other node failure.

The `confirm` node hashes the action **when it shows it** and carries the hash on the
suspension, so the customer's answer is read against what they were asked rather than against
whatever the state says by the time they reply. A frame can be re-entered in between - a gate
whose predicate lapsed pushes its redirect before the reply is delivered - and that is not a
theoretical window: `test_the_identity_gate_cannot_be_skipped_by_losing_verification_mid_workflow`
walks through it.

**A retry cannot refund twice.** The `tool_call` row is inserted, and the approval consumed, in
one transaction *before* the tool runs. A completed row replays without running anything. A row
left `running` is a call whose outcome nobody knows, and what happens then depends on the tool's
own `idempotent` flag: repeat it, or refuse and mark the row `indeterminate`. The refusal writes
that mark in its own committed transaction and is raised afterwards - the first version raised
inside the claim transaction and rolled the mark back, which a test caught.

**The read-only gateway stays read-only.** Phase 3's gateway refuses a non-READ tier before the
runner is spoken to; `ToolRuntime.invoke` refuses it again for `caller="model_loop"`, holding a
valid approval or not. `RegistryToolRunner.describe` reports each tool's *true* tier, because
reporting the truth is what lets the gateway refuse; the sample pack's `find_charge` node is the
positive case and `tests/packs/hostile_pack/graphs/loop_tools.yaml` the negative one.

### Things worth knowing that came up while building

- **A pack imported twice is two modules.** `import_pack_tools` loads `<pack>/tools/__init__.py`
  under a name derived from its path, so two packs whose package is called `tools` do not shadow
  each other (DESIGN.md 6.7 keeps two versions loaded side by side). The consequence caught me
  out: `packs.acme_billing.tools.billing.BILLING` and the registry's copy are different objects,
  so a test resetting the first left the second holding yesterday's refunds, and the refund
  scenario silently took the denial path on its second run. Packs now expose `reset_backend()`
  and the tests call it through `import_pack_tools`.
- **Validating a pack now runs the pack's Python.** That follows from DESIGN.md 8.3 and there is
  no way round it, but it changes what `support pack validate` means, so an import that raises
  is a finding (`tools.import_failed`) rather than a traceback, and a half-imported module is
  removed from `sys.modules` so the next attempt does not find it and believe the pack loaded.
- **`extra="forbid"` on a tool's input model is load-bearing.** An argument the model does not
  declare is refused rather than dropped, so it can reach neither the hash nor the tool. A tool
  whose model allowed extras would have them dropped by `model_validate` before hashing, which
  is still sound - the hash covers what the tool receives - but the refusal is better, and the
  test says which is which.
- **`graph.approval_reused` was missing a shape.** Phase 1's rule caught a *cycle* back into a
  confirmed tool node. Writing `tests/packs/hostile_pack/graphs/replay.yaml` - one confirm, then
  two different tool nodes both naming it - showed the validator accepting it. The rule now also
  refuses a second tool node bound to the same confirm and reachable from the first. Two nodes on
  mutually exclusive branches are still fine, because only one of them can run.
- **The circular foreign key needed `use_alter`.** `tool_call.approval_id` and
  `action_approval.consumed_by_tool_call_id` point at each other, which leaves
  `Base.metadata.sorted_tables` unable to order the tables - and the test fixtures truncate that
  list. The hint is for `create_all` only; the migrations are the schema.
- **A `ToolRefused` escaping a custom node type used to kill the turn.** A pack-registered node
  is not obliged to translate the runtime's vocabulary, and a refusal is a run-time failure of a
  node, which DESIGN.md 7.3 routes. The loop now converts it, with `tool_refused` and
  `tool_failed` as distinguishable handoff reasons.
- **All four phase-3 cassettes were re-recorded**, because `root.yaml` gained a `refund` edge and
  every classify prompt therefore changed. `python -m tests.cassettes.build_cassettes` does it,
  and `test_the_committed_cassette_is_what_the_builder_produces` is what proves the committed
  files are what the generator produces.

### Commands run at the end of the phase

All from the repository root with `.venv/Scripts/python.exe`, Windows 11, Docker container
`customer-support-agent-db-1` healthy, database `support_test`.

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) |
| `python -m ruff format --check .` | `124 files already formatted` (exit 0) |
| `python -m mypy` (strict) | `Success: no issues found in 124 source files` |
| `python -m pytest -q` | `1138 passed, 2 deselected in 288.88s` |
| `python -m pytest -q -m live` | `2 skipped, 1138 deselected` |
| `python -m pytest -q tests/verify_phase_2_resolution.py` | `39 passed` |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (9 warning(s))`, exit 0 |
| `python -m alembic downgrade base` | down to base, no errors |
| `python -m alembic upgrade head` | `Running upgrade 0005 -> 0006` |
| `python -m alembic check` | `No new upgrade operations detected.` |
| `python -m tests.cassettes.build_cassettes` | five cassettes, re-recorded byte-identically |

## Self-critique

Written after re-reading DESIGN.md sections 3, 6.2, 6.4, 7.2, 7.3, 8.1 to 8.4, 17 and 19, PLAN.md,
and the four earlier reviews.

### What did I skip or simplify?

- **A pack's Python is trusted, completely.** This is the biggest thing to say about the phase and
  it is not a defect I can fix inside it. Importing a pack executes its code in-process; a tool is
  a function with the service's database credentials, its network and its memory. Every control
  this phase adds - the tiers, the approval, the idempotency key - constrains *the graph and the
  model*, and none of it constrains the pack author. A tool that declares itself READ and moves
  money is callable from a model loop with no confirmation, and nothing in core can tell. That is
  the design's trust boundary (DESIGN.md 8.3 has no other source of a risk tier than the exported
  `Tool`), and phase-1 finding I is closed in the sense that a *file the author might forget to
  update* no longer governs anything - but "the pack author is trusted" should be written on the
  front of the box, and today it is written only in module docstrings.
- **`requires_human_approval` cannot be satisfied.** The runtime looks for a second approval with
  `approved_by = 'human'`, and nothing can create one until phase 6's desk. A pack that sets the
  flag has a tool that always refuses. That is the safe direction and it is tested, but it means
  the fourth column of DESIGN.md 8.2's table has never run green.
- **The `ctx.customer` patch is one capability, not several.** Any WRITE or HIGH tool may set
  `identity_verified`. `send_otp` could, if the pack author were careless. A tool declaring which
  context fields it may write would be better and is maybe fifteen lines; I did not do it because
  it adds a field to the public `Tool` shape that DESIGN.md does not have, and I would rather a
  reviewer decided that than me.
- **A refused call leaves no `tool_call` row.** The claim is rolled back so that a refusal does
  not burn the idempotency key, which means the audit answer to "show me every attempted refund"
  is in `trace_step.error` rather than in the table built for tool calls. For a compliance story
  (DESIGN.md 20) that is the wrong table.
- **The per-turn tool budget still does not count `tool` nodes.** `limits.max_tool_calls_per_turn`
  counts model-loop calls only (that is what phase-3 finding V6 fixed). A graph that loops through
  five `tool` nodes makes five calls against a limit of ten and none of them is counted;
  `max_nodes_per_turn` is the only thing bounding it. I left it because widening the counter
  changes what an existing manifest key means, which is a decision, not a fix.
- **No `nodes/` directory loading.** DESIGN.md 5 has an optional `nodes/` directory and 6.2 says
  custom node types are "registered by name in the pack". `register_node_type` exists (phase 2)
  and the tests use it, but `load_pack` still does not import a pack's `nodes/`. Phase 9's
  registry-scoping item (R6) should probably own both.
- **The MCP adapter has never spoken to a real server.** It is written against the two methods
  `mcp.ClientSession` offers and tested against a fake. The tier default, the wrapping, the
  argument model and the result flattening are all real and tested; the transport, the handshake,
  authentication and reconnection are not, and no pack wires one in, so no MCP tool has ever gone
  through a graph.
- **Async tools are thin.** `Tool.async_` dispatches, suspends, and completes from a callback.
  There is no poller, no timeout on the dispatch beyond the pack's `waiting_async_tool` rule, and
  the callback payload is trusted: whoever can call `resume_async_tool` decides what the tool
  returned, and the approval was already spent at dispatch. Phase 7 owns the endpoint that will
  receive one, and it will need to authenticate it.

### Where does the code diverge from the design?

- **The approval is bound to more than DESIGN.md 8.2 says**, and the hash is taken over coerced
  arguments. Both are recorded as decisions in BACKLOG.md. The binding is strictly stronger, so
  the risk is a pack that is *refused* where the design would allow it: a legitimate second call
  of the same action needs a second confirmation. I think that is right - "one approval, one
  action" is what a customer means by yes - but it is a choice.
- **`ctx` is written by tools.** DESIGN.md 6.1 says the context is read-only to all nodes and 19
  step 9 has a tool set `identity_verified`. Reconciled by having the tool ask and the engine
  write, in the checkpoint transaction. Nothing else in the design describes this path.
- **A confirmation has three answers.** DESIGN.md 6.2 gives the node two edges; `unclear` is not
  an edge, it re-presents. A pack cannot route on it.
- **`confirm_exempt` gained a required reason and became a warning** (finding J's decision).
  `--strict` now fails on any pack with an exemption, including the sample pack, until a human
  has read the reason. That is intended and it is a real cost: `make check` does not use
  `--strict`, so what actually enforces it is a release gate that does not exist yet (phase 8).
- **The validator imports pack code.** DESIGN.md 5.2 says "load_pack parses everything, resolves
  references, and runs a validator before the service accepts traffic" and 8.3 says the registry
  comes from the export; putting the two together means `support pack validate` runs the pack.
  Defensible, and now documented in the README, but it changes what a validation command is.
- **`graph.approval_reused` covers a shape the design never mentions** (two tool nodes, one
  confirm). It is an addition in the same spirit as phase 1's other additions.
- **The sample pack's `refund.yaml` is DESIGN.md 6.4's graph with additions**: `on_error` on
  `check_eligibility` and `issue_refund`, `escalate_dispute`/`lookup_failed`/`refund_failed` as
  `say` nodes instead of `handoff` nodes (which are not executable until phase 6), and a router
  `default`. The confirm and the call are byte-identical to the design's.

### What could an attacker or a buggy pack still achieve?

Ordered by how much I would worry, and all of these are things I could not talk myself out of.

1. **A pack author can do anything.** See above. The model cannot, the customer cannot, the graph
   cannot; the pack can. If the threat model ever includes a pack, everything here has to be
   re-argued behind a sandbox.
2. **The approval covers the arguments, not the sentence.** The customer approves a rendered
   prompt - "I can refund $29.00 for Pro Plan (Sep 3)" - and the hash covers `charge_id` and
   `amount`. A pack whose prompt renders a *different* charge's description than the one in
   `charge_id` would be approved for what it asked, not for what it showed. The sample pack reads
   both from the same object, so it is coherent; nothing enforces that it must be. Hashing the
   rendered prompt alongside the arguments would close it and would also make the prompt
   un-editable between question and answer, which may be too strict.
3. **The model chooses the charge, and the sample account has two identical ones.** `find_charge`
   is an `llm` node that writes `charge_id`; the confirmation then shows an amount and a
   description that are the same for `ch_1001` and `ch_1002`. The customer cannot tell which they
   are approving. The compensating control - a human sees the money before it moves - is weaker
   than it looks whenever two charges are indistinguishable in the prompt. That is a pack defect
   more than a core one, and the pack is the one I wrote.
4. **A gate redirect between a confirmation and the call is not analysed.**
   `graph.approval_args_mutated` walks intra-graph edges; a `gate` names a *graph*, so a redirect
   whose sub-graph runs a WRITE tool that patches `ctx` is invisible to it. The run-time hash
   check still catches an argument that actually changed, so the consequence is a refusal on a
   customer's turn rather than a load-time error - the exact shape finding H was about, one level
   out. Phase 6 has to rebuild the cross-graph control-flow model anyway (deferred finding N3);
   this belongs with it.
5. **`confirm_exempt` remains an escape hatch with a sentence in front of it.** A pack that marks
   `charge_card` exempt and writes a plausible reason gets a warning nobody has to read. The
   reason is now *present*, which is what makes review possible, but review is still a human
   habit rather than a gate.
6. **An unclear confirmation can be asked for ever.** Each unclear reply re-presents the proposal.
   It costs a customer message per iteration, so it is not a resource attack, but there is no
   "let us not do this then" after N tries and no handoff.
7. **A `tool` node's arguments can be anything the graph computes**, including values the model
   wrote into state. The confirmation is the control, and it is a real one - the hash is over
   exactly those values. But a WRITE tool that is `confirm_exempt` takes its arguments from the
   same place with no confirmation at all: `send_otp` sends to `ctx.customer.email`, which is
   fine, and a pack that made it `state.email` from a model-written field would be sending
   passcodes wherever the model said. Nothing warns about that.
8. **Two conversations, one customer.** Everything is scoped to a conversation. The same customer
   in two conversations can be verified in one and unverified in the other, and can have two
   approvals for two refunds of the same charge - the *tool* refuses the second (the fake billing
   system checks `refunded_by`), which is the pack doing the work core does not.

### Which tests are weak?

- **The MCP tests are all fake-client tests.** They prove the adapter's decisions, not that it can
  talk to anything. `test_an_mcp_tool_goes_through_the_same_registry_policy_and_key` is the most
  valuable one because it proves an MCP tool is not a second execution path, but the transport is
  untested by construction.
- **The `live` group is still two skipped tests.** No API key, so the confirm classifier and the
  refund conversation have never met a real model. The scripted rules key on substrings of node
  instructions, which is a stand-in for a classifier and not a classifier;
  `test_an_unclear_answer_is_asked_again_rather_than_read_as_a_yes` exercises the *keyword*
  default over four phrasings, and the model-backed reading is exercised only through a cassette
  that always says yes.
- **The concurrency test is in-process.** Two `Executor` objects on one engine genuinely contend
  for the advisory lock (asyncpg gives each operation its own connection), but phase 2 set a
  higher bar with two OS processes, and the money-moving race is exactly where that bar should be
  met. The database-level race (`test_two_racing_callers_cannot_both_spend_one_approval`) is the
  stronger of the two and is not process-level either.
- **`test_the_identity_gate_cannot_be_skipped...` asserts the last node is `send_code` or
  `ask_code`.** That is a disjunction, which is a smell: it is written that way because the
  cassette's next interaction depends on how far the redirect gets. A tighter assertion would pin
  one.
- **Nothing tests two pack versions side by side**, which is where `import_pack_tools`'
  path-derived module name earns its keep. `test_two_packs_with_a_tools_package_each_do_not_
  shadow_one_another` uses two *different* packs, not two versions of one, and the pin machinery
  is not involved.
- **The async tool tests use a tool that dispatches synchronously.** There is no test of a
  callback arriving days later, or of two callbacks for one dispatch, or of a callback for a
  conversation that has moved on.
- **No property or differential test for the canonical form.** Phase 1 has one for `unparse`;
  `canonical_json` has hand-written cases only. A hypothesis property over argument mappings
  (same values in any order hash alike; different values do not) would be cheap and I did not
  write it.

### Which adversarial tests did I verify are load-bearing?

Each enforcement was switched off in the source, the suite re-run, and the source restored. Every
one is caught; the counts are how many tests failed in
`test_adversarial_approvals.py`, `test_tool_runtime.py`, `test_tool_crash_recovery.py`,
`test_refund_flow.py` and `test_tool_loop.py`.

| enforcement removed | tests that fail | the ones that matter |
|---|---|---|
| the approval check entirely (`_authorise` returns `None`) | 15 | hash mismatch refused; no-confirm refused; one approval, one call; two concurrent turns |
| the runtime's model-loop tier check | 1 | `test_a_write_tool_is_refused_from_a_model_loop_even_with_an_approval` - the *only* test of the second lock, because phase 3's gateway is the first and still holds |
| at-most-once (a non-idempotent call is retried) | 2 | the in-process one and the cross-process one, which counts side effects in a file |
| the confirm's shown-versus-about-to-approve comparison | 1 | the amount changed under the customer's answer |
| single use (`consumed_at IS NULL`) | 2 | one approval cannot authorise a second call |
| the run/frame/node binding | 17 | an approval from another frame, another confirm node, or another tool |
| only a `confirm` node may record an approval | 1 | a pack-registered node type forging one |
| only a `tool` node may change `ctx.customer` | 1 | a node type declaring the customer verified |
| only a `tool` node may invoke | 1 | and the approval check *still* refused the call, which is the defence in depth working |

Two of these tests exist because the mutation testing found nothing failing: "only a `confirm`
node may record an approval" and "only a `tool` node may change `ctx.customer`" were both
implemented and both untested until the mutation said so. That is the strongest argument for
doing it at all, and I would not have found either by reading.

What the table does not prove: that the *set* of attacks is complete. It proves each check I
wrote is checked. The attacks I did not think of are not in it.

### What would break under concurrency or a crash mid-step?

- **A crash between the claim and the tool** leaves a `running` row and no side effect, and the
  retry re-executes for an idempotent tool or refuses for a non-idempotent one. The refusal is
  the conservative answer to a state that is genuinely ambiguous, and it costs a handoff on a
  call that never happened. A tool that could be *asked* whether it happened (a payment provider
  with a lookup) would do better, and DESIGN.md has no place for one to say so.
- **A crash between the tool and its result row** is the case the phase is built around and is
  tested across a real process boundary. What is not tested: a crash between the result row and
  the *checkpoint*, where the tool call is durable and the step is not. The step re-executes,
  replays the recorded result, and proceeds - correct by construction, and asserted only
  indirectly by `test_the_same_step_replays_a_completed_call_without_running_it_again`.
- **A crash inside the claim transaction** rolls back both the row and the approval consumption,
  which is the point of putting them together. A crash *between* the claim commit and the
  execution leaves the approval consumed and the call unmade; the retry finds the row and re-uses
  its `approval_id` rather than consuming a second, so the customer's one yes still buys one
  attempt.
- **Two turns of one conversation** are serialised by the advisory lock, and the approval consume
  is atomic underneath it, so the lock is not what makes it safe. Two *conversations* share
  nothing except the pack's in-memory fake, which is a property of the sample pack and not of the
  engine.
- **The in-memory fakes are per-process.** `BILLING` and `OTP` live in one interpreter. Two service
  replicas would have two of each, which no test would notice because the tests are one process.
  A real pack has a service behind it; a demo with two workers would behave strangely, and the
  README should probably say so.
- **`ctx` is rebuilt per turn from the conversation row and mutated in memory during it.** A tool
  that patches the customer mid-turn is visible to the rest of that turn and durable at the
  checkpoint. If a node *after* the patch fails and the turn hands off, the patch is already
  committed - which is right (the tool really did verify the identity) but means a handoff can
  leave the context ahead of the workflow.
- **`_maybe_summarize` still reads and writes in two transactions** under the conversation lock,
  which phase 3's review noted; nothing in this phase makes it worse.

### What I fixed while writing this

Two things, both committed rather than recorded: an idempotency key re-entered with *different*
arguments now refuses instead of executing the new ones under the old approval (`14c5e32`), and
the two node-type defences the mutation testing showed were untested got the tests that
mutation-kill them (`78da4a5`, `c73effa`). The rest of this section stands as written.

## Independent review

Reviewer: a separate agent that did not write this code. Reviewed commits `e65e4a6..e4a9e08`
against DESIGN.md sections 3, 4.1, 6.2, 6.4, 7.2, 7.3, 8.1-8.4 and 17, PLAN.md, BACKLOG.md and
reviews/phase-0.md to phase-3.md.

**Verdict.** The money is safe. I ran twenty-five distinct approval-bypass attacks against the
runtime and could not get a WRITE or HIGH tool to execute without a live, matching, unconsumed
`action_approval`: not by reusing an approval from another run, frame, confirm node, tool or
conversation; not by re-recording the confirm step to un-consume one; not by racing two callers
for one row; not by approving one argument form and executing another; not through the model
loop even holding a valid approval; not by smuggling a tool through the `confirm_exempt` slot.
The read-only gateway held under every attack including a forged READ spec for a HIGH tool, and
the runtime's second lock held when I bypassed the gateway entirely. The exit criterion holds
independently of the implementer's tests, and it is node logic that drives it: replacing the
cassette-backed confirm classifier with a hook that answers `no` produces zero approvals and
zero refunds, and `unclear` re-presents. Every claim in the implementer's command table
reproduced exactly. The one thing I did break is the async tool: an async `tool` node dispatched
by the executor can never be completed by its callback, because the idempotency key it is looked
up under is not the one it was claimed under - and on a graph whose `on_error` returns to the
tool node, that made a WRITE tool's side effect happen **twice** from one customer intent and one
callback. That is a must-fix, and the self-critique's "async tools are thin" does not reach it.
The rest is should-fix and nit. On the headline caveat: the pack-code trust boundary is
acceptable per DESIGN.md 4.1, but the `ctx.customer` write capability inside it is not, and
should be narrowed (see the last subsection).

### Findings

| id | severity | location | finding | suggested fix |
|----|----------|----------|---------|----------------|
| R1 | must-fix | `support_core/engine/executor.py:726-727` and `:896`; `support_core/tools/runtime.py:197-216` | An async `tool` node's callback can never find its dispatched call, and the failure can repeat the side effect. `_run_node` computes `attempt = frame.attempts.get(node_id, 0)`; the dispatching pass then increments `frame.attempts[node_id]` in `_advance` before checkpointing. So the dispatch claims `run:frame:node:0` and the `resume_async_tool` pass looks up `run:frame:node:1`. `complete_async` finds no row and raises `ToolRefused("no dispatched call ... to complete")`. The `tool_call` row is stranded at `awaiting_callback` for ever. With `on_error` pointing back at the tool node, the node re-enters at attempt 2, claims a *third* key, and dispatches again: I measured the handler running twice for one intent and one callback. A tool needing an approval is saved by single-use (the approval was consumed at dispatch, so the retry is refused) - but a `confirm_exempt` WRITE async tool repeats without limit, and DESIGN.md 7.2's `waiting_async_tool` status is unusable either way. No test covers the executor round trip: `test_an_async_tool_is_dispatched_and_completed_by_its_callback` calls `ToolRuntime.complete_async` directly with the same `CallSite`, and `tests/test_engine_suspension.py` suspends into `waiting_async_tool` through a *registered custom node type*, not a real `tool` node. | Carry the dispatch's idempotency key on the suspension detail the way `confirm` carries `args_hash`, and have `NodeToolAccess.complete` use it; or take the attempt for a resuming node from the run's suspension record rather than from the incremented counter. Add an executor-level test: dispatch, resume, assert one `succeeded` row and one handler invocation. |
| R2 | should-fix | `support_core/tools/runtime.py:602-626` (`RegistryToolRunner.invoke`) | The model-loop path never re-checks the node's allow-list. `ToolRuntime.invoke` accepts `allowed=`, and the `tool_node` path passes it, but `RegistryToolRunner.invoke` omits it, so the `tools:` list is enforced in exactly one place - `ReadOnlyToolGateway`. I called `RegistryToolRunner.invoke("peek", ...)` for a runner whose node declared only `charge`, and the tool ran. READ tier only, so no money, but this is precisely the "the gateway is one object away from the model, and this is the object that would otherwise do the thing" argument the module docstring makes for the tier check - and the allow-list does not get it. | Have `RegistryToolRunner` carry the node's declared list and pass it as `allowed=`. |
| R3 | should-fix | `packs/acme_billing/graphs/verify_identity.yaml` (`wrong_code` -> `send_code`) | The OTP loop has no attempt cap. `check_code` -> `verified_router` -> `wrong_code` -> `send_code` -> `ask_code` -> `check_code` cycles for ever, one customer message per pass, and the pack's code is a pure function of the address so it does not change between passes. A six-digit code with unlimited guesses is not a verification. `max_nodes_per_turn` bounds one turn, not the sequence. Because `verify_otp` is the tool that sets `ctx.customer.identity_verified`, this is the cheapest route past the refund graph's gate in the sample pack. | Count attempts in the graph's state and route to `not_verified` after three; and say in the module docstring that a real pack must rate-limit. |
| R4 | should-fix | `support_core/tools/base.py:88-105`, `support_core/tools/runtime.py:528-562` | Any WRITE or HIGH tool may write any `CustomerContext` field, including `identity_verified`, and a WRITE tool may be `confirm_exempt`. So a `tool` node can declare the customer verified with no confirmation and no approval anywhere in the picture. The sample pack does exactly this legitimately (`verify_otp`), which is why it is invisible; `send_otp`, and `boom`/`dispatch`/`ping` in the test pack, are all equally entitled. The self-critique names this and explicitly defers the decision to the reviewer. My judgement: narrow it. | Add `patches_context: frozenset[str] = frozenset()` to `Tool` and have `_settle` refuse a key the tool did not declare. Fifteen lines, and it turns "any write tool can verify anyone" into something an author has to write down and a validator can report. |
| R5 | should-fix | `support_core/storage/repositories.py:504-533` | `record_approval`'s `ON CONFLICT (run_id, step_id) DO UPDATE SET args_hash = EXCLUDED.args_hash` updates the hash but not `args`, `tool`, `frame_seq` or `node_id`. The row can therefore end up with an `args_hash` for one action and an `args` column recording another - and `args` exists precisely so the audit reader does not have to reverse a sha256. I confirmed the update does *not* clear `consumed_at`, so there is no bypass here; this is an integrity problem in the audit record, not in the gate. | Either extend the update to `args` and `tool`, or make it `DO NOTHING` on the observation that a re-executed confirm step computes the same proposal anyway. |
| R6 | should-fix | `support_core/tools/runtime.py:291-341` | A refused call leaves no `tool_call` row: `_authorise` raises inside the claim transaction, rolling back the insert so the key is not burnt. The consequence is that "show me every attempted movement of money" is answerable only from `trace_step.error`, and the table built for tool calls has no record of the attempt. The self-critique names this. For DESIGN.md 20's compliance story it is the wrong table. | Write a `refused` row in its own transaction under a key that cannot collide with the real claim (the step id plus a `#refused<n>` suffix), or keep the claim and mark it `refused` rather than rolling it back. |
| R7 | should-fix | `support_core/tools/runtime.py:342-360` | `requires_human_approval` consumes the customer approval with `approved_by IN ('customer','human')` and *then* consumes a second row with `approved_by = 'human'`, ordered by `approved_at`. It fails safe in every ordering I tried, but the first query can eat the human's row when the human approved first, so the outcome depends on row order rather than on the rule. Once phase 6's desk exists this is a bug waiting. | Restrict the first consume to `('customer',)` so the two queries cannot compete for one row. |
| R8 | nit | `packs/acme_billing/graphs/refund.yaml` (`state.charge: Charge`) | Validator warning `graph.state_type_unresolved`: `Charge` is not a type core knows, so `state.charge` is `Any` and `state.charge.amount` is unchecked in both the `confirm` action and the `issue_refund` args. No money risk - the two expressions are identical, `graph.approval_args_mutated` covers the path between them, and the hash is taken over coerced arguments at both ends - but the one type in the pack that carries an amount is the one the validator cannot see. | Declare `charge_amount: float | None` and `charge_description: str | None` in the graph's state, or teach the loader to resolve a model the pack's tools export. |
| R9 | nit | `support_core/engine/executor.py:734` | The per-turn tool budget still counts only model-loop calls, so a graph that walks five `tool` nodes spends none of `max_tool_calls_per_turn`. The self-critique names it and declines to change what an existing manifest key means; I agree with the reasoning, and record it here so it becomes a backlog item rather than a note in a review. | Backlog for phase 9 with the manifest-compatibility decision attached. |

I judged the nine `support pack validate` warnings individually. Four `assignment_optional`
warnings (`str | None` into `str`) are real but harmless: the input model refuses `None` in
`canonical_args`, so the failure mode is a refused call routed to `on_error`, not a call with a
null charge id. Two `manifest.interrupt_graph_unknown` warnings name graphs phase 6 will add.
Two `graph.confirm_exempt` warnings are the exemption report DESIGN.md 8.2 asks for and both
carry a reason that argues the case. One - `graph.state_type_unresolved` - hides something worth
knowing, and is R8 above. None of the nine hides a way to move money.

### Approval bypass attempts

All against `ToolRuntime` and the executor, on real Postgres (`support_test`). Scripts were
written for this review under `reviews/scratch-phase-4/` and deleted afterwards; every scenario
is reproducible from its description.

| # | attempt | result |
|---|---------|--------|
| 1 | Call a HIGH tool with no approval row at all | **held** - `ToolRefused`, no ledger entry |
| 2 | Call a HIGH tool with a valid approval but `requires_approval` omitted by the graph | **held** - refused for naming no confirm node |
| 3 | Forge an `action_approval` row directly in the database with every binding field correct, then call | **executed** - by design: the row *is* the authorisation and the database is the trust root. Recorded, not a finding |
| 4 | Spend one approval on two sequential calls of the same tool and arguments | **held** - second refused, ledger has one entry |
| 5 | Present an approval recorded in frame 1 to a call in frame 2 | **held** |
| 6 | Present an approval from an earlier run of the same conversation | **held** |
| 7 | Present an approval recorded by a different `confirm` node | **held** |
| 8 | Present an approval recorded for a different tool | **held** |
| 9 | Present an approval from a different conversation | **held** |
| 10 | Approve `{label, amount}`, execute `{amount, label}` (key order) | **executed** - correct: `canonical_json` sorts keys, it is the same call |
| 11 | Approve `29`, execute `29.00` | **executed** - correct: both coerce to `29.0` through the input model |
| 12 | Approve `"29"`, execute `29.0` | **executed** - correct, same coercion |
| 13 | Approve `29.0`, execute `29.01` | **held** |
| 14 | Approve `-5.0`, execute `5.0` | **held** |
| 15 | Approve a label as NFC `café`-composed, execute it decomposed | **held** - different bytes, different hash. Safe direction, and no false refusal is possible because both ends call one function |
| 16 | Add a nested key the input model does not declare | **held** - `extra="forbid"` refuses before the hash is taken |
| 17 | Two concurrent callers, one approval, one tool | **held** - one succeeded, one refused, ledger has one entry |
| 18 | Re-record the confirm step under the same `(run_id, step_id)` to clear `consumed_at`, then call again | **held** - the `ON CONFLICT` update touches only `args_hash`; see R5 |
| 19 | Call a HIGH tool from `caller="model_loop"` holding a valid approval | **held** - refused on tier before the claim |
| 20 | Smuggle `charge` through the slot of a `confirm_exempt` tool (`allowed=("ping",)`) | **held** - `'charge' is not a tool this step may call` |
| 21 | `requires_human_approval` tool with one human-approved row | **held** - refused; no desk exists to make the pair. See R7 |
| 22 | Pack-supplied node type returning an `ApprovalProposal` | **held** by `_check_result` (`executor.py:1146`); read in source, and the implementer's mutation test covers it |
| 23 | Non-`tool` node invoking through `rt.tools.invoke` | **held** - `NO_TOOL_ACCESS.invoke` refuses, and the approval check refuses underneath it |
| 24 | Node id carrying `#` or `:` to collide a `tool` node's key with a model-loop key | **not reachable** - `NODE_ID = ^[a-z][a-z0-9_]*$` is enforced by `read_graphs`, which even an unvalidated pack goes through |
| 25 | A tool mutating its own arguments after the hash is taken | **inert** - the tool receives a fresh `input_model.model_validate(canonical)`, and the recorded `args` and the hash were both taken from `canonical` before execution. A hostile pack can still lie about what it did with them, which is the trust boundary, not a bypass |

### Double-payment attempts

| # | attempt | result |
|---|---------|--------|
| 1 | Kill the process before the claim | **safe** - no row, no side effect; the retry runs the call once. Covered by the implementer's cross-process test |
| 2 | Kill after the claim, before the side effect | **safe** - row left `running`; a non-idempotent tool is refused and marked `indeterminate`, an idempotent one repeats. Cannot be told apart from #3 by construction, which is why the refusal is the right answer |
| 3 | Kill after the side effect, before the record (real OS process, `os._exit` inside the tool) | **safe** - `tests/test_tool_crash_recovery.py` passes; the file-backed ledger shows one side effect |
| 4 | Kill during the record's commit | **safe** - the same `running` state as #2; the retry replays or refuses |
| 5 | Two processes racing one step id | **safe** - the unique `idempotency_key` makes one the claimant; the loser re-enters and replays or refuses |
| 6 | Two concurrent callers spending one approval | **safe** - `FOR UPDATE SKIP LOCKED` plus `consumed_at IS NULL` gives exactly one winner (measured, attempt 17 above) |
| 7 | Resume a run whose step id collides with a live one | **safe** - the key carries `run_id` and the monotonic `frame_seq`, neither of which is reused |
| 8 | Two different logical calls under one step id | **safe** - `_reenter` compares the stored `args` against the new canonical form and refuses a mismatch (commit `14c5e32`); I could not construct a pair that shared a key and matched |
| 9 | One logical call whose id changes across a crash | **safe for synchronous tools** - `frame.attempts` lives in the checkpointed frame, so a crash before the checkpoint leaves the attempt unchanged. **Broken for async tools**: the id changes between dispatch and callback because the suspending pass increments the counter. This is R1 |
| 10 | Retry after the executor's error path, with `on_error` returning to the tool node | **safe for approval-bound tools** (the approval was consumed by the first attempt, so the second is refused); **unsafe for a `confirm_exempt` WRITE async tool** - two dispatches, two side effects, measured. This is R1 |

### Commands run

| command | result |
|---------|--------|
| `.venv/Scripts/python.exe -m ruff check .` | `All checks passed!` (exit 0) |
| `.venv/Scripts/python.exe -m ruff format --check .` | `124 files already formatted` (exit 0) |
| `.venv/Scripts/python.exe -m mypy` | `Success: no issues found in 124 source files` |
| `.venv/Scripts/python.exe -m pytest -q -p no:randomly` | `1138 passed, 2 deselected in 280.07s` |
| `.venv/Scripts/python.exe -m pytest -q -m live` | `2 skipped, 1138 deselected in 0.85s` |
| `.venv/Scripts/python.exe -m pytest -q tests/verify_phase_2_resolution.py` | `39 passed in 125.44s` |
| `.venv/Scripts/python.exe -m support_core.cli.main pack validate packs/acme_billing` | `acme-billing: well-formed (9 warning(s))`, exit 0 |
| `.venv/Scripts/python.exe -m alembic downgrade base` | down to base, exit 0 |
| `.venv/Scripts/python.exe -m alembic upgrade head` | `Running upgrade 0005 -> 0006`, exit 0 |
| `.venv/Scripts/python.exe -m alembic check` | `No new upgrade operations detected.` |
| reviewer script: 25 approval-bypass scenarios | 21 held, 1 not reachable, 3 executed and correct (key order, `29` vs `29.00`, `"29"` vs `29.0`); plus the forged database row, which is by design |
| reviewer script: gateway, MCP and registry attacks | all held. MCP undeclared tier -> `high`, `needs_confirm=True`; a duplicate tool name is rejected whether it comes from the pack or from MCP; a forged READ spec for a HIGH tool is refused by the runtime underneath |
| reviewer script: async `tool` node dispatch plus `resume_async_tool` | **callback refused, and the handler ran twice on the `on_error` loop** (R1) |
| reviewer script: refund conversation with the confirm answer forced to `yes`/`no`/`unclear` | `yes` -> one approval, one refund, path `confirm_refund -> issue_refund -> tell_done -> done`; `no` -> zero approvals, zero refunds, path to `abandon`; `unclear` -> re-presented, zero of both |

Every command in the implementer's own table reproduced. `alembic downgrade base` then
`upgrade head` then `check` is clean, and migration 0006 matches DESIGN.md 17's `tool_call` and
`action_approval` rows plus the additions the phase argues for; every added column is nullable or
defaulted, so it is genuinely additive.

### Design conformance

- **8.1 Tool contract**: every field of the design's class is present with the design's default,
  plus `confirm_exempt`/`confirm_exempt_reason` (8.2 asks for the flag; 8.1's class omits it) and
  `ToolContext.patch_customer`. `ToolContext` carries the idempotency key, the customer identity,
  the step id standing in for the trace span, and the approval - all four the design names.
- **8.2 Risk policy**: `support_core/tools/risk.py` transcribes the table once and both the
  validator and the runtime read it. The hash is `sha256(tool_name + canonical_json(args))`
  exactly, over coerced arguments - a deviation the phase argues for and I agree with. The
  binding being stronger than the design's can only cause refusals, never permissions. The
  `requires_human_approval` cell has never run green (R7).
- **8.3 Registry and MCP**: duplicate names rejected, JSON-schema check present, an undeclared
  MCP tier defaults to HIGH (measured), tool outputs treated as untrusted data (see below).
- **8.4 Model-facing loop**: bounded, READ-only, and validated against the node's list - with the
  allow-list checked in one layer where the tier is checked in two (R2).
- **6.4 graphs**: `refund.yaml`'s `confirm_refund` and `issue_refund` are byte-identical to the
  design's; the additions (`on_error` edges, `say` nodes standing in for `handoff` until phase 6,
  a router `default`) are declared in the phase notes.
- **17 data model**: covered above.
- **The `requires_human_approval` seam**: the shape is right - the runtime asks for a second
  approval row with `approved_by = 'human'`, and phase 6's desk is the only thing that can write
  one, so a pack that sets the flag fails closed today. The ordering is wrong (R7) and should be
  fixed now rather than discovered in phase 6.

### Regression check

- **Phase 3's prompt boundary.** Tool results reach the model through `data_block(..., nonce=
  prompt.nonce)` in `support_core/llm/service.py:334-340`, the same per-render delimiter path as
  retrieved documents, and the fence label is the *resolved* tool's name rather than the model's
  string (finding V10 still holds). I fed a tool result containing the live nonce and a
  well-formed `-----END UNTRUSTED DATA <nonce>-----` line: `neutralise` prefixed it so it is no
  longer a line-initial delimiter. `tests/test_prompt_injection_matrix.py` already carries
  `tool_result_content` and `tool_result_name` as injection points and both pass.
- **Phase 2's durability.** Nothing the turn depends on is memory-only in core: the approval, the
  `tool_call` row, the `ctx.customer` patch and the frame's `attempts` all go through the
  checkpoint transaction. `frame.attempts` being durable is what makes the idempotency key stable
  across a crash - and, in the async case, what makes it *unstable* across a suspension (R1). The
  one memory-only thing is the sample pack's `BILLING`/`OTP` singletons, which is a property of a
  fake backend and is written down.
- `tests/verify_phase_2_resolution.py` passes (39), so nothing in phases 0-3 regressed.

### Exit criterion

Both halves hold independently of the implementer's tests. `tests/test_adversarial_approvals.py`
passes inside the full green suite, and my own twenty-five scenarios above are a superset of what
it covers. The refund graph runs through `confirm_refund` and `issue_refund` against the fake
provider with one approval, one consumed marker and one `issue_refund:ch_1002:29.0` ledger entry.
And the conversation is driven by node logic, not by the cassette: with the cassette supplying
every other model call and the confirm classifier replaced by a hook answering `no`, the graph
takes the `no` edge to `abandon`, writes no approval and moves no money; `unclear` re-presents
the proposal indefinitely and also moves nothing. The cassette supplies the model's words; the
graph decides what they buy.

### On the headline caveat: is "a pack's Python is trusted completely" acceptable?

**Yes as a trust boundary, no as it is currently drawn.** DESIGN.md 4.1 is explicit - one domain,
one repository, one image, one service - and 8.3 gives the exported `Tool` as the only source of
a risk tier. A pack is first-party code shipping in the same image as core; sandboxing it would
mean a process boundary and an RPC contract, which is a different design rather than a fix to
this one. The controls this phase builds are aimed at the model, the customer and the graph, and
those are the right targets: the model cannot act, the customer's yes is bound to arguments, the
graph cannot route around the runtime. That is the correct answer to the threat model DESIGN.md
declares.

Two narrowings are worth making anyway, because they cost little and they turn a capability every
pack has by default into one an author must ask for:

1. **`ctx.customer` writes should be declared per tool** (R4). "Any WRITE tool may declare the
   customer verified" is a far wider grant than "the tool that checks the passcode may set
   `identity_verified`", and the difference is a `frozenset` on the `Tool` model and four lines in
   `_settle`. It also gives the validator something to report, which is what makes review
   possible.
2. **`confirm_exempt` deserves a machine-checkable narrowing, not only a sentence.** The required
   reason is a real improvement over an INFO line, but the flag still means "this WRITE tool needs
   no approval, trust me", and it is the flag that makes R1's double dispatch and R3's OTP loop
   reachable. At minimum the validator should report the *arguments* a `confirm_exempt` tool's
   nodes pass it, so a reviewer sees `send_otp(email: state.email)` - a model-written address -
   differently from `send_otp(email: ctx.customer.email)`. The self-critique raises exactly this
   as its attack 7 and it is the sharpest thing in that list.

What should be written on the front of the box, and today is written only in module docstrings,
is that a pack author is inside the trust boundary and a pack review is therefore a security
review. That belongs in the README's pack section and in DESIGN.md 4.1.

### Missed by self-critique

- **R1, the async tool.** The self-critique says async tools are "thin" and lists the transport,
  the poller and the callback authentication as untested. It does not notice that the feature has
  never run end to end through the executor at all, that the callback path is unreachable, and
  that the resulting `on_error` re-entry repeats a WRITE tool's side effect. The gap is exactly
  the one its own "which tests are weak" section describes - "the async tool tests use a tool that
  dispatches synchronously" - one inference short of the bug.
- **R2, the allow-list has one layer where the tier has two.** The phase's central argument is
  defence in depth and the runtime's module docstring makes it explicitly for the tier. The
  allow-list is the other half of DESIGN.md 8.4's sentence and it lives only in the gateway.
- **R5, `record_approval`'s partial `ON CONFLICT` update.** Not a bypass, but the phase went to
  the trouble of storing `args` for the human reader and then left a path on which `args` and
  `args_hash` can disagree.
- **R3, the unbounded OTP loop.** The self-critique's attack list covers the pack thoroughly -
  two identical charges, `send_otp` to a model-written address, two conversations for one customer
  - and does not include "the passcode can be guessed as many times as the customer likes". Given
  that `verify_otp` is what opens the refund gate, that is the pack's shortest path.
- The self-critique is otherwise good, and unusually honest about what its mutation table does and
  does not prove. Its attacks 2 (the approval covers the arguments, not the sentence) and 4 (a
  gate redirect between confirmation and call is not statically analysed) are both real and both
  correctly scoped to later phases; I have nothing to add to either.

## Resolution

Resolver: a fresh agent that wrote none of the phase-4 code, 2026-09-06 (PLAN.md step 5). The
must-fix is fixed, with a regression test confirmed to fail against the code as reviewed. Every
should-fix is fixed. Both nits are deferred with a reason and a home. The review itself was
committed first as `c27345f`, as in phases 0 to 3.

### R1: the fix, not the refusal

The instructions offered two acceptable answers - repair the identity mismatch, or make an async
tool refuse to load at all and move the feature to a later phase. **The repair**, for three
reasons.

The bug is not in the feature; it is one string computed in the wrong place. The dispatching pass
knows the key it claimed - it is on the `ToolCallResult` it already has - and the suspension
detail is already the channel by which a suspending node tells the resuming pass what it knew:
the `confirm` node has been carrying its `args_hash` there since the first commit of this phase,
for the same reason and against the same hazard (the state has moved on; what the customer
answered is not what would now happen). The async node needed to carry one more field down a road
that was already built and already tested. Refusing at load would have been the honest answer to
a hole in the design; this is a hole in five lines of wiring.

Second, refusing at load costs more than it saves. `waiting_async_tool` is one of DESIGN.md 7.2's
four suspension statuses, migration `0006` and the `run` row already carry it, phase 2's
suspend-and-resume matrix already exercises it, and `Executor.resume_async_tool` is the endpoint
phase 7 is going to authenticate. A load-time refusal would leave all of that in place with no
way to reach it, and the phase that eventually implemented the feature would be re-deriving what
is now three lines of `ToolRunner`.

Third - and this is the part that decided it - the reviewer's instruction was not to lean on the
approval as the defence for this path, and the fix does not. The approval is spent at dispatch
whatever happens; what stops the second side effect now is that the callback lands on the call it
belongs to, so there is no failure, so there is no `on_error` re-entry. The residual - an
`on_error` edge that returns to its own `tool` node, which repeats a `confirm_exempt` WRITE tool
once per failure whether it is async or not - is a *different* shape that R1 happened to expose,
and it is written down as a deferred finding against phase 6 rather than smoothed over here.
Nothing in the async path now depends on an approval being spent.

**Confirmed failing first.** `tests/test_async_tool_flow.py` was written before the fix and run
against the reviewed code, where both of its tests fail:
`test_a_callback_does_not_make_the_tool_node_dispatch_a_second_time` reproduces the review's exact
measurement - `LEDGER.executed` holds **two** `dispatch_async` entries for one customer message
and one callback, and the run is back in `waiting_async_tool` with two `awaiting_callback` rows.
After the fix: one handler invocation, one row, `succeeded`, the run `done`.

The key is checked rather than trusted. It arrives from durable state the executor wrote, but
`complete_async` still requires the row it names to belong to this run, this node and this tool,
so carrying a key on a suspension is not a way to complete somebody else's call
(`test_a_callback_cannot_complete_a_call_another_node_dispatched`).

### R2 and R3

**R2, the allow-list.** `RegistryToolRunner` now carries the list the executor read from the
validated graph (`_model_tools(node)`: an `llm` node's `tools:`, and the empty tuple for every
other node type) and passes it as `allowed=`. Two checks in two layers from two reads of the same
declaration, which is the standard the tier already met: removing the gateway's `_refuse_reason`
leaves the runtime refusing, and removing the runtime's leaves the gateway refusing. The
reviewer's own probe - call the runner directly for a tool the node never declared - is
`test_the_model_loop_runner_refuses_a_tool_its_node_did_not_declare`, and the constructor
argument is required, so a runner that does not know what its node declared cannot be built at
all.

**R3, the passcode.** Three wrong guesses per conversation, counted in `OtpStore`, surviving a
re-send, reported as `locked` on `verify_otp`'s output, and routed by `verified_router` to a new
`too_many_attempts` node and on to the `not_verified` end the graph already had. The count lives
in the tool rather than in the graph because the expression language has no arithmetic, so a
counter a graph can increment does not exist; the *decision* is still the graph's, which is where
a pack author will look for it. The re-send half is the half that matters and has its own test:
a limit the customer resets by asking for another code is not a limit. The module docstring now
says what a real pack has to do instead - per account, per address, over a window, with a lockout
that outlives the conversation - because three guesses per conversation is still three guesses
per *conversation*.

### The rest

| id | severity | action | commit |
|----|----------|--------|--------|
| R1 | must-fix | **Fixed.** The dispatch's idempotency key travels on the suspension detail and `complete_async` takes it explicitly, checked against the row's run, node and tool. `tests/test_async_tool_flow.py` drives dispatch and callback through the executor on a graph whose `on_error` returns to the tool node. **Confirmed failing first**: two side effects from one intent and one callback, exactly as measured. | `7174d6a` |
| R2 | should-fix | **Fixed.** `RegistryToolRunner(runtime, site, allowed=...)`, built from the node's own `tools:` by the executor and passed to `ToolRuntime.invoke`. Either layer alone still refuses. | `f2011ed` |
| R3 | should-fix | **Fixed.** `MAX_OTP_ATTEMPTS = 3`, counted per conversation, not reset by a re-send; `verified_router` routes `locked` to `too_many_attempts` and `not_verified`. The `acme_refund` cassette is re-recorded because the ask node's state block gained a field; the four others are byte-identical. | `dbdbd5a` |
| R4 | should-fix | **Fixed, as the reviewer judged it.** `Tool.patches_context: frozenset[str]`, empty by default; `_settle` refuses a patch outside it; a read-tier tool may not declare one, and a field `CustomerContext` does not have is a load error rather than a patch that silently never lands. `verify_otp` declares `identity_verified`; nothing else in either pack declares anything. Recorded in the Decisions log because it adds a field DESIGN.md 8.1's class does not have. | `f634885` |
| R5 | should-fix | **Fixed, as `DO NOTHING`** rather than by widening the update - spelled as a no-op `DO UPDATE` so `RETURNING` still yields the row. A confirm step re-executed under the same `(run_id, step_id)` computes the same proposal from the same checkpointed state, so there is nothing to update; and if it somehow did not, the row written when the customer was actually asked is the honest one. | `db491da` |
| R6 | should-fix | **Fixed.** A refusal of a side-effecting tool writes a `refused` row under `<step_id>#refused:<random>`, in its own transaction, with the arguments, the risk and the message. The claim is still rolled back, so the real key stays unclaimed and the properly approved call can still be made under it - asserted in the same test. A refused READ call writes nothing: the gateway already counts it, and this table is for side effects. | `db491da` |
| R7 | should-fix | **Fixed.** The first consume is `('customer',)`, so the two queries cannot compete for one row. The new test approves as a human *first* and then as the customer, which is the ordering that failed before. | `db491da` |
| R8 | nit | **Deferred to phase 9**, with a checklist line there and an entry in "Deferred findings". Both available fixes are bigger than a resolution pass should carry: flattening `state.charge` rewrites the arguments of the one HIGH-risk call in the pack (and its confirm, and the cassette, and an adversarial test's internals), and letting a graph name a pack-exported model is loader work that needs the registry in both the loader and the validator - the same plumbing phase 9's registry scoping needs. No money risk, for the reasons the reviewer gives. | - |
| R9 | nit | **Deferred to phase 9**, on the reviewer's own recommendation, with the compatibility question attached: widening `max_tool_calls_per_turn` to count `tool` nodes changes what an existing manifest key means. | - |

Two things were found while doing the above, and are recorded rather than smoothed over.

**Two callers racing one step id lost with an `IntegrityError`.** Writing the double-payment
matrix as a test turned up the loser of a claim race raising a raw SQLAlchemy exception rather
than a `ToolError` - a dead turn instead of a routed failure. The money was safe (one side
effect, measured), which is why the review recorded row 5 as safe. The loser now refuses, and
deliberately does not re-enter: a `running` row left by a *dead* process may be repeated when the
tool declares itself idempotent, but a `running` row held by a caller who is still executing it
is a different thing wearing the same clothes, and losing the insert is the one moment the two
are distinguishable. `test_05_two_callers_racing_one_step_id` fails both ways round - against the
reviewed code (an unrouted exception) and against a version that re-enters (two side effects from
an idempotent tool).

**The residual `on_error` shape.** R1's second half - a graph whose `on_error` returns to its own
tool node - is not specific to async tools, and closing R1 does not close it. Deferred to phase 6
with a checklist line; the note says what it costs (a `confirm_exempt` WRITE tool repeats once
per failure) and what it does not (a tool holding an approval is refused on the second attempt,
because the approval is spent).

The review's closing recommendation about `confirm_exempt` is also written down rather than done:
reporting the *arguments* a `confirm_exempt` tool's nodes pass it needs the cross-graph
control-flow model phase 6 has to build anyway, because a gate redirect can reach such a node
from another graph. The other closing recommendation - that a pack author is inside the trust
boundary, so a pack review is a security review - is now a paragraph in the README's pack
section, where the reviewer asked for it.

### The two attack matrices, before and after

Both are now test files that a plain `pytest` run collects, in the review's own order, with its
verdicts as the assertions - including the three that correctly execute, because turning those
into refusals would be a regression in the other direction. The before-column was measured by
checking the reviewed source back out (`git checkout e4a9e08 -- support_core packs`) and running
the new files against it.

| matrix | reviewed code | after the resolution |
|--------|---------------|----------------------|
| Approval bypass, 25 scenarios (`tests/test_approval_bypass_matrix.py` carries 24; scenario 22 needs a whole executor and stays in `tests/test_adversarial_approvals.py`) | **24 of 24 as the review recorded them** - 21 held, 3 executed and correct | **24 of 24**, unchanged |
| Double payment, 10 rows (`tests/test_double_payment_matrix.py` carries 9 in-process; rows 3 and 5's real `os._exit` cases stay in `tests/test_tool_crash_recovery.py`) | **7 of 9** - row 5 lost with an unrouted `IntegrityError`, rows 9 and 10 produced two side effects from one intent and one callback | **9 of 9** |

Nothing that held before fails now. The approval binding was as strong as the review said: all
twenty-four held against the reviewed code too, which is exactly why they are worth keeping -
they are a regression harness now rather than a discovery exercise.

### Which enforcements are load-bearing

The implementer's exercise, repeated over every check this phase has - the four it gained in this
resolution included. Each enforcement was switched off in the source, the suite that is supposed
to catch it was run, and the source was restored. The suite is
`test_adversarial_approvals.py`, `test_tool_runtime.py`, `test_approval_bypass_matrix.py`,
`test_double_payment_matrix.py`, `test_async_tool_flow.py`, `test_refund_flow.py`,
`test_tool_loop.py` and `test_identity_attempts.py` - 114 tests.

**All fourteen are load-bearing.** Nothing was switched off without a test noticing.

| enforcement removed | tests that fail | the ones that matter |
|---|---|---|
| the approval check entirely (`_authorise` returns `None`) | 32 | hash mismatch; no confirm named; one approval one call; the whole bypass matrix |
| the runtime's model-loop tier check | 2 | a WRITE tool from a model loop holding a valid approval - still the only test of the second lock |
| **the runtime's allow-list check (R2)** | 4 | the runner called directly for a tool its node never declared, and three matrix rows |
| at-most-once for a non-idempotent call | 2 | the in-process one and the cross-process one that counts side effects in a file |
| **the claim-race refusal** | 1 | two callers racing one step id run an idempotent tool twice |
| **`patches_context` (R4)** | 1 | a WRITE tool patching a customer field it did not declare |
| the confirm's shown-versus-about-to-approve comparison | 1 | the amount changed under the customer's answer |
| **the dispatch key on the suspension (R1)** | 2 | both async round-trip tests; the second one measures the two side effects again |
| single use (`consumed_at IS NULL`) | 4 | one approval cannot authorise a second call |
| the run/frame/node binding | 40 | an approval from another frame, another confirm node, another run or another tool |
| **the customer-only first consume (R7)** | 1 | a human who approved before the customer |
| only a `confirm` node may record an approval | 1 | a pack-registered node type forging one |
| only a `tool` node may change `ctx.customer` | 1 | a node type declaring the customer verified |
| only a `tool` node may invoke | 20 | and the approval check still refuses underneath, which is the depth working |

What this still does not prove is that the *set* of enforcements is complete - only that each one
is checked. The four new rows are the ones worth noting: R2, R4 and R7 were all cases where the
code did the right thing in some orderings and the wrong thing in others, and none of them had a
test until now.

### Everything re-run at the end

From the repository root with `.venv/Scripts/python.exe`; Postgres 16 in
`customer-support-agent-db-1`, database `support_test`, `ANTHROPIC_API_KEY` unset.

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) |
| `python -m ruff format --check .` | `129 files already formatted` (exit 0) |
| `python -m mypy` (strict) | `Success: no issues found in 129 source files` |
| `python -m pytest -q` | `1186 passed, 2 deselected in 341.71s` |
| `python -m pytest -q -m live` | `2 skipped, 1186 deselected` - skips on the missing key rather than failing |
| `python -m pytest -q tests/verify_phase_2_resolution.py` | `39 passed in 147.17s` - phase 2's proof harness still holds |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (9 warning(s))`, exit 0 - the same nine, since R8 is deferred and R3 added no new one |
| `python -m alembic downgrade base`, `upgrade head`, `alembic check` | all six revisions down and up cleanly; `No new upgrade operations detected.` |
| `python -m tests.cassettes.build_cassettes` | five cassettes; the committed files are what the builder produces |
| the mutation exercise above (14 enforcements) | 14 caught, 0 missed |

The test count is the reviewed 1138 plus 48: the async round trip through the executor,
the passcode limit, the two attack matrices, and one regression test each for R2, R4, R5, R6, R7
and the claim race.

### Status

Phase 4 is **done** in BACKLOG.md. The must-fix is fixed rather than refused, with a regression
test confirmed to fail against the reviewed code; every should-fix is fixed; both nits are
deferred with a home and a checklist line; and every command above is green.
