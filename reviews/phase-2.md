# Phase 2 review: Execution engine and durability

Design references: DESIGN.md sections 6.1, 6.3, 6.6, 7.1 to 7.3, 17 (principles from 3, 8.2).
Backlog: BACKLOG.md "Phase 2" plus the deferred findings assigned to phase 2 (phase-0 F5,
phase-1 P1 and P2). Inherits the settled decisions in reviews/phase-0.md and reviews/phase-1.md.

## Plan

Written before any code, per PLAN.md step 1.

### Task breakdown

1. **Engine types** (`support_core/engine/types.py`). DESIGN 6.3 verbatim where it gives a
   shape: `NodeResult` with `state_patch`, `next_edge`, `outbound`, `suspend`, `push_graph`;
   a `Node` protocol with `id`, `type`, `run`, `resume`; `NodeRuntime` as the object nodes
   use to reach everything outside themselves. Plus the types 6.3 names but does not spell:
   `OutboundMessage`, `SuspendReason`, `ResumeEvent`, `GraphInvocation`, and `Frame` /
   `FrameStack` (DESIGN 6.1: "Frames live on a stack").

   `Frame` carries `frame_seq` (unique and monotonic per run - the middle field of the step
   id), `graph_id`, `node_id`, `state` (JSON), `kind`, `return_node`, `outputs_into`,
   `attempts` and `passed_gates`. **`kind` is `root | subgraph | gate_redirect | interrupt`
   from the first commit** and `push_frame` takes a kind: DESIGN 6.6's interrupt stack pushes
   frames that are not sub-graph calls, and phase 6 must not have to change the frame shape or
   the migration to get one.

2. **Node runners** (`support_core/engine/runners.py`). One class per node type implementing
   the 6.3 protocol, built by a factory registry keyed on node type. The runner is separate
   from the *config* model in `graph/nodes.py` (the phase-1 reviewer's forward-compat question
   (a)): config models describe YAML, runners describe behaviour, and `NodeTypeSpec` keeps
   being the single declaration of a node type. Executable in phase 2: `router`, `say`, `end`,
   `subgraph`, `gate`, `ask`. Not executable: `llm`, `tool`, `confirm`, `handoff`, which raise
   `NodeNotExecutableError` naming the phase (PLAN.md's standing rule holds trivially - no
   phase-2 code path can invoke a tool).

3. **Checkpointing** (`support_core/engine/checkpoint.py`). One transaction per node holding
   the `run` update (frames, status, `checkpoint_seq`, suspension fields, per-turn counter)
   *and* the `trace_step` insert *and* the outbound `message` rows. Nothing that determines
   the outcome lives anywhere else, so "resume from the last committed checkpoint" is the
   whole recovery story. Step id is exactly DESIGN 7.1: `run_id:frame_seq:node_id:attempt`,
   where `attempt` is the number of *completed* executions of that node in that frame, held in
   the frame and therefore advanced atomically with everything else.

4. **Migration 0002** (phase-0 deferred finding F5 plus what the engine needs).
   `trace_step.seq` (the run's `checkpoint_seq` at write time) with `uq_trace_step_run_seq`,
   backfilled for existing rows by `row_number()` so it applies to a non-empty table;
   `trace_step.error`; `run.suspended_at`, `run.timeout_at`, `run.awaiting`,
   `run.turn_nodes`, `run.pack_fingerprint`. `started_at` and `ended_at` are written from an
   injectable clock, never left to the `now()` default, because two steps in one transaction
   share a transaction timestamp. Downgrade drops exactly what upgrade added.

5. **Single writer** (`support_core/engine/locks.py`). `pg_advisory_xact_lock` (DESIGN 17) on
   a dedicated connection whose transaction spans the turn, so the many checkpoint
   transactions can commit inside it and a dead process releases the lock by dying. Inbound
   always writes its `message` row with `status = pending` *first*, then tries the lock; the
   holder drains pending messages oldest-first inside the lock. A caller that cannot get the
   lock waits for it (bounded by `lock_timeout`) and becomes the next drainer, which is what
   makes "processed in order when the lock frees" true without a lost-wakeup window.

6. **Turn loop** (`support_core/engine/executor.py`), DESIGN 7.1 in order, minus the parts
   phases 3 to 7 own. Those become named hooks on one `EngineHooks` object with defaults that
   do nothing surprising: `interrupt_check` (defaults to `continue`), `slot_extractor`
   (deterministic: the reply text fills the first declared slot), `handoff` (records nothing),
   `channel` (does not send), `probe` (nothing; tests use it to crash the process at named
   points). Guardrails are *not* stubbed: phase 7 owns them and an empty hook would only
   pretend they exist.

7. **Frame stack semantics**: push with input mapping, pop with output mapping, and
   **gate re-evaluation on every frame entry** (DESIGN 6.6). A frame records the gates it has
   passed; entering a frame (resume from suspension, return from a pushed frame, interrupt
   return) re-evaluates them before running the current node, and the first one that is now
   false pushes its redirect again. That is the property "gates cannot be skipped" and it gets
   a test that suspends after a gate, invalidates the gate's precondition, resumes, and shows
   the redirect runs again before the protected node.

8. **Suspension and resumption** (DESIGN 7.2), all four statuses, with per-status and
   per-channel timeouts added to `pack.yaml` as a `timeouts:` block (the design requires
   configurable per-status timeouts and 5.1 has nowhere to put them). Entry points
   `on_inbound`, `resume_human`, `resume_async_tool`, `resume_timer`, and `sweep_timeouts`.
   Resume loads everything from Postgres, so a resume days later in another process is the
   same code path as a resume a second later in the same one.

9. **Failure handling** (DESIGN 7.3): `max_nodes_per_turn` from the manifest, counted in a
   column so the count survives a crash; a node error routes to the node's `on_error` edge
   when it declares one and otherwise to the handoff hook with a reason; the run then suspends
   `waiting_human`. The frame-level `on_error` *graph* of 7.3 has no place in the graph schema
   phase 1 delivered, so it is recorded as a divergence rather than invented here.

10. **Deferred findings.** P1: `load_pack` reads every graph file twice, so the pin can hash
    bytes it did not parse - the validator now snapshots the text it read onto the report and
    the loader parses that snapshot. P2: `templates.ENVIRONMENT` becomes a per-pack
    environment (`Pack.environment`) with validation using its own throwaway environment, so
    no Jinja environment is shared between two packs or two pack versions.

11. **Retire `tests/stepper.py`.** The instruction is explicit that there must not be a second
    long-lived execution path. The stepper is deleted and its test file rewritten to drive the
    real executor over the same `deterministic_pack`, asserting the same path, messages and
    outputs, so the phase-1 exit criterion is preserved under the engine that replaces it.

12. **Tests.** Real Postgres throughout (PLAN.md: no mocking the database).
    - Kill-and-resume at four points: before the node runs, after it ran but before the
      checkpoint transaction, inside the transaction before commit, and after the commit but
      before the loop advances. Each one runs in a **separate OS process that exits with
      `os._exit`** for at least one point and in-process with a disposed engine for the
      matrix, and each asserts the resumed run reaches byte-identical frames, trace steps and
      outbound messages as an uninterrupted run.
    - Concurrency with two OS processes against real Postgres: one holds the lock in a slow
      turn while the other arrives; both messages are processed once, in arrival order, and no
      state is lost.
    - Step id determinism, `(run_id, seq)` ordering, a double checkpoint of the same step id.
    - Gate re-entry, frame push/pop/output mapping, an interrupt-kind frame push.
    - Suspension and resumption for each of the four statuses, and timeout sweeping.

### Intended deviations from DESIGN.md and why

- **`gate` and `ask` become executable in phase 2**, though `graph/nodes.py` recorded them as
  phase 4 and phase 3. Gates are the subject of a phase-2 checklist line ("gate re-evaluation
  on frame entry") and DESIGN 6.6's "gates cannot be skipped" cannot be *tested* without
  running one; a gate only evaluates an expression and pushes a graph, so nothing about it
  needs phase 4. `ask` is the only node that suspends into `waiting_customer`, which is the
  first row of DESIGN 7.2, and phase 2 owns 7.2; its slot extraction - the part that genuinely
  needs an LLM - is the injectable `slot_extractor` hook with a deterministic default, and
  phase 3 replaces the default rather than the node. Both `executable_phase` values are
  corrected to 2 so the registry keeps telling the truth.
- **A public `register_node_type`.** DESIGN 6.2 ends with "Custom node types are Python
  classes registered by name in the pack"; `NODE_TYPES` is a closed dict (phase-1 self-critique
  item 1). Phase 2 needs the mechanism for an honest reason: `waiting_async_tool` and
  `waiting_timer` have no core node type at all (they belong to phase 4's async tools and to a
  scheduler), so without registration the only way to test two of the four statuses in
  DESIGN 7.2 is to hand-write database rows and pretend. The registration API is small, it is
  the same seam phase 4 and 6 need, and the test packs that use it live under `tests/`.
- **`pack.yaml` gains `timeouts:`.** DESIGN 7.2 says "Timeouts are per status and configurable
  per pack" and section 5.1's example has no such block. Added with defaults, plus per-channel
  overrides because 7.2's own example contrasts web chat with email.
- **`run` and `trace_step` gain columns** beyond DESIGN 17's list, as phase 0 did for other
  tables: `seq` and `error` on `trace_step`; `suspended_at`, `timeout_at`, `awaiting`,
  `turn_nodes`, `pack_fingerprint` on `run`. Every one is required by 7.1 to 7.3 behaviour and
  all are additive.
- **Outbound messages are written inside the checkpoint transaction** with
  `status = pending_send`, and sending happens after the commit. DESIGN 7.1 lists sending
  before "advance"; writing the row first is what makes a crash between the two survivable.
- **The advisory lock is `pg_advisory_xact_lock` on a dedicated connection**, not on the
  connection doing the work, because the turn deliberately spans one transaction per node.
  DESIGN 17 names the function; it does not say which connection holds it.
- **`interrupt_check` handles `continue` only.** `new_intent`, `cancel` and `unclear` are
  DESIGN 6.6 and belong to phase 6; the hook returns them and the executor raises rather than
  half-implementing the return-to-interrupted-workflow prompt. The frame stack is already
  shaped for them.
- **No guardrails, no LLM, no tool execution, no channel adapter.** Phases 3, 4 and 7.
