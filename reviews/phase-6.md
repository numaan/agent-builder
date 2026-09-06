# Phase 6 review: Interrupts, root graph, handoff

Design references: DESIGN.md sections 3 (principles), 6.5, 6.6, 7.2, 7.3, 13, 14.
Backlog: BACKLOG.md "Phase 6" plus the deferred findings assigned to it (phase 1 N3, the phase 4
`confirm_exempt` argument report, and the phase 4 self-returning `on_error` shape). Inherits
every settled decision in reviews/phase-0.md to phase-4.md and reviews/phase-w.md.

## Plan

Written before any code, per PLAN.md step 1.

### What this phase is really about

Two of the three parts are the demo's most visible gaps, and they are visible for opposite
reasons. The interrupt is the thing a person watching a demo *tries*: they start a refund and
then change the subject, and either the system copes or it does not. The handoff is the thing
nobody sees and everybody relies on: today every failure path in the engine ends in a run parked
`waiting_human` that no human is ever told about, and `packs/acme_billing` says "I cannot do that
yet" precisely because promising otherwise would be a lie. Ordered accordingly:

1. **The interrupt check and the return to the parked workflow** (6.6). The single most
   demonstrable thing in the phase.
2. **The handoff node, packet, sinks and desk API** (13). The thing that makes the honest message
   in `root.yaml` replaceable by a true one.
3. **Every failure path of 7.3 routed to a real handoff with the right reason.**
4. **The sample pack**, so both are demonstrable against `packs/acme_billing`.

*(Everything above this line was written before any code. The plan's own wording is left as it
was, with one correction marked in item 6 and one in item 3, so that what was predicted and what
was built can be told apart.)*

### Task breakdown

1. **Interrupt intents from the root graph** (`support_core/engine/interrupts.py`).
   DESIGN.md 6.6: "Available intents are the root graph's declared edges." Derive them: for each
   edge label of the entry graph's start node, if the target is a `subgraph` node, the label is an
   intent naming that node's graph. A pack therefore declares its interruptible workflows by
   writing its root graph, not by writing a second list.

2. **The interrupt check as a structured model call.** `LlmService.read_interrupt` +
   `StructuredInterruptCheck` in `llm/wiring.py`, filling the existing
   `EngineHooks.interrupt_check` seam that phase 2 left. Schema: `kind`
   (`continue|new_intent|cancel|unclear`), `intent`, `confidence`. **Coded defensively** against
   the GLM family of defects in the decisions log (absent and nested values spelled as text): the
   word `null` as an intent means no intent, a `kind` of `new_intent(refund)` is read as
   `new_intent` plus that intent, and surrounding quotes and whitespace are stripped before the
   `Literal` is matched. An unknown intent degrades to `unclear`, never to a guess.

3. **The engine's interrupt handling** (`executor.py`, in `_claim_turn`, which already runs
   before the claim precisely so a model call can happen there):
   - `continue` / `unclear`: resume the suspended node, as today.
   - `new_intent(g)` where the **top frame's graph** allows interrupts: mark the suspended frame
     parked, push an `interrupt` frame for `g`. The customer's message is consumed by the check
     itself and is not re-queued: it is what said to switch.
   - `new_intent(g)` where the top frame's graph is in `blocked_in` (or is not in
     `allowed_from`): resume the suspended node **with a hint** on the `ResumeEvent`, record the
     secondary intent durably, and emit one core-written deferral line. This is DESIGN.md 19
     step 8 exactly.
   - `cancel`: pop back to the root frame and acknowledge. Allowed from anywhere, including a
     `blocked_in` graph: refusing to let a customer stop is worse than any workflow it interrupts.
   - When the interrupt frame ends, the parked frame is offered back (6.6 step 4) through a new
     hook (`resume_offer` as built) whose default is a keyword reading and whose real
     implementation is a structured call. `unclear` re-offers; `no` abandons the parked frame.
   - **Gates fire first.** The gate re-check already runs on every frame entry and runs before the
     return offer, and an interrupt frame is a *fresh* frame whose `passed_gates` is empty, so its
     own gates run from the start. Tested specifically, both ways round.

4. **Secondary intents, surfaced to the root graph** (DESIGN.md 19 step 15: "the engine surfaces
   the recorded secondary intent. The model chooses `update_address`"). Stored in a new
   `run.secondary_intents` column, written in the same transaction as the claim; rendered into
   layer 6 as its own fenced data block, and only for a node in the **root** frame. Cleared when a
   frame for that graph is pushed and when the root frame pops.

5. **The handoff packet and its sinks** (`support_core/handoff/`). `HandoffPacket` is DESIGN.md
   13's model field for field. `build_packet` reads it all from durable state: the run's frames
   for `workflow`/`node`/`state_snapshot`, `tool_calls_for_run` filtered to WRITE and HIGH for
   `actions_taken`, the live unconsumed approval for `pending_action`, `ctx.customer` for
   `identity_verified` and `customer`, and an LLM-written `summary` using the pack's
   `escalation_model`. `citations` stays empty with the retriever seam named in its docstring:
   phase 5 is deferred and I will not invent a citation.
   `HandoffSink` protocol, `PostgresQueueSink` (the `handoff` table), `WebhookSink` (httpx POST),
   `CompositeSink`.

6. **The `handoff` node** (`HandoffRunner`), reaching the packet builder through a
   `NodeRuntime.handoff` closure the executor builds - the same shape as `NodeToolAccess`, because
   DESIGN.md 6.3 says nodes never touch storage. `run` builds and delivers the packet, says one
   sentence to the customer and suspends `waiting_human`; `resume` takes the `resumed` or `closed`
   edge from what the desk did. *(Changed while building: the executor builds and delivers the
   packet, not the node. See the implementation notes.)*

7. **The desk API** (`support_core/api/desk.py`, mounted by `create_app`): list handoffs, read one,
   reply, resume with a state patch, close, read a conversation transcript (which is what
   `packet.transcript_url` points at), and **approve a pending action**, which is what makes
   phase 4's `requires_human_approval` satisfiable at last.

8. **Failure routing** (7.3). The executor's `_handoff` becomes a real packet through
   `HandoffService`, which is also the `EngineHooks.handoff` implementation, so every path phase 2
   routed - `limit_exceeded`, `node_error`, `pack_incompatible`, `timeout`, `engine_error` - and
   every reason phase 3 and 4 added - `llm_unavailable`, `llm_invalid_output`, `low_confidence`,
   `model_requested_handoff`, `tool_refused`, `tool_failed` - arrives at a sink with that reason on
   it. Plus a **per-node consecutive-error cap** (`limits.max_node_errors`, counted in the frame so
   it survives a crash), which is the run-time half of the deferred `on_error` finding.

9. **Deferred findings.** Phase 1 N3: `graph.subgraph_cycle` becomes path-sensitive - a leg of a
   cycle counts only when the call node is reachable from that graph's start without passing a
   suspending node. The interprocedural CFG gains the interrupt push and return edges. Phase 4's
   `confirm_exempt` report gains the *arguments* each call site passes, so a model-written address
   reads differently from `ctx.customer.email`. Phase 4's `on_error` shape gets a load-time
   WARNING as well as the run-time cap.

10. **The sample pack.** `no_workflow` becomes a real `handoff` node with an honest message;
    `refused` keeps its `say` node, because a refusal is not a misunderstanding. `payment_capture`
    leaves `blocked_in` (no such graph, and it was one of the validator's forward-reference
    warnings). New cassette scenarios for the blocked interrupt (DESIGN.md 19 end to end), the
    allowed interrupt with the return offer, and the handoff.

### Intended deviations from DESIGN.md, and why

- **`allowed_from` is an allow-list, not a default.** A graph that appears in neither list cannot
  be interrupted; it is treated exactly like `blocked_in` (hint plus recorded secondary intent).
  DESIGN.md 6.6 says "if the current graph allows interrupts", and the safe reading of "allows" is
  "says so".
- **The deferral sentence is written by core, not by the model.** DESIGN.md 19 step 8 has the node
  reply "Thanks. I'll come back to the address change once the refund is sorted", but the nodes
  that suspend into `waiting_customer` are `ask` and `confirm`, and neither speaks on resume. The
  hint reaches the model too (it is on the `ResumeEvent` and in the `SlotRequest`, which stops an
  extractor reading "change my address" as a passcode), but the sentence the customer sees is
  core's, so that the promise is one the engine actually keeps.
- **The return offer is a core step on the parked frame, not a node.** It writes a trace step under
  the reserved node id `__interrupt_return__`, which no pack can collide with because a pack node
  id must match `^[a-z][a-z0-9_]*$`.
- **`HandoffNode` gains an optional `message`.** DESIGN.md 6.2 gives the node a `reason` and two
  edges and no way to say anything; a node that parks a customer in silence is worse than one that
  does not exist. Optional, with a core default.
- **`pack.yaml` gains `limits.max_node_errors`.** Same precedent as phase 2's `timeouts:` and phase
  3's `memory:`; it is the bound DESIGN.md 7.3's retry story needs and section 5.1 has nowhere to
  put.
- **7.3's middle failure tier - "otherwise the frame's `on_error` graph" - is still not
  implemented.** Phase 2 recorded it as a divergence and phase 2's reviewer endorsed the omission;
  nothing this phase adds can raise a frame-level error a node-level edge could not catch, and the
  fall-through now reaches a real packet rather than a bare status. Recorded again rather than
  invented.
- **No retrieval, no citations, no email, no tracing, no eval harness.** Phases 5, 7 and 8.

### What I will not claim

Every model call in this phase is scripted or replayed: there is no API key in this environment.
The interrupt check's *behaviour under a real model* - whether it says `continue` when a customer
mentions their address in passing - is not measured here, and the self-critique will say so.

## Implementation notes

Environment: Windows 11, Python 3.13, Postgres 16 in `customer-support-agent-db-1`, database
`support_test`. No `ANTHROPIC_API_KEY` and no GLM key, so every model call went through phase 3's
`ScriptedProvider` or `FakeProvider`. Another agent was committing roadmap edits to `BACKLOG.md`
in the same working tree while this phase was built (`97affb5`, and three before it); nothing of
theirs was lost and nothing of theirs is phase 6's.

### Shape of the code

```
support_core/engine/interrupts.py    what a pack allows, and the four sentences core says  (173)
support_core/graph/routing.py        the root graph's declared edges, as workflows          (69)
support_core/handoff/
  packet.py     DESIGN.md 13's HandoffPacket, field for field                              (145)
  builder.py    reading one out of durable state; the fallback summary; the next steps     (236)
  sinks.py      HandoffSink, NullSink, PostgresQueueSink, WebhookSink, CompositeSink       (201)
  service.py    the one object the handoff node and the failure ladder both use            (208)
support_core/api/desk.py             list, read, reply, resume, close, approve, transcript (312)
support_core/engine/executor.py      + _interrupt, _offer_return, _tell_a_human, the cap  (+450)
support_core/storage/migrations/versions/0008_handoff_and_interrupts.py
tests/packs/interrupt_pack/          three workflows: allowed, blocked, and behind a gate
tests/test_interrupts.py             22 tests, DESIGN.md 6.6 step by step                  (664)
tests/test_handoff.py                16 tests: the packet, the sinks, every 7.3 reason     (551)
tests/test_desk_api.py               8 tests over real HTTP and a real socket              (320)
tests/test_interrupt_check_schema.py 21 cases of what a model's spelling may be            (103)
```

### Where the plan changed while building

- **The executor builds the packet, not the node.** The plan gave the `handoff` node a
  `NodeRuntime.handoff` closure, symmetric with `NodeToolAccess`. Two things killed that. It
  would have made `support_core.engine` import `support_core.handoff` at module scope, which
  imports the engine's own types back - a cycle whose only fix is `TYPE_CHECKING` in three
  places. And it puts the delivery *inside* the node, where the executor cannot order it against
  the checkpoint. Moving it to `_advance` got the ordering right for free: deliver, then
  checkpoint, so a crash in the window re-executes and re-delivers into an idempotent write. The
  node now says one sentence and suspends, which is all DESIGN.md 6.2 asks of it.
- **The root frame is never interrupted**, which the plan did not anticipate. It fell out of
  writing the return offer: "shall we go back to: is there anything else?" is not a question, and
  DESIGN.md 6.5's classifier is a better answer to a topic change at the root than any interrupt.
  Recorded in the decisions log.
- **The intent derivation moved into the `graph` package** (`routing.py`) because the *validator*
  needs the same answer: an interrupt adds control-flow edges, and the confirm-coverage analysis
  has to see the paths they create. Two derivations would have been two answers.
- **A third defect of the GLM family was coded for before it was seen.** The decisions log
  predicts one; the shape it would take here is `kind: "new_intent(update_address)"`, which is
  DESIGN.md 6.6's *own* notation for that answer and so exactly what a model shown the design's
  vocabulary would write. `InterruptCheck` reads it, along with `new_intent: refund`, quoted
  labels, and the recorded `null`-as-text case in `intent`. 21 cases pin which spellings are read
  and which are refused.
- **`limits.max_node_errors` counts consecutive failures, not attempts.** The first cut counted
  attempts per node per frame, which hands off a root graph's `classify` node on the fifth turn
  of an ordinary conversation. Consecutive-and-cleared-on-success is the version that bounds a
  retry loop without touching a loop.

### Things worth knowing that came up while building

- **A tight `on_error` cycle back into a node is already refused at load**, by
  `graph.unsuspended_cycle` from phase 1. So the shape phase 4 deferred can only be expressed
  through a suspending node - `fails -> ask -> fails again` - which means the error count has to
  survive a suspension, which is why it lives in the frame rather than in the turn.
- **The return offer had to advance its attempt counter.** The first version deliberately did
  not, on the theory that a re-offer is the same question being asked again; the second offer
  then wrote a second `trace_step` under the same step id and hit `uq_trace_step_step_id`. The
  constraint was right and the theory was wrong: three checkpoints are three steps.
- **`reset_backend()` did not reset the address book.** Invisible until a *third* conversation
  reads the address a second one changed into its prompt, at which point a recorded cassette
  stops reproducing. Found by the drift test that exists for exactly this.
- **Adding one key to `nonce_for` re-keys every cassette.** The delimiter token is a hash over
  every prompt slot, so `pending_intents` joining the record changed every fingerprint. All seven
  cassettes were regenerated with `python -m tests.cassettes.build_cassettes`.
- **Two golden scenarios had to be re-recorded for a reason worth stating**: the interrupt check
  is a model call on *every* reply to a suspended workflow, so it is part of the conversation
  whether or not the customer changes the subject. Recording it rather than letting it fail is
  what makes the replay the conversation the service would really have.

### Commands run at the end of the phase

From the repository root with `.venv/Scripts/python.exe`; Postgres 16 in
`customer-support-agent-db-1`; no `ANTHROPIC_API_KEY` and no GLM key in this shell.

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) |
| `python -m ruff format --check .` | `160 files already formatted` (exit 0) |
| `python -m mypy` (strict) | `Success: no issues found in 160 source files` |
| `python -m pytest -q` | `1351 passed, 2 deselected in 465.79s` |
| `python -m pytest -q -m live` | `2 skipped, 1352 deselected` - skips on the missing key rather than failing |
| `python -m pytest tests/verify_phase_2_resolution.py -q` | `39 passed in 131.40s` - phase 2's proof harness still holds |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (18 warning(s))`, exit 0 |
| `alembic downgrade base`, `upgrade head`, `check` (`support_test`) | all eight revisions down and up cleanly; `No new upgrade operations detected.` |
| `alembic check` (development database) | `No new upgrade operations detected.` |
| `python -m tests.cassettes.build_cassettes` | seven cassettes; the committed files are what the builder produces |

The 1351 are phase W's 1259 plus 92: 23 interrupts, 16 handoff, 8 desk, 21 interrupt-check
spellings, 9 more golden-conversation cases (two new scenarios), 5 validator (the two new rules
and the path-sensitive cycle, both ways), and one each for the desk's second signature, the
retry bound, `graph.confirm_exempt`'s call sites and the demo configuration. Two tests inverted
rather than being added: `handoff` was the last core node type that could not run, so "the engine
refuses node types core cannot run yet" is now "every core node type is executable", and the
refusal machinery is asserted where it still applies - a pack-declared type with no runner.

The 18 `pack validate` warnings are 16 `graph.assignment_optional` and
`graph.state_type_unresolved` from the refund and address graphs (phase 4's deferred finding R8,
owned by phase 9) and the 2 `graph.confirm_exempt` lines, which now name their call sites. The
`manifest.interrupt_graph_unknown` warnings phase 3's review asked this phase to close are gone:
every graph the manifest names exists.

### One thing worth a reviewer's attention first

`Executor.resume_human(close=True)` changed behaviour. It used to close the conversation
directly; when a node is waiting it now delivers the close to that node so a pack's `closed` edge
runs, and only a run the *engine* parked is closed the old way. That is the one existing entry
point this phase altered, and it is where I would look first for a case I have not thought of.

### Verification of the exit criterion

DESIGN.md section 19 end to end with the fake provider, including step 7's interrupt and step
15's secondary intent, is `tests/cassettes/scenarios.py::ACME_INTERRUPT_DEFERRED`, replayed by
`tests/test_golden_conversation.py` against real Postgres. Its asserted path is the design's
conversation: the refund pushes `verify_identity`, the customer answers the passcode question and
asks for an address change in the same message, `verify_identity` is in `blocked_in` so the
request is recorded and the node resumes with a hint, the refund completes, and four turns later
the root graph's classifier is shown the request it could not take up and pushes
`update_address`. The interrupt the *other* way - a workflow that does stop, parked and offered
back - is `ACME_INTERRUPT_SWITCH`, whose asserted path contains the `__interrupt_return__` step
twice: the offer, and the answer to it.

## Self-critique

Written after re-reading DESIGN.md 3, 6.5, 6.6, 7.2, 7.3, 13 and 14, and PLAN.md.

### What did I skip or simplify?

- **DESIGN.md 7.3's middle failure tier is still missing**, for the third phase running: "the
  node's `on_error` edge if declared; otherwise the frame's `on_error` graph; otherwise handoff."
  Graphs have no `on_error` key. I argued in the plan that phase 2's reviewer endorsed the
  omission and that the fall-through now reaches a real packet rather than a bare status, and I
  still think that is right - but the phase whose checklist line says "all failure paths from
  section 7.3" is the phase that should have said so out loud rather than in a plan, so it is
  here too. What it would cost: a key on the graph schema, a `FrameKind`, CFG edges, and a
  decision about what a frame-level handler may do that a node-level one may not. What it would
  buy today: nothing I can name.
- **Only `waiting_customer` suspensions are interrupted.** DESIGN.md 6.6 step 1 says "suspended
  in `ask` or `confirm`", which is exactly what is implemented - but a run parked
  `waiting_human` also gets customer messages, and today they queue until the desk resumes. That
  is defensible (the customer is waiting for a person, and a topic change should not smuggle the
  workflow past them) and it is not stated anywhere in the design. It should be a decision, and
  it is currently an implementation detail of `RESUMABLE_BY_CUSTOMER`.
- **A parked frame has no expiry.** A customer who interrupts a refund, finishes the second
  workflow and then abandons the conversation leaves a frame parked for ever; the per-status
  timeout applies to the *run*, not to the parked frame, so it does the right thing by accident.
  Nothing tests that.
- **Only one workflow can be parked at a time in practice.** The mechanism is per-frame and would
  nest, but the check refuses an interrupt naming the graph it is already in and nothing refuses
  a *third* level, so a customer could push interrupts three deep and be offered three workflows
  back in reverse order. That is arguably right and is certainly untested.
- **`cancel` pops frames without telling the pack.** A workflow that had allocated something - a
  held seat, a draft - has no `on_cancel`. Nothing in this pack does, so nothing leaks; a pack
  that did would have no way to say so.
- **The webhook sink has no retry, no signature and no timeout budget beyond httpx's.** A queue
  that is down loses the page and keeps the row. Phase 7 owns the scheduler that would retry it.
- **No cost accounting for the new model calls.** The interrupt check runs on every reply to a
  suspended workflow and the handoff summary uses the *escalation* model, which is the expensive
  one. DESIGN.md 20 says to run interrupt checks on a cheaper model; nothing does. The per-node
  `model:` override exists but the interrupt check is not a node.
- **The desk has no authentication**, which is worse than phase W's gap rather than equal to it:
  the web chat endpoints expose *your own* conversation to whoever holds your session key, and
  this exposes everybody's to anybody. It is stated at the top of `desk.py` and in the README and
  it is phase 7's first job, but a reviewer should weigh whether shipping it off by default would
  have been better than shipping it on with a warning.
- **No retrieval, so `packet.citations` is always empty.** That is the seam the instructions
  asked for rather than a gap, and the field is on the model so phase 5 fills a shape.

### Where does the code diverge from the design?

- **Core writes four customer-visible sentences** (deferral, switch, return offer, cancelled).
  DESIGN.md 19 step 8 attributes the deferral to the node. Argued in the decisions log; the cost
  is that they are not in the pack's voice and a pack cannot change them.
- **`allowed_from` is an allow-list and the root frame is never parked.** Both in the decisions
  log. The second means DESIGN.md 5.1's example manifest, which lists `root` in `allowed_from`,
  is inert in that one respect - a pack author could reasonably expect it to do something.
- **`HandoffNode` gained `message` and `next_steps`**; `pack.yaml` gained
  `limits.max_node_errors`; `handoff` gained six columns and `run` gained one. All additive, all
  in the decisions log.
- **The return offer is a step under a reserved node id**, which puts a node id in the trace that
  no graph declares. A replay endpoint (phase 7) will meet it and should render it as what it is.
- **`resume_human(close=True)` no longer closes the conversation directly** when a node is
  waiting: it delivers a `close` to that node so the pack's `closed` edge runs. For a run the
  *engine* parked, the phase-2 behaviour stands. That is a behaviour change to an existing entry
  point, and it is the one I would most want a reviewer to check for a case I have not thought of.

### Which tests are weak?

- **Every interrupt-check answer in the engine tests is a stub.** That is deliberate and stated -
  the engine's behaviour is what is under test and there are exactly four answers - but it means
  nothing here measures whether a *model* says `continue` to "sure, 581139, and also my address".
  The golden conversations use a scripted rule keyed on the customer's exact words, which is a
  stand-in for a classifier and not a classifier. Phase 8's evals are the only honest place for
  that, and until then the headline feature's *trigger* is unmeasured.
- **One durability test for the whole phase.** `test_an_interrupt_survives_a_crash_between_the_
  push_and_the_first_node` covers one kill point. Phase 2's reviewer built a 28-point matrix over
  a suspend-and-resume turn and found two must-fix bugs with it; the interrupt push is a *new*
  place where the stack moves while a message is pending, which is precisely the configuration
  that broke then. If I had another hour this is what I would spend it on: parametrise
  `tests/verify_phase_2_resolution.py`'s matrix over an interrupting turn.
- **Nothing kills a process between delivering a packet and checkpointing the handoff.** The
  idempotency is asserted by calling `raise_handoff` twice, which is the *shape* of that crash
  and not the crash. The claim in the code's comments is stronger than the test.
- **No concurrency test anywhere in the phase.** Two desk actions on one conversation, a desk
  resume racing a customer message, two replicas draining a conversation whose frame is parked -
  all are serialised by the advisory lock, and "the lock makes them the same path" is an
  argument, which is what phase 2's self-critique said about its own concurrency gap.
- **The desk tests use one conversation shape.** No test resumes a handoff *inside* a sub-graph,
  which is where a state patch and a `resumed` edge get interesting, and no test approves an
  action and then watches the tool run - the two halves of `requires_human_approval` are tested
  in different files against different fixtures.
- **`CompositeSink`'s failure path is tested; its success path with two live sinks is not.**
- **The `graph.on_error_repeats_side_effect` rule is tested on two fixtures**, and neither is a
  gate redirect reaching the node from another graph, which is the case the deferred finding said
  needed the cross-graph model.

### What would break under concurrency or a crash mid-step?

What I believe holds, and why:

- **An interrupt is durable before it can be observed.** The frame is pushed in the same
  transaction as the claim (`claim_and_begin_turn`), so a process that dies leaves either a
  pending message and no push, or a `running` run whose stack already contains the interrupt
  frame and whose `awaiting` says no node is waiting for the message. Both re-enter cleanly.
- **A secondary intent commits with the claim** for the same reason and cannot be recorded twice:
  `_defer` refuses a duplicate graph, and `_push` clears it when the workflow actually runs.
- **The parked frame cannot be resumed past its gates.** `check_gates` runs before the return
  offer is even made, and again on the entry after it, so the two orders a reviewer would try -
  gate lapses while parked, gate lapses between the offer and the answer - both redirect.
- **A handoff packet is delivered at least once and queued at most once**, by the ordering
  (deliver, then checkpoint) and `uq_handoff_run_step` respectively.

What I know is fragile:

1. **The interrupt check is a model call before the claim**, so a turn now begins with a
   round trip that a slow provider can stretch. It is inside the conversation lock, which is
   phase 2's deferred finding R7 made slightly worse: one more second of held lock per reply to a
   suspended workflow. Nothing measures it.
2. **A parked frame's state can go stale in a way a gate cannot see.** Gates re-check a
   *predicate*; a refund frame parked at `confirm_refund` re-presents its proposal from the state
   it holds, and if the interrupting workflow changed something the proposal reads, the customer
   is re-shown a proposal built from state that moved. The `confirm` node's hash comparison
   catches the case where it moved *between* the question and the answer, but the re-presentation
   happens *before* that comparison, so what protects this is that a re-presented proposal is
   recomputed from current state. I believe that is correct and I have not tested it.
3. **`_offer_return` is not a node, so its failures have no ``on_error`` edge.** Fixed while
   writing this rather than recorded: a `resume_offer` hook that raises is now read as
   ``unclear``, which re-offers, because asking again costs one message and taking the turn down
   costs the conversation. What is *not* covered is a failure of the checkpoint itself, which is
   the same exposure every core step has.
4. **The desk's `reply` writes a message outside any lock.** It is not a turn, so it takes none;
   two humans replying at once produce two rows with the same `created_at` and ordinal 0, which
   `ORDER BY created_at, ordinal, id` then breaks by a random UUID - phase 2's finding R4, in the
   one place phase 6 added that does not go through a checkpoint.
5. **`HandoffService.delivered` and `.failures` are per-process lists.** They are for tests and
   for making a dropped page visible; a second replica has its own, and neither is durable. The
   `handoff` table is the durable record, and nothing sweeps it for a packet whose sink refused.
6. **Nothing bounds how long a run stays `waiting_human`.** The pack's `waiting_human` timeout
   defaults to never, so a queue nobody reads holds conversations for ever. That is the pack's
   choice to make and the sample pack has not made it.

### What I fixed while writing this

Two things started here and were fixed rather than recorded: the return offer's step-id
collision (found by the cassette build, then reasoned about here), and `reset_backend()` not
resetting the address book, which would have made every future re-recording order-dependent.
