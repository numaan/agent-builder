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
| `python -m pytest -q` | `1137 passed, 2 deselected in 308.95s` |
| `python -m pytest -q -m live` | `2 skipped, 1137 deselected` |
| `python -m pytest -q tests/verify_phase_2_resolution.py` | `39 passed` |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (9 warning(s))`, exit 0 |
| `python -m alembic downgrade base` | down to base, no errors |
| `python -m alembic upgrade head` | `Running upgrade 0005 -> 0006` |
| `python -m alembic check` | `No new upgrade operations detected.` |
| `python -m tests.cassettes.build_cassettes` | five cassettes, re-recorded byte-identically |
