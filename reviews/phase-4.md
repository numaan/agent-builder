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
