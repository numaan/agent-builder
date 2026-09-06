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
