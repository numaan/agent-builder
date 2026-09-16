# Composable Reasoning Strategies: ReAct, Reflection, Reflexion

Status: Design note, 2026-09-16
Author: Numaan (with Claude)
Scope: How a pack picks a reasoning strategy at build time. Interfaces and graph shapes.
Implementation is deferred; nothing here is built yet.

Relates to [DESIGN.md](../DESIGN.md) sections 5 (pack anatomy), 6 (graph model), 7 (execution),
10 (memory), 11 (LLM layer), 14 (guardrails), 16 (evals).

---

## 1. What this is for

The ask: build an agent whose *reasoning strategy* is a build-time choice. The same task should
be runnable as a ReAct agent, a Reflection agent, or a Reflexion agent, chosen when the pack is
composed rather than baked into core.

This note says how. The short version: a strategy is not a new engine. It is a shape of graph
over the engine that already exists. So the composition lever is the one the project already has
(DESIGN.md Q1, Q5) — core ships the strategy as a reusable sub-graph, and a pack references it.

## 2. The three strategies, in one paragraph each

**ReAct.** Reason, then act, then observe, then repeat until the agent can answer. The agent
interleaves a thought with a tool call and reads the result before the next thought. There is no
separate self-critique step; the loop ends when the agent decides it has enough.

**Reflection.** The agent produces a draft answer, a second step critiques that draft, and the
agent revises. The critique is generated from the draft alone — no outside feedback, no memory
that outlives the attempt. It is a generate-critique-revise loop with a small iteration cap.

**Reflexion.** Reflection plus memory. After an attempt, the agent writes a short verbal
self-critique ("I searched the wrong table; next time filter by status first") to durable memory,
and reads it back on the next attempt. The improvement is carried across tries, not just within
one. Reflexion is the only one of the three that needs a place to keep a note between attempts.

## 3. The core claim: strategies are graph shapes

Each strategy maps onto the node vocabulary in DESIGN.md 6.2 with no new engine primitives:

| Strategy | Shape | New pieces core must add |
|---|---|---|
| ReAct | one `llm` node running its bounded read-tool loop, then a decision edge | none — this is DESIGN.md 6.2's `llm` node as written |
| Reflection | `llm` (draft) → `critic` → `router` (good enough?) → loop back to a revise `llm`, capped | a `critic` node type; a bounded-loop count |
| Reflexion | Reflection, plus a `reflect_memory` write after a failed attempt and a read before the next | a `reflect_memory` node (write + read), on top of Reflection |

Because they are graph shapes, the validator (DESIGN.md 5.2) still checks them like any other
graph, and every safety rule below still applies whichever strategy a pack picks.

## 4. How a pack selects a strategy at build time

Two ways, and the recommendation is the second.

**4a. By sub-graph reference (recommended).** Core ships three strategy sub-graphs —
`strategy/react`, `strategy/reflection`, `strategy/reflexion` — each with typed `inputs` and
`outputs` (DESIGN.md 6.1). A pack's task graph invokes one with a `subgraph` node:

```yaml
# packs/acme_billing/graphs/answer_policy_question.yaml
nodes:
  reason:
    type: subgraph
    graph: strategy/reflection        # swap to strategy/react or strategy/reflexion
    inputs:
      goal: state.question
      tools: [search_policies, list_recent_charges]
      max_iterations: 2
    outputs:
      answer: state.answer
    next: deliver
```

Changing one line changes the strategy. The task graph around it — its gates, its confirms, its
handoff edges — does not move. This keeps strategy selection inside the graph, where the validator
and the trace already see it, and it reuses DESIGN.md Q5's "sub-graphs are first-class" decision
rather than inventing a parallel mechanism.

**4b. By a manifest field (rejected as the primary path).** A `pack.yaml` key like
`reasoning: reflexion` would be terser, but it makes the strategy invisible in the graph the
validator checks and the replay renders, and it forces one strategy per pack when different tasks
in one pack want different ones (a quick FAQ lookup wants ReAct; a refund dispute wants Reflexion).
A manifest default that a graph can override is a reasonable convenience later; it is not the
mechanism.

## 5. The decision that matters: where the loop lives

A reasoning loop can live *inside* one node or *across* several. The choice is not cosmetic.

**Inside a node.** The `llm` node's read-tool loop already works this way (DESIGN.md 6.2). It is
cheap — one checkpoint for the whole node — and it is the natural home for ReAct, whose loop is
short and read-only. Its cost: the iterations are opaque. The frame stack shows one node; the
replay (DESIGN.md 15) cannot show the third thought that changed the answer, because the node
committed once.

**Across nodes.** Draft, critique, and revise are each their own node, joined by loop edges. It is
more verbose, and each pass is a checkpoint. Its gain: every reasoning step is durable, auditable,
and replayable — the same property the whole engine is built for (DESIGN.md 7.1, 15). A crash
mid-reflection resumes at the exact step. A reviewer asking "why did the agent change its mind"
reads it in the trace instead of guessing.

**Recommendation.** ReAct stays inside a node (its loop is bounded and read-only, and paying a
checkpoint per thought buys little). Reflection and Reflexion go across nodes, because their whole
value is a second judgement about the first answer, and a judgement nobody can see in the trace is
a judgement nobody can audit. This matches the project's bias throughout: durability and replay
over cleverness that hides.

## 6. The safety boundary, stated plainly

A strategy shapes how the agent *thinks*. It does not relax how the agent *acts*. Every rule in
DESIGN.md 8 and 14 holds whichever strategy wraps the work:

- A WRITE or HIGH tool still runs only from a `tool` node behind a `confirm` and a single-use
  `ActionApproval` (DESIGN.md 8.2, Q6). A reflection loop may revise a *plan* to issue a refund,
  but the refund itself leaves the loop and passes the gate — and because approvals are single-use
  and bound to the argument hash, a revised amount is a new action needing its own approval, not a
  re-run of the old one.
- The validator's confirm-on-every-write-path rule (DESIGN.md 5.2) is checked on the fully
  composed graph, strategy sub-graph included. A strategy cannot introduce a path to a write that
  skips a confirm; such a graph does not load.
- The outbound citation guardrail (DESIGN.md 14, section 9.2) still runs on whatever the strategy
  finally emits. Reflexion improving an answer does not exempt that answer from needing a citation.

This is the line between "the agent got smarter about what to do" and "the agent did something
twice, or did something nobody approved." Reflection and Reflexion move the first; they must not
touch the second. That boundary is why these are worth building on this engine rather than on an
open-ended tool-calling loop: the strategy raises answer quality, and the gates keep the raised
quality from turning into raised blast radius.

## 7. The three sub-graphs in more detail

### 7.1 `strategy/react`

Inputs: `goal: str`, `tools: list[str]` (read-tier only), `max_iterations: int`.
Output: `answer: str`, `citations: list[str]`.

One `llm` node with the bounded read-tool loop of DESIGN.md 6.2, capped by `max_iterations`
(reusing the existing `max_tool_iterations` budget). It reasons, calls read tools, observes, and
repeats until it emits an answer or hits the cap; at the cap it routes to handoff, the same
fail-closed rule the confidence threshold uses (DESIGN.md 11.3). Nearly nothing new — this is the
`llm` node as already designed, packaged as a referenceable sub-graph.

### 7.2 `strategy/reflection`

Inputs: `goal`, `tools`, `max_iterations` (the revise cap, e.g. 2).
Output: `answer`, `citations`.

```
draft (llm)  ->  critic (critic)  ->  enough? (router)
                                        |-- yes --> emit (end)
                                        |-- no  --> revise (llm) --> critic   (loop, capped)
```

The `critic` node is a new core node type: an `llm` node specialised to score a draft against the
goal and the retrieved passages, returning a structured verdict (`good_enough: bool`, `problems:
list[str]`). It writes nothing to customer-facing state; its output feeds the router and the next
revise. The loop is capped by `max_iterations` on the frame, counted like DESIGN.md 6.6's
`max_node_errors` so it survives a crash rather than resetting. At the cap the current best draft
is emitted (with its citations) or, if still failing the critic, routed to handoff.

Note: the existing reject-correct-retry ladder behind the citation guardrail (section 9.2, phases
3 and 5) is already a narrow, rule-based Reflection over one dimension. `strategy/reflection`
generalises it from "one rule re-prompts once" to "an LLM critic re-prompts up to N times", and
should reuse that ladder's machinery rather than build a second loop beside it.

### 7.3 `strategy/reflexion`

Inputs: `goal`, `tools`, `max_iterations`, plus a `memory_key` for where reflections live.
Output: `answer`, `citations`.

`strategy/reflection` with two additions:

- **Read before**: a first node reads prior reflections for this `memory_key` from durable memory
  and puts them in the draft node's prompt, so a past failure informs this attempt.
- **Write after a bad attempt**: when the critic rejects a draft, a `reflect_memory` node writes a
  short verbal self-critique to durable memory before the revise, so the lesson outlives the turn.

The store is DESIGN.md 10's customer-memory layer, and the write goes through the WRITE-tier
internal memory tool (DESIGN.md 10) — so a reflection write is risk-policed and traced like any
other side effect, and nothing written here is ever treated as verified identity. Reflexion needs
no new storage; it needs a disciplined use of the memory layer that already exists, plus the rule
that a reflection is content, not instruction: it is fenced and delimited in the prompt like every
other untrusted string (phase 3's boundary), because a stored reflection is text the model wrote
and could be steered into writing.

## 8. What exists and what to build

**Exists** (reusable as-is): the `llm` node and its read-tool loop (ReAct's core); first-class
sub-graphs with typed inputs/outputs (the composition surface); the frame stack and per-node
checkpointing (across-node loops); the memory layers (Reflexion's store); the reject-correct-retry
ladder (narrow Reflection); the trace and replay (auditing the loop); per-frame counters that
survive a crash (bounding the loop); the validator (checking the composed graph).

**To build**:

1. A `critic` node type — an `llm` node specialised to score a draft, returning a structured
   verdict. Registered in core's node vocabulary, not per pack.
2. A `reflect_memory` node type — write a verbal reflection through the WRITE-tier memory tool,
   and a read variant that surfaces prior reflections into a prompt.
3. A clean bounded-loop construct for reflection iterations — expressible today via router + a
   frame counter, but worth a first-class `max_iterations` on the strategy sub-graph so the cap
   is one declared number, not a hand-wired counter each pack re-derives.
4. The three sub-graphs of section 7, shipped in core and validated in CI like any pack graph.
5. Node evals (DESIGN.md 16.1) for the `critic`: does it reject a draft that misses the goal, and
   accept one that meets it. A critic that always says "good enough" turns Reflection back into a
   single pass and nobody notices without a test.

## 9. Open questions

- **Cost.** Reflection and Reflexion multiply model calls per turn (a draft, a critique, one or
  more revisions). DESIGN.md 20's p95 latency budget and per-conversation cost cap (phase 9) both
  bite harder here. A cheaper critic model (the per-node `model:` override already exists) is the
  obvious lever; the strategy should default the critic to the cheap model, not the reasoning one.
- **When Reflexion's memory is wrong.** A stored reflection can be mistaken ("always skip identity
  for this customer") and then it steers every future attempt the wrong way. Reflections need a
  scope (per task, not per customer, at least to start) and probably an expiry, and they must
  never be able to set anything the gates read — the same rule customer memory already has about
  `identity_verified`.
- **Does strategy belong to the graph or the node?** This note puts it at the sub-graph. An
  alternative is a per-`llm`-node attribute (`strategy: reflection`) that wraps a single node's
  own output. That is terser for a one-node task and worse for a multi-node one; the sub-graph is
  the more honest unit, but the node attribute may be worth it as sugar later.
- **Interaction with interrupts.** A customer who changes the subject mid-reflection (DESIGN.md
  6.6) parks a frame that is halfway through a critique loop. The frame stack already handles this,
  but the resume-and-return prompt should probably summarise "you were refining an answer to X"
  rather than drop the customer back into a bare revise node. Worth an eval.

## 10. Recommendation

Build strategies as sub-graphs, composed by reference at build time (section 4a), with the loops
across nodes for Reflection and Reflexion and inside the node for ReAct (section 5). Add the two
node types and the bounded-loop knob (section 8), ship the three sub-graphs in core, and gate them
behind the same validator and the same write-approval rules as everything else (section 6). The
result is a pack author choosing a reasoning strategy the way they already choose a workflow — one
line in a graph — without core learning anything about the pack, and without any strategy buying
its way past a gate.
