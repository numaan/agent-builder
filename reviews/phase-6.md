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
| `python -m pytest -q` | `1352 passed, 2 deselected in 387.45s` |
| `python -m pytest -q -m live` | `2 skipped, 1352 deselected` - skips on the missing key rather than failing |
| `python -m pytest tests/verify_phase_2_resolution.py -q` | `39 passed in 125.38s` - phase 2's proof harness still holds |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (18 warning(s))`, exit 0 |
| `alembic downgrade base`, `upgrade head`, `check` | all eight revisions down and up cleanly; `No new upgrade operations detected.` |
| `alembic check` (development database) | `No new upgrade operations detected.` |
| `python -m tests.cassettes.build_cassettes` | seven cassettes; the committed files are what the builder produces |

**Where those runs happened, and why it matters.** Phase-0 finding N9 is still open and it bites
hard: two `pytest` processes against one database corrupt each other's fixtures, and the
signature is a spray of unrelated failures - leaked rows, `NoResultFound`, websocket timeouts -
that do not reproduce individually. A reviewer began working in this tree while this phase was
being verified, and two full runs collided with theirs (13 failures, then 56, none of which
reproduced alone). The numbers above are from a run against a **private database**,
`support_phase6_test`, named through `SUPPORT_TEST_DATABASE_URL` - which is the explicit opt-in
phase-0 finding F2 added for exactly this. Same code, same Postgres, same migrations; only the
database name differs. Anyone reproducing them should do the same, or make sure they are alone.

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

## Independent review

Reviewed by an agent that did not write the code, against DESIGN.md 3, 6.5, 6.6, 7.2, 7.3, 13
and 14, PLAN.md, BACKLOG.md and reviews/phase-0 to -4 and -w. **The phase holds where it matters
most.** I could not reach a guarded action through an interrupt by any route I tried: an
interrupt frame is fresh, its `passed_gates` is empty, the gate re-check runs before the return
offer, and `consume_approval`'s `run_id + frame_seq + node_id + tool + args_hash + consumed_at IS
NULL` binding makes a parked frame's approval unusable anywhere else. I could not break
durability a third time either: three kill points around the interrupt push all resumed with the
message still accounted for and no protected node reached, and a customer message cannot drive a
run parked `waiting_human` - it stays `pending`. The interrupt check's defensive parsing is
genuinely defensive: of twenty spellings I fed it, every one was either coerced to the answer the
model plainly meant or refused, and none was guessed. What I did break is the desk: a `resume`
state patch is written into durable frame state with no validation of its field names, and one
undeclared key permanently bricks the conversation while blaming the pack for it. That, plus a
`queued` flag that is true when nothing was queued, is the substance of this review. The exit
criterion is met: `ACME_INTERRUPT_SWITCH` parks a refund mid-confirmation, runs `update_address`
over it and returns to complete the refund, and `acme_account_question` now ends `waiting_human`
with a real packet on `billing-tier-1`.

### Findings

| id | severity | location | finding | suggested fix |
|----|----------|----------|---------|---------------|
| P1 | must-fix | `support_core/engine/executor.py:838` (`_resume`), `support_core/api/desk.py:176` | A desk `resume` patch is applied with `turn.frame.state.update(...)` and never validated against the frame's graph state model. An undeclared key is written durably into `run.frames` and cannot be removed by any API call, and every later entry to that frame fails `_state()` and raises `IncompatiblePackError`, so the run is parked for ever with `handoff_reason = "pack_incompatible"` - which blames the pack for a desk typo and is the wrong 7.3 reason. Reproduced: patch `{"identity_verified": true, "not_a_field": "x"}` left `frames[-1].state == {"not_a_field": "x", "identity_verified": true}`, status `waiting_human`, reason `pack_incompatible`. The docstring in `desk.py` claims the patch is "validated by the graph's own state model on the next node entry, so a patch a graph cannot hold is a node error rather than a corrupted frame"; it is a corrupted frame. | Validate the patch against `self._graph(turn.frame).state.model` inside `resume_human` **before** applying it, and refuse it: `400` from the desk, no write, run untouched. The desk has no authentication, so an unvalidated durable write is also the cheapest denial of service in the system. |
| P2 | should-fix | `support_core/engine/executor.py:1510` (`_tell_a_human`), `support_core/handoff/service.py:130` (`deliver`) | `deliver` swallows every sink exception into a per-process `failures` list, so `_tell_a_human` returns `queued=True` when nothing was delivered, and `suspend_detail.queued` records "the hook did not raise" rather than "a human was told". Meanwhile `packs/acme_billing/graphs/root.yaml:no_workflow` tells the customer "I have passed this to a billing specialist ... They will reply here." That is a DESIGN.md 14 forbidden promise with no corresponding action record on exactly the path phase 6 built to stop being one. Nothing sweeps `failures`, and a webhook-only deployment therefore drops the page silently. | Have `deliver` report success or failure, return it from `_tell_a_human` honestly, and either fall back to the Postgres queue sink or suppress the pack's promise (and say something weaker) when nothing took the packet. A durable `handoff.status = 'undelivered'` row plus a sweep is the phase-7 half. |
| P3 | should-fix | `support_core/engine/executor.py:628-706` (`_interrupt`), `support_core/llm/wiring.py:169` | The interrupt decision's `confidence` is carried all the way to the engine and then never read. `pack.yaml`'s `llm.confidence_threshold` gates an `llm` node's decision but not this one, so a `new_intent` at confidence 0.0 parks a workflow and a `cancel` at confidence 0.0 unwinds the whole stack to root. DESIGN.md 14 says an injection flag "lowers the allowed decision confidence"; here there is no threshold to lower. `cancel` is the worst of the two, because it is the only answer the engine acts on from a `blocked_in` graph. | Apply `manifest.llm.confidence_threshold` in `_interrupt`: below it, read the answer as `continue` (which is already the safe default everywhere else in this module). |
| P4 | should-fix | `support_core/api/desk.py:215-252` (`approve`) | `approve` is scoped to the *conversation*, not to the handoff it is called on. It takes `live_approvals(...)[-1]` filtered to `approved_by == "customer"`, does not check the handoff's `status` (a `closed` or `resumed` row still signs), does not check `run_id`, and does not check that it is the same action the packet showed - `build_packet` prefers an *unfinished tool call* for `pending_action` and only falls back to the newest live approval, so a human looking at "issue_refund - indeterminate" can sign a different, newer approval with one click. The endpoint returns the tool and args it signed, which is a receipt, not a guard. | Resolve the approval from the handoff row (`run_id`, and the frame/node the packet named), refuse when the handoff is not `open`, and refuse when the resolved approval is not the packet's `pending_action`. |
| P5 | should-fix | `support_core/engine/executor.py:962-970`, `_loop` | Returning to a parked frame re-runs its current node, so a parked `confirm` proposes again and writes a **second** `ActionApproval` while the first is still live and unconsumed (same run, frame, node, tool and hash). Nothing is over-authorised in `refund` because no edge returns to `issue_refund` in the same frame, and `consume_approval` spends exactly one - but the invariant "one live approval per proposal" no longer holds, and any graph with an `on_error` path back to its tool node would find the spare waiting. `ACME_INTERRUPT_SWITCH` exercises this path in the shipped golden conversation. (Read from the code and from the scenario's shape; I did not count the rows.) | Supersede the frame's live approvals for that confirm node when it re-proposes: one `UPDATE action_approval SET consumed_at = now() ... WHERE run_id/frame_seq/node_id AND consumed_at IS NULL` in the same checkpoint that writes the new one. |
| P6 | should-fix | `support_core/engine/executor.py:139` (`RESUMABLE_BY_CUSTOMER`) | A customer message arriving on a run parked `waiting_human` is left `pending` for ever with nothing said to the customer and nothing shown to the desk. That is the right refusal (verified - the run does not move) and the wrong ending: on web chat it is a message into a void; on email, which phase 7 adds and where a customer *will* reply to "a specialist will pick this up", it silently swallows the reply until somebody resumes. The self-critique names this as an undocumented implementation detail; it is more than that once the pack promises a reply. | Make it a decision in the design and a behaviour in the code: surface the queued message on the desk's handoff view (it is already durable), and either acknowledge it to the customer or count it against the SLA. |
| P7 | should-fix | DESIGN.md 7.3; `support_core/engine/executor.py:1398` (`_route_error`) | 7.3's middle tier - "otherwise the frame's `on_error` graph" - is still not implemented, in the phase whose backlog line reads "all failure paths from section 7.3". The self-critique records it and argues nothing needs it yet; I agree it buys little today, but three phases of "recorded again rather than invented" is the point at which the design should be amended or the key added, not the phase after. | Either add `on_error:` to the graph schema with a `FrameKind` and CFG edges, or amend DESIGN.md 7.3 to two tiers and note the removal in the decisions log. |
| P8 | nit | `support_core/engine/executor.py:1274` | `return_offer(frame.graph_id)` always passes the graph id, never the intent label, although `return_offer` takes an optional label for exactly that. The customer is asked "Shall we go back to update address?" from the graph id rather than from the edge label the pack chose. | Park the intent's label on the frame when `_switch` runs and pass it here. |
| P9 | nit | `support_core/api/desk.py:247` | `record_human_approval` raises `ValueError` for an unbound template approval; the endpoint does not catch it, so a legitimate 4xx condition is served as a 500. | Catch it and return 409 with the message. |
| P10 | nit | `support_core/engine/interrupts.py:56`, `_offer_return` | The trace now contains `node_id = "__interrupt_return__"` with edges `offer`, `resumed` and `abandoned` that no graph declares. Correct and documented, but phase 7's `conversation_replay` renders the frame stack against graph nodes and will not resolve it. | Note it in the phase-7 backlog item (it is not there) so replay handles the reserved id rather than discovering it. |

### Interrupt bypass attempts

Run against `tests/packs/interrupt_pack` (`allowed_from: [alpha]`, `blocked_in: [beta]`, `gated`
behind `ctx.customer.identity_verified`) with the real executor and real Postgres. Scratch tests,
now deleted; the commands and outcomes are below.

| # | attempt | result |
|---|---------|--------|
| 1 | Suspended in `alpha`; interrupt with `new_intent(gated)`. Does the fresh frame skip the gate? | **Held.** Frames became `root/alpha/gated/verify`, the trace never contains `protected`, and the customer was asked for a verification code. |
| 2 | Inside `verify` (the gate's own redirect, which is in neither list and so is treated as blocked); ask for `gated` again, twice. | **Held.** `protected` never appears; the request is deferred both times. `blocked_in` by omission works. |
| 3 | Interrupt naming the graph the frame is already in (`alpha` from `alpha`). | **Held.** Read as `continue`; the reply was consumed by `ask_a`, the workflow finished normally and no second `alpha` frame was pushed. |
| 4 | `cancel` from `beta`, a `blocked_in` graph. | **Held and intended.** The stack unwound to `["root"]` and the customer was told nothing was done. Nothing durable was undone, which is what the sentence claims. |
| 5 | Interrupt while suspended at a `confirm`, then return and try to land on the tool step without answering again. | **Held.** The parked frame's `node_id` is still the confirm node and `pending_event` is `None` after `_switch`, so the confirm node **re-runs** and re-proposes; there is no path from the return offer into `issue_refund`. See P5 for what re-running costs. |
| 6 | Make a parked confirm's approval satisfy a tool call in the interrupting workflow. | **Held by construction.** `consume_approval` matches `run_id`, `frame_seq`, `node_id`, `tool`, `args_hash` and `consumed_at IS NULL`; an interrupt frame gets a fresh monotonic `frame_seq`, so a parked approval matches nothing in it. |
| 7 | Nest interrupts three deep. | **Not reachable in this pack** - only `alpha` is interruptible and the check refuses an intent naming the current graph - so the self-critique's "arguably right and certainly untested" stands untested here too. |
| 8 | Gate lapses while a frame is parked. | **Held by ordering**, read rather than run: `_loop` sets `check_gates = True` on entry and `_failed_gate` runs over `frame.passed_gates` *before* the `frame.offer_return` branch, so a redirect is pushed before the offer is made. |
| 9 | Desk `resume` patch setting `identity_verified`. | **Held for the gate, broken for the frame.** The patch reaches frame state only; `ctx.customer.identity_verified` is built from `conversation.context` and no desk path writes it, so gates are unaffected. The write itself is P1. |
| 10 | Twenty spellings of the interrupt check's answer (JSON-as-string, literal `"null"`, `"None"`, an undeclared intent, empty `kind`, missing `kind`, `kind: null`, `new_intent(update_address)`, `new_intent: refund`, quoted and uppercase labels, `intent` as a dict or list, extra fields, out-of-range confidence, `"(cancel)"`). | **Held.** Eight coerced to the answer the model meant, twelve refused with a validation error. Nothing guessed: an undeclared intent validates but `StructuredInterruptCheck` degrades it to `unclear` and `resolve_intent` refuses it again in the engine. `new_intent` with no intent also degrades to `unclear`. |

### Crash and handoff attempts

| # | attempt | result |
|---|---------|--------|
| 11 | Kill the process at `claim_before_commit` on the interrupting turn (between the interrupt check deciding to push and the transaction that commits the push and the claim). | **Held.** The message stayed `pending`, a fresh executor re-drained it, no inbound message was left pending at the end, and `protected` never ran. |
| 12 | Kill at `before_node` (the push is committed; the first node of the interrupt frame has not run). | **Held.** Same outcome; the stack already contained the interrupt frame and re-entry ran its gate. |
| 13 | Kill at `after_checkpoint` (the interrupt frame's first checkpoint is committed). | **Held.** Same outcome. |
| 14 | Customer message on a run parked `waiting_human`. | **Held.** The trace was byte-identical before and after, the run did not move, and the message was left `pending` (exactly one row). See P6 for what happens to it afterwards. |
| 15 | Desk `resume` with a patch naming an undeclared field. | **Broken.** P1. |
| 16 | Desk `approve` replaying or creating an approval. | **Cannot create one.** `record_human_approval` copies conversation, run, frame, node, tool, args and hash from the customer's own row and refuses a template that is not bound to a run, a frame and a confirm node, so a desk cannot name an action. **Can sign the wrong one**: P4. It cannot replay a consumed approval, because `live_approvals` filters `consumed_at IS NULL` and `consume_approval` is a single-row `UPDATE ... FOR UPDATE SKIP LOCKED`. |
| 17 | `requires_human_approval` bypassed by closing and reopening. | **Held.** The gate is `consume_approval(..., approved_by=['human'])` in the tool runtime, not anything the desk's status field controls; closing a handoff row resolves a queue row and writes no approval. The reverse - `approve` still working on a resolved row - is P4. |
| 18 | Forge the phase-3 delimiter token through the packet's summary. | **Held.** `HandoffService._summary` goes through `assemble()` like every other call, so the transcript and the frame state are fenced data blocks under a per-render nonce from `nonce_for`, and a line carrying the nonce is neutralised. There is no path that concatenates untrusted text into the system prompt. |
| 19 | Sink failure during the handoff. | **Broken, quietly.** P2. |

### Commands run

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) - reproduces |
| `python -m ruff format --check .` | `160 files already formatted` (exit 0) - reproduces |
| `python -m mypy` (strict) | `Success: no issues found in 160 source files` - reproduces |
| `python -m pytest -q` (first attempt, run while `ruff`/`mypy` and the two demo servers were also on the box) | `60 failed, 1286 passed, 2 deselected, 6 errors in 388.53s`. Every failure was harness collateral, not product: `DeadlockDetectedError` on the fixture's `TRUNCATE ... RESTART IDENTITY CASCADE`, then `no conversation <uuid>` and `trace_step_run_id_fkey` violations in tests whose rows had been truncated underneath them. Two of the failures re-run alone pass in 3.21s. |
| `python -m pytest -q` (second attempt, quiet box, own database) | `1352 passed, 2 deselected in 340.37s` (exit 0). Green. The implementer reported `1351 passed`; the extra test is a count difference, not a failure. |
| `python -m pytest -q -m live` | `2 skipped, 1352 deselected in 0.85s` - skips on the missing key, does not fail. Reproduces |
| `python -m pytest tests/verify_phase_2_resolution.py -q` | `39 passed in 86.66s` - phase 2's proof harness still holds under phase 6's changes. Reproduces |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (18 warning(s))`, exit 0 - reproduces, and the 18 are the 16 `graph.assignment_optional`/`graph.state_type_unresolved` of phase 4's deferred R8 plus the 2 `graph.confirm_exempt` lines, which now name their call sites and arguments |
| `alembic downgrade base` / `upgrade head` / `check` against `support_test` | all eight revisions down and back up; `No new upgrade operations detected.` - reproduces |
| Twenty `InterruptCheck.model_validate` spellings | see attempt 10 |
| Ten scratch attack tests (`reviews/scratch-phase-6/`, deleted) | see the two tables above |

The first pytest run is worth a sentence rather than a finding against phase 6, because the
failure mode is the suite's fixture and not the code: the per-test `TRUNCATE` takes an
`AccessExclusiveLock` while another pooled connection still holds a `RowShareLock`, and under
load the two deadlock and take the rest of the file down with them. It is not phase 6's, it did
not exist as a *reported* problem before, and a reviewer who runs the suite while anything else
is happening will see it. Worth a backlog line under deferred findings so the next reviewer does
not spend twenty minutes on it as I did.

### Design conformance

**6.6, step by step.** Step 1: only `waiting_customer` suspensions are checked, which is `ask`
and `confirm` as written, plus core's own return-offer suspension, which is correctly excluded
from the check (`_interrupt` returns early on `INTERRUPT_RETURN_NODE`). Step 2: intents are
derived from the entry graph's start node's edges that lead to `subgraph` nodes -
`graph/routing.py`, one derivation shared with the validator, which is the right call. Step 3:
`continue` and `unclear` both resume, deliberately. Step 4: push, then offer on the way back.
Step 5: hint on the `ResumeEvent`, `deferral_notice` to the customer, `run.secondary_intents`
durable, surfaced only in the root frame and cleared on push and on root pop. The last sentence
of 6.6 holds under attack (attempts 1, 2, 8). Two deviations are declared and both are defensible:
`allowed_from` read as an allow-list, and the root frame never parked - the second makes
DESIGN.md 5.1's own example manifest (`allowed_from: [root]`) inert, which is worth a line in
5.1 rather than only in the decisions log.

**Section 13, field by field.** `reason`, `summary`, `identity_verified`, `customer`, `workflow`,
`node`, `state_snapshot`, `actions_taken`, `pending_action`, `suggested_next_steps`, `citations`,
`transcript_url` are all present with the design's types; `citations` is an honest empty seam for
phase 5. The additions (`conversation_id`, `run_id`, `step_id`, `queue`, `sla_minutes`, `detail`,
`frames`, `created_at`) all earn their place for a payload that leaves the process. `transcript_url`
resolves to a real endpoint, which is the difference between a citation and a plausible one. The
desk offers reply, resume-with-patch and close as 13 requires, plus `approve` and `transcript`.

**7.3.** LLM failure to `llm_unavailable`, tool failure to the node's `on_error` else handoff,
crash mid-node re-executed under the same step id, limits to `limit_exceeded` - all present and
all now reaching a packet with that reason rather than a bare status, which is the real
improvement of this phase. The middle tier is P7.

**Section 14.** The four core sentences are short, factual and keep what they promise. The sample
pack's new `no_workflow` message is backed by a real queue row - except when the sink fails, which
is P2, and which is the same class of defect as the wording this phase replaced.

### Forward compatibility

- **Phase 7 (replay).** `__interrupt_return__` is a trace node id no graph declares, with three
  core-only edge names. The phase-7 backlog item for `conversation_replay` does not mention it.
  P10.
- **Phase 7 (tracing).** The interrupt check and the handoff summary are model calls made outside
  any node, so section 15's `llm_call` span has no `node` parent for them. Both need a span of
  their own (`turn -> llm_call`) or the cost of the two most-often-run and most-expensive new
  calls in the system will not appear in the per-node attribution.
- **Phase 7 (email).** P6 is a web-chat annoyance and an email correctness bug: a run parked
  `waiting_human` after "a specialist will reply here" swallows the customer's reply.
- **Phase 7 (cost).** The interrupt check runs on every reply to a suspended workflow, inside the
  conversation lock, before the claim; the handoff summary uses the *escalation* model. DESIGN.md
  20 asks for the check on a cheaper model and there is no per-call model override because it is
  not a node. Worth a `llm.interrupt_model` before phase 7 measures anything.
- **Phase 5 (retrieval).** `build_packet` takes `citations` and `Passage` is defined, so the shape
  is ready - but `gather()` has no retriever seam and `HandoffSummaryRequest` carries no passages,
  so a grounded summary will need both threaded through `PacketRequest`. Naming that now would
  stop phase 5 inventing a second shape.

### Missed by self-critique

The self-critique is unusually good - it predicted the durability question I spent the most time
on, and it was right that the answer is "it holds". What it missed:

1. **P1.** `desk.py` states the opposite of what the code does ("a node error rather than a
   corrupted frame"), and the corrupted frame is unrecoverable through the API. The self-critique
   flagged `resume_human(close=True)` as the entry point to check and did not look at the patch on
   the `resume` side of the same method.
2. **P2.** The self-critique knows `HandoffService.failures` is per-process and that nothing
   sweeps it, and knows the pack now promises a specialist - but does not join the two into "the
   customer is told a thing that did not happen", which is the DESIGN.md 14 rule this phase's own
   pack change was written to satisfy.
3. **P3.** Confidence is plumbed through three layers and read by none. Not mentioned anywhere.
4. **P4.** `approve` is listed as a strength ("cannot name a different action", which is true) and
   the weaker property - that it cannot be pointed at a *particular* action either - is not
   examined. The mismatch with `build_packet`'s `unfinished[-1]` preference is a real one.
5. **P5.** The self-critique's fragility item 2 reasons carefully about a re-presented proposal
   being recomputed from current state, and stops one step short of what re-presenting *writes*:
   a second live approval row.
6. **The suite's fixture deadlock.** Reported as `1351 passed`; it is `1351 passed` on a quiet
   box and a heap of truncate deadlocks otherwise. Not phase 6's fault and not phase 6's to fix,
   but a phase that adds 92 DB-backed tests is the phase that will be blamed for it.

**Verdict: 1 must-fix, 6 should-fix, 3 nits.** The interrupt machinery and the frame stack are
sound and I could not get past a gate or an approval with them. Fix P1 before the desk is exposed
to anything, and P2 before the sample pack's promise is demonstrated to anyone.

## Resolution

Resolver: a fresh agent that wrote none of the phase-6 code, 2026-09-06 (PLAN.md step 5). The
must-fix is fixed with three regression tests that fail against the reviewed code; every
should-fix is fixed, none was deferred whole; both nits that were a few safe lines are fixed and
the third (P10) is a checklist line where it belongs. The review itself was committed first as
`7323080` (it was sitting uncommitted), and its own verification-numbers correction landed
alongside as `42c81e6`.

Two findings did not turn out to be quite what the review said, and both are recorded as what
they were rather than as what was reported: **P5** does not reproduce (a re-presented proposal
writes no approval at all - `ConfirmRunner` records one on a *yes*, and `ACME_INTERRUPT_SWITCH`
leaves exactly one row for `confirm_refund`), and the **suite deadlock** turned out to be a quick
safe fix rather than a backlog line.

| id | severity | action | commit |
|----|----------|--------|--------|
| P1 | must-fix | **Fixed.** `Executor._check_patch` validates a desk `resume` patch against the declared state model of the frame the run is suspended in, *before* anything is written, and refuses with the offending field names and the fields the graph declares. Two further rules it enforces: `identity_verified` is refused whatever a graph declares (DESIGN.md 10 gives it to `verify_identity` alone, through a tool - the rule `AppConfig` is already held to), and a patch is refused with 409 while an approval is live on that frame, because the proposal the customer agreed to was computed from the state it would edit and the desk's one route to an action is `approve`. `check_state_patch` is the same check without the lock, so the desk refuses before it writes the human's parting message into the transcript. Three regression tests in `tests/test_desk_api.py`; **all three fail against the reviewed code**, and the first is the reviewer's own repro, `{"identity_verified": true, "not_a_field": "x"}` - which left `frames[-1].state == {"not_a_field": "x", "identity_verified": true}` and a `pack_incompatible` handoff, and now leaves a 400 and a run that has not moved. The `desk.py` docstring that stated the opposite of what the code did now says what it does. | b660e1e |
| P2 | should-fix | **Fixed.** `HandoffHook` now answers whether a human was actually told: `HandoffService.deliver` returns it, `no_handoff` returns `False` (an engine with no desk configured has told nobody), and a `CompositeSink` partial failure counts as delivered, because a webhook down while the Postgres row was written is still a page a desk can find. When nothing took the packet, core **replaces** the pack's sentence with `HANDOFF_UNDELIVERED_MESSAGE`, which claims only what is still true - the run is parked, the conversation is saved, nobody has it yet - and promises nothing about when, because the thing that would have paged a person is the thing that failed. `test_a_queue_that_is_down_does_not_promise_a_specialist` drives the sample pack's whole account-question conversation with every sink refusing and asserts no queue row, `queued=False`, and no "billing specialist". The durable half - an `undelivered` row and a sweep - is deferred to phase 7 with a checklist line, which is where the reviewer put it. | 35f7382 |
| P3 | should-fix | **Fixed.** `pack.yaml`'s `llm.confidence_threshold` gates the interrupt decision, as it already gates an `llm` node's decision and a `confirm` node's yes. `cancel` and `new_intent` are the only two answers the engine acts on and both are destructive; below the threshold it does not act and does not pretend it understood - one core sentence, a hint on the `ResumeEvent` so a slot extractor does not read the reply as an answer, and the node that asked the question asks it again. Two parametrised tests. Every recorded interrupt-check confidence in the seven cassettes is 0.9 or higher against a threshold of 0.4, so no prompt changed and no cassette was regenerated. | 09d3f73 |
| P4 | should-fix | **Fixed.** `approve` resolves the approval *from the packet*: the handoff's `run_id`, and the confirm node, tool and arguments the packet's `pending_action` showed - all four - and only while the handoff is `open`. A packet that showed nothing, or showed an unfinished call, signs nothing and says to re-read the handoff. The existing test signed an approval written *after* the packet was built, which is the bug rather than the feature; it now writes the proposal first, which is the order the real `requires_human_approval` path has, and a second test covers the three ways the old endpoint could sign the wrong thing. | 754ff96 |
| P5 | should-fix | **Not reproduced as written; the invariant is enforced anyway.** A parked `confirm` re-presenting its proposal writes no `ActionApproval`: `ConfirmRunner` records one on a *yes*, not when it presents, and `ACME_INTERRUPT_SWITCH` leaves exactly one row for `confirm_refund`, not two. The shape behind the finding is real - a confirm re-entered in one frame and answered yes twice would leave two live rows for one run, frame, node, tool and hash - so `record_approval` now supersedes the frame's earlier live approvals for that node in the same statement batch as the insert, and therefore in the same checkpoint transaction. Scoped by `approved_by`, so the human half of a `requires_human_approval` pair does not cancel the customer half it countersigns. The superseded row is kept with `consumed_by_tool_call_id` NULL: spent by nothing, which is what superseded means. | 2286347 |
| P6 | should-fix | **Fixed, both halves the reviewer named.** The refusal stands - a customer message on a run parked `waiting_human` still stays `pending`, because a topic change must not smuggle a workflow past the person it was escalated to - but the ending changed. The desk's handoff view now carries `waiting_messages`, everything the customer has said since the handoff was raised (`repo.pending_inbound`), and the engine says one sentence, once per parking, recorded on `run.awaiting`. `test_a_customer_message_on_a_parked_run_is_answered_and_shown_to_the_desk` sends two messages and asserts one acknowledgement, two rows on the desk, and both still queued. The SLA half - counting a queued message against the handoff's deadline - is deferred to phase 7 as a nit, since phase 7 owns the metrics and the scheduler. | 754ff96 |
| P7 | should-fix | **Fixed by the reviewer's second option: DESIGN.md 7.3 is amended to two tiers.** "Otherwise the frame's `on_error` graph" is removed, with the amendment and its reasons inline in the design and in the decisions log. Three phases running recorded it as unimplemented and argued nothing needed it; the reviewer's point was that the argument had to end. Graphs have no `on_error` key, no error `FrameKind` exists, nothing the engine can raise at frame level is beyond a node-level edge, and the fall-through now reaches a real packet carrying the failure's own reason rather than a bare status. Adding the tier later is additive and needs a use case first. `_route_error`'s docstring says two tiers rather than apologising for one. | b8e2c8e |
| P8 | nit | **Fixed**, without a field on the frame. The label is derived at offer time from the root graph's own declared edges (`Executor._label_for`, over the same `workflow_intents` the check and the validator share), so "shall we go back to *update address*?" reads from the pack's vocabulary, and it falls back to the graph id for a parked frame the root declares no edge to. No stored state, no migration. | 754ff96 |
| P9 | nit | **Fixed.** `record_human_approval`'s `ValueError` for an unbound template approval is a 409 carrying its own message, not a 500. | 754ff96 |
| P10 | nit | **Fixed where it belongs**: a phase-7 checklist line on `conversation_replay` naming `__interrupt_return__` and its three core-only edges, so replay handles the reserved id rather than discovering it. | b8e2c8e |
| the fixture deadlock | (reviewer asked for a backlog line) | **Fixed instead - it was a few safe lines.** The per-test `TRUNCATE ... RESTART IDENTITY CASCADE` takes an `AccessExclusiveLock` on every mapped table, so anything holding a `RowShareLock` on some of them deadlocks it and the rest of the file fails behind it. `tests/conftest.py` now sets `lock_timeout` and retries on `deadlock_detected`/`lock_not_available`, which is the documented remedy; verified two ways against a held reader - waiting 1.66 s behind a live transaction and then succeeding, and retrying past a forced 300 ms `lock_timeout` and then succeeding. The *other* half - another process truncating rows out from under a running test - no retry can fix, and it is recorded against phase-0 finding N9, which was deferred for exactly the session-long lock that would. | b8e2c8e |

### Deferred, each with a checklist line in the owning phase

- **[phase 7] P2's durable half.** A handoff no sink would take is visible only in
  `HandoffService.failures`, a per-process list nothing sweeps. The customer-facing half is
  closed; a durable `handoff.status = 'undelivered'` row and a retry need the scheduler phase 7
  owns.
- **[phase 7] P6's SLA half.** A queued customer message is acknowledged and shown, but nothing
  counts it against the handoff's deadline or re-prioritises the queue.
- **[phase 7] Spans, and a cheaper model, for the two new out-of-node model calls.** The
  interrupt check and the handoff summary have no `node` parent for section 15's `llm_call` span,
  and the check has no `llm.interrupt_model` to run on because the per-node `model:` override
  cannot reach it. Both from the reviewer's forward-compatibility section.
- **[phase 5] A retriever seam through `gather()` and `HandoffSummaryRequest`**, so
  `HandoffPacket.citations` stops being an empty shape - named now so phase 5 does not invent a
  second one.

### The reviewer's two attempt matrices, re-run

Re-run as the review's logs describe them, against `tests/packs/interrupt_pack` and the sample
pack, with the real executor and real Postgres. Scratch tests again, deleted again: sixteen of
the nineteen as a scratch file (`14 passed`, three attempts sharing a test), and attempts 10, 16
and 18 through the committed tests that already *are* those matrices
(`tests/test_interrupt_check_schema.py`, `tests/test_desk_api.py`,
`tests/test_prompt_injection_matrix.py`: `457 passed` together).

| | before | after |
|---|---|---|
| Interrupt bypass (attempts 1-10) | 9 held, 1 broken (#9's frame write) | **10 held** |
| Crash and handoff (attempts 11-19) | 7 held, 2 broken (#15, #19) | **9 held** |

**Nothing that held before fails now**, and in particular no interrupt reaches a guarded action:
attempt 1 still ends `root/alpha/gated/verify` with `protected` absent from the trace; attempt 2
still defers `gated` twice from inside the gate's own redirect and never reaches `protected`;
attempts 11 to 13 kill at `claim_before_commit`, `before_node` and `after_checkpoint` and each
re-drains with `protected` absent and no inbound message left pending; and attempt 5's
`ACME_INTERRUPT_SWITCH` still authorises exactly one refund with exactly one approval, with
nothing left live afterwards.

Three attempts changed outcome for the better and two changed shape:

- **#9** (a desk patch setting `identity_verified`) was "held for the gate, broken for the
  frame"; it is now refused outright, by name, and the frame is untouched.
- **#15** (a desk patch naming an undeclared field) was **broken**; it is now a 400 naming the
  field and the fields that exist.
- **#19** (sink failure) was **broken, quietly**; it is now loud - `queued=False`, no queue row,
  and a customer sentence that promises nothing.
- **#14** (a customer message on a run parked `waiting_human`) held and still holds - the trace
  is byte-identical and the message is still exactly one `pending` row - but the customer is now
  told so once, which is the behaviour change P6 asked for.
- **#16** (desk `approve`) could sign the wrong action; it can now sign only the one the packet
  showed, on an open handoff, on that run.

Attempt 7 (nesting three deep) is still not reachable in `interrupt_pack`, so the self-critique's
"arguably right and certainly untested" stands untested, as it did for the reviewer.

### Commands run after the fixes

From the repository root with `.venv/Scripts/python.exe`; Postgres 16 in
`customer-support-agent-db-1`, database `support_test`; no `ANTHROPIC_API_KEY` and no GLM key.
The two demo servers on ports 8000 and 8001 were left running throughout and neither port was
bound by anything here.

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) |
| `python -m ruff format --check .` | `160 files already formatted` (exit 0) |
| `python -m mypy` (strict) | `Success: no issues found in 160 source files` |
| `python -m pytest -q` | `1361 passed, 2 deselected in 502.28s` (exit 0) |
| `python -m pytest -q -m live` | `2 skipped, 1361 deselected in 1.18s` - skips on the missing key, does not fail |
| `python -m pytest tests/verify_phase_2_resolution.py -q` | `39 passed in 128.44s` - phase 2's proof harness still holds |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (18 warning(s))`, exit 0 - the same 18 as before |
| `alembic downgrade base`, `upgrade head`, `check` (`support_test`) | all eight revisions down and back up cleanly; `No new upgrade operations detected.` |
| the two attempt matrices | see above |

The 1361 are the reviewer's 1352 plus 9: three desk-patch refusals, one `approve` scoping, one
queued-message acknowledgement, two interrupt-confidence cases, one queue-that-is-down, and one
approval supersede. No cassette was regenerated, because no prompt changed: the interrupt
threshold reads a value the recordings already carry, and every core sentence this resolution
added or replaced is written by the engine rather than by a model.
