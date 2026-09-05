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

## Implementation notes

Environment: Windows 11, Python 3.13.14, SQLAlchemy 2.0.52, asyncpg 0.31.0, Alembic 1.19.2,
pydantic 2.13.5, Postgres 16.15 from `pgvector/pgvector:pg16` (container
`customer-support-agent-db-1`), database `support_test`. `ANTHROPIC_API_KEY` is unset and
nothing in this phase would use it.

### Shape of the code

```
support_core/engine/
  types.py        Frame, NodeResult, SuspendReason, ResumeEvent, GraphInvocation, step_id (206)
  runners.py      the DESIGN.md 6.3 node protocol, one runner per node type, the registry (396)
  hooks.py        the seams later phases fill, with defaults that do nothing surprising (143)
  locks.py        pg_advisory_xact_lock on a connection of its own (81)
  errors.py       what is routed to on_error and what is not (36)
  executor.py     the turn loop, the frame stack, the checkpoint composition (1032)
support_core/storage/
  repositories.py every statement the engine runs, including write_checkpoint (327)
  migrations/versions/0002_engine_durability.py                                (98)
tests/
  engine_support.py    hooks that record and crash on cue, readers for what was written
  engine_child.py      a separate OS process that drives the engine and can os._exit
  packs/engine_pack    ask, gate, sub-graph: the pack the suspension tests use
  packs/custom_pack    the registered `wait` and `boom` node types; gates and failure routing
  packs/queue_pack     echoes the message that drove each turn, for the ordering assertions
```

The executor is about a thousand lines against DESIGN.md 7.4's estimate of "roughly 2,000 lines
of well-tested Python" for the whole engine, which is the right order of magnitude with phases 3
to 6 still to add.

### Where the plan changed while building

- **There is no `engine/checkpoint.py`.** The plan gave checkpointing its own module; it turned
  out to be one function over three tables, so it lives with the rest of the engine's SQL in
  `storage/repositories.py` (`write_checkpoint`), which DESIGN.md 18 lists as `storage/` -
  "repositories". The executor composes the call; the module boundary is "who writes SQL".
- **The hooks are named `extract_slots` and `send`**, not `slot_extractor` and `channel`.
- **Two things the plan did not foresee** and the tests forced: the resume event has to be
  durable (`run.awaiting.turn_event`) and delivered only to the node the run suspended at, and
  `Executor.recover_stalled` has to exist because a crashed turn on a conversation nobody writes
  to again has no deadline for the timeout sweep to find. Both are described below.

### Commands run at the end of the phase

From the repository root with `.venv/Scripts/python.exe`.

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) |
| `python -m ruff format --check .` | `77 files already formatted` (exit 0) |
| `python -m mypy` (strict) | `Success: no issues found in 77 source files` |
| `python -m pytest -q` | `463 passed in 120.55s` |
| `support pack validate packs/acme_billing` | `acme-billing: empty but well-formed`, exit 0 |
| `python -m alembic downgrade base` then `upgrade head` | both revisions apply from base and roll back cleanly |
| `python -m alembic check` | `No new upgrade operations detected.` |

The 463 include 81 new tests: 36 durability (28 of them the crash matrix), 15 suspension,
7 frame stack and gates, 7 graph execution, 7 registry and manifest, 5 pack coherence,
4 concurrency. The phase-1 count of 389 was 389 with the stepper's 6 tests included; those 6
became 7 against the real engine.

### Things worth knowing that came up while building

- **The turn spans many transactions, so the lock cannot live on the working connection.**
  DESIGN.md 7.1 wants a checkpoint after every node and DESIGN.md 17 wants
  `pg_advisory_xact_lock` around the turn; those two are only compatible if the lock has a
  connection of its own whose transaction stays open for the turn. That is also the crash-safe
  choice: nobody has to run an unlock, because a dead connection aborts the transaction and
  Postgres drops the lock. `test_a_dead_process_releases_the_lock` kills a child with `os._exit`
  inside the checkpoint transaction and then takes the lock.
- **A message can be claimed and never delivered.** The gate re-check fires on frame entry,
  before the node that was waiting for the reply runs; if the redirect suspends, the turn ends
  with the customer's message marked `received` and nothing to show for it. Two fixes were
  needed: the resume event is delivered only to the node the run actually suspended at (the
  first version handed it to whatever ran next, which called `resume` on a `say` node), and an
  undelivered event puts its message back on the queue - in the same transaction that clears it
  from `run.awaiting`, so a crash cannot leave the message both queued and about to be replayed.
- **The event that drives a turn is durable state.** It lived in a local variable, so a process
  that died between claiming a message and delivering it lost the message: the row said
  `received` and the frame stack could not say what the node had been waiting for.
  `run.awaiting.turn_event` now holds it until a node consumes it.
- **An injectable clock must not order anything two processes both write.** Outbound messages
  were stamped from the engine clock; a test clock in one process and a real one in another
  reordered a conversation's transcript across a crash and a resume. Message timestamps come
  from the server now. The *trace* still uses the engine clock, which is why `trace_step.seq`
  exists.
- **`frame_seq` has to be monotonic per run, not a stack depth.** A graph invoked twice at the
  same depth would otherwise produce the same step ids twice, and phase 4 would deduplicate two
  genuinely different tool calls into one. `run.next_frame_seq` is a column for that reason.
- **`extra="forbid"` on the built state models does real work.** It is what turns "the pack was
  redeployed with a renamed state field" into a `pack_incompatible` handoff instead of a frame
  quietly losing a value.
- **`_advance` is the only place the stack changes**, and it returns whether it changed, which
  is what drives the gate re-check. Getting that wrong in either direction is either a skipped
  gate or an infinite re-check.


## Self-critique

Written after re-reading DESIGN.md 3, 6.1, 6.3, 6.6, 7.1 to 7.3 and 17, and PLAN.md.

### What did I skip or simplify?

- **Guardrails are absent, not stubbed.** DESIGN.md 7.1 has `guardrails.inbound(message)` and
  `guardrails.outbound(result.outbound)` in the loop. Phase 7 owns them, and an empty hook would
  be a claim that the seam is right when nothing has ever passed through it. The two places they
  go are named in the executor's module docstring.
- **Three of the four interrupt-check answers raise.** The hook returns
  `continue | new_intent | cancel | unclear` (DESIGN.md 6.6) and the executor implements
  `continue`. `new_intent` needs the push, the return-to-interrupted-workflow prompt and the
  `blocked_in` handling of 6.6, which is phase 6; a half-implementation that pushed a frame and
  forgot to offer the customer their old workflow back would be worse than an error.
- **DESIGN.md 7.3's middle failure tier does not exist.** "The node's `on_error` edge if
  declared; otherwise the frame's `on_error` graph; otherwise handoff." Graphs have no
  `on_error` key in the schema phase 1 delivered, so the fall-through goes straight from the
  node's edge to the handoff hook. Adding the key is a graph-schema change and belongs with
  whoever needs it; nothing today can produce a frame-level error that a node-level edge could
  not catch.
- **LLM failure handling, tool failure handling, tool idempotency, `max_tool_calls_per_turn`
  and the cost cap** are phases 3, 4 and 9. The engine enforces `max_nodes_per_turn` only.
- **The handoff is a hook call and a status.** No packet, no queue row, no sink (DESIGN.md 13,
  phase 6). What phase 2 owes is that *every* failure path ends in one durable `waiting_human`
  run and one call, which it does: `limit_exceeded`, `node_error`, `pack_incompatible` and
  `timeout` all go through `_handoff`.
- **`sweep_timeouts` and `recover_stalled` are methods, not a scheduler.** Something has to call
  them; phase 7's service is where that lives.
- **The graph-version story is the safe half only.** The run records the pack fingerprint, and a
  frame whose stored state no longer fits the loaded graph hands off. It does *not* compare pins
  (the old pin is not stored, only its fingerprint), so a change that keeps every field name but
  changes a type, or adds a field, is undetected - the frame simply validates. DESIGN.md 6.7's
  migration hook and keeping two pack versions loaded are phase 9.
- **`support_core.api` still has no app**, so nothing calls the engine over HTTP (phase 7).
- **Conversation summary and customer memory** (DESIGN.md 10) are untouched; `ctx.summary` is
  read from the column and never written.

### Where does the code diverge from the design?

- **`gate` and `ask` are executable now.** Argued in the plan and recorded in the backlog's
  decisions log. The risk a reviewer should weigh: `ask` is now half phase-2 and half phase-3,
  and phase 3 must replace `extract_slots` rather than the node, or there will be two ways to
  fill a slot.
- **`register_node_type` exists.** DESIGN.md 6.2 promises it; phase 4 was going to deliver it
  with pack imports. It is used only by tests today, and `load_pack` still does not read a
  pack's `nodes/` directory, so the promise is half kept: the mechanism is there, the wiring is
  not.
- **`pack.yaml` gains `timeouts:`.** An addition to a schema DESIGN.md 5.1 shows in full. It is
  required by 7.2 and has defaults, so an existing manifest is unaffected.
- **`run` and `trace_step` gained six columns and two constraints** beyond DESIGN.md 17's list
  (`seq`, `error`; `pack_fingerprint`, `turn_nodes`, `next_frame_seq`, `suspended_at`,
  `timeout_at`, `awaiting`; `uq_trace_step_run_seq`, `uq_run_conversation`). All additive, all
  behaviour 7.1 to 7.3 asks for. `uq_run_conversation` is the one that closes a question rather
  than adding a field, and it is in the decisions log.
- **`conversation.context["inputs"]` seeds the entry graph's state.** DESIGN.md never says how
  the root graph is invoked because its own root graph takes no inputs. This is my invention and
  the piece of phase 2 I am least sure about: it puts a graph-shaped thing inside a column whose
  other keys are `ConversationContext` fields, and `_context` has to remember to pop it. A
  separate column, or a `Conversation.entry_inputs`, would be cleaner.
- **`NodeResult` gained `pop` and `outputs`.** DESIGN.md 6.3 lists the fields an `llm` or `tool`
  node needs and 6.2 says `end` "pops the frame, returns outputs" without saying how the node
  says so. Keeping it in the result rather than special-casing `EndNode` in the executor is what
  lets a registered node type end a frame.
- **The lock waits instead of returning.** DESIGN.md 17 does not say who processes a queued
  message. A caller that cannot take the lock waits up to `lock_wait_seconds` and becomes the
  next drainer, because returning immediately leaves a window where the current holder finished
  draining just after the newcomer's row became visible and nobody comes back for it.
  `lock_wait_seconds=0` gives the non-blocking behaviour, and the queue is drained by the next
  arrival or by `drain`.
- **Outbound messages are written inside the checkpoint and sent after it.** DESIGN.md 7.1 sends
  before advancing; writing the row first is what makes a crash between the two survivable, at
  the cost of at-least-once delivery towards the channel.
- **The phase-1 reading of `inputs` is unchanged** ("an input lands in the state field of the
  same name"). Phase 1's self-critique invited phase 2 to overrule it; I did not, because
  nothing in the engine needed a different answer and changing it would touch the validator, the
  loader and every fixture.

### Which tests are weak?

- **The crash matrix runs one pack, one path, one turn.** Twenty-eight combinations sounds like
  a lot, but they are twenty-eight points on a single seven-node deterministic route with no
  suspension in it. A crash *during a resume* is covered by exactly one test
  (`test_a_crash_between_claiming_a_message_and_delivering_it_keeps_the_message`) and is not
  compared against a baseline; a crash during a **gate re-check** is covered by none. Those are
  the two places where the resume event and the stack interact, which is where both bugs of this
  phase were. If I had another hour this is what I would write: parametrise the crash matrix
  over the `engine_pack` conversation as well, so every point of a suspend-resume turn is
  covered the same way.
- **Nothing randomises the crash point.** A property test that runs a conversation, kills it at
  a uniformly chosen probe occurrence, resumes, and compares to the baseline would explore
  points no hand-written list contains - and would keep exploring as phases 3 to 6 add nodes.
- **The concurrency test is two processes, once.** No three-way contention, no two processes
  crashing at once, no test that a second process arriving *during* the first's drain loop (not
  before it) is still ordered. The lock makes all of these the same code path, but "makes them
  the same path" is an argument, not a test.
- **`sweep_timeouts` and `recover_stalled` are never tested under contention.** Both take the
  lock, and `recover_stalled` deliberately skips a conversation it cannot lock, but nothing
  proves a sweep running against a live turn is harmless.
- **The gate tests never see a redirect satisfy its own gate.** Nothing in phase 2 can change
  `ctx` from inside a graph - that needs phase 4's identity tools - so the satisfying path is
  simulated by writing `conversation.context` from the test between turns. The negative property
  (the protected node is not reached) is proven; the positive one is staged.
- **`NodeRuntime` is barely exercised**: only `render` is called. It is the object phases 3 to 5
  hang everything on, and today it is nearly empty.
- **No test asserts the whole `run.awaiting` vocabulary**, so a later phase could change the
  shape of `{"kind": "node" | "handoff", ...}` and only `_resume`'s behaviour would notice.
- **The email channel is tested for its timeout value and nothing else.** Inbound and outbound
  on email are phase 7, but `_channel` already branches on it.
- **Test time.** The suite went from 12s to 120s, most of it the crash matrix rebuilding a
  conversation twice per case. That is a real cost for every later phase, and the honest fix is
  a baseline computed once per module rather than per case.

### What would break under concurrency or a crash mid-step?

The things I believe are solid, with the reason each one is more than an assertion:

- **A crash cannot produce a half-applied step.** The frame stack, the trace step and the node's
  outbound messages are one transaction, so either the node happened and the stack knows, or
  neither. Killed inside the transaction, `checkpoint_seq` and the trace agree
  (`test_the_frame_stack_and_the_trace_step_commit_together`).
- **A retried step keeps its id**, because every field of `run_id:frame_seq:node_id:attempt`
  comes from the row, and the attempt counter advances in the same transaction as everything
  else. That is what phase 4's idempotency will rest on.
- **A dead process releases its lock** without anyone running an unlock, because the lock lives
  in that connection's transaction.
- **A message is never lost and never overtakes**: it is written `pending` before the lock is
  attempted, claimed under the lock, and put back - atomically with the run forgetting it - if
  no node consumed it.

What I know is fragile:

1. **`hooks.send` runs outside the transaction, so delivery is at-least-once.** A crash between
   the commit and the send leaves rows `pending_send`, and the next checkpoint or the next turn
   re-offers *all* of them to the channel. Phase 7's adapters have to deduplicate; nothing in
   the engine will. Worse, `_flush_outbound` offers every pending row, so a send that fails
   permanently for one message blocks the rest of the conversation's delivery behind it, and a
   raising `send` hook aborts the turn (leaving the run `running`, recoverable, but a customer
   sees nothing).
2. **A stalled run needs someone to call `recover_stalled`.** Without it, a crashed turn on a
   conversation nobody writes to again sits `running` for ever, and so does its pending queue.
   There is no daemon in the engine; if phase 7 forgets to schedule it, this is a silent leak.
3. **`lock_wait_seconds` defaults to 30 and the caller blocks.** Under a burst on one
   conversation, callers queue on the advisory lock holding a connection each. A pool of N
   connections and N+1 concurrent inbound messages on one conversation is a stall. The engine
   should probably grow a "queue and return" mode for channels that cannot wait, with a poller
   draining; today the choice is `lock_wait_seconds=0` and hoping something calls `drain`.
4. **The lock connection sits idle in transaction for the whole turn.** Once turns include LLM
   calls (phase 3) that is seconds, not milliseconds, and an idle-in-transaction connection
   holds back vacuum on the whole database. A session-level `pg_advisory_lock` with an explicit
   unlock would avoid the open snapshot; I chose the transactional form because it cannot leak,
   and I think that is right, but it is a trade and phase 7 should measure it.
5. **`ctx` is read once per turn.** A gate re-check inside a turn evaluates against the context
   as it was when the turn started, so a change made by another process mid-turn is invisible
   until the next turn. Since `ctx` is single-writer per conversation too, the only writer that
   could race is an external system updating `conversation.context` directly - which is exactly
   what the gate tests do between turns.
6. **A crash during a gate re-check is untested and the ordering is delicate.** The re-check
   checkpoints a step for the gate node and pushes a frame in the same transaction, so it should
   be as safe as any other node; but the resume event survives that checkpoint in
   `run.awaiting`, and the interaction of "event still pending" with "stack changed" is the one
   piece of state that lives in two places at once.
7. **Two conversations whose ids collide in the 64-bit lock key would serialise.** Blake2b over
   the UUID makes that ~2^-63 per pair; the failure mode is slowness, not corruption.
8. **`run.frames` has no size bound.** A deep stack with large states is rewritten in full on
   every checkpoint, so a pathological pack makes every node write O(stack) bytes. Nothing
   limits stack depth either: only `max_nodes_per_turn` bounds a runaway recursion, and it does
   so per turn, not per conversation.
9. **A popped frame's final state exists only as patches in the trace.** Replay (phase 7) can
   reconstruct it by folding `state_patch` in `seq` order, which works because every state
   change goes through a patch - but nothing tests that folding, and the day a node mutates
   state some other way, replay silently diverges from what happened.
10. **`turn_nodes` is reset by `_begin_turn`, which is not part of a checkpoint.** A crash
    between that write and the first checkpoint leaves the counter at zero for a turn that has
    already run nodes - harmless (the limit is generous) but it means the limit is per *attempt*
    at a turn, not per turn.

### What I fixed while writing this

Two of the items above started as findings in this section and were fixed rather than recorded:
the stalled-run leak (item 2, now `recover_stalled` with a test) and the crash matrix's gap over
the push and pop nodes (now all seven nodes rather than four). The rest stands as written.
