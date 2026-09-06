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
     `resume_interrupted` hook whose default is a keyword reading and whose real implementation is
     a structured call. `unclear` re-offers; `no` abandons the parked frame by popping it.
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
   edge from what the desk did.

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
