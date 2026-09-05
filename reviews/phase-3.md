# Phase 3 review: LLM layer and prompted nodes

Design references: DESIGN.md sections 3 (principles 2 and 7), 6.2 (`llm`, `ask`), 6.4, 10
(memory), 11.1 to 11.3, 14 (the seams guardrails will need). Backlog: BACKLOG.md "Phase 3".
Inherits the settled decisions in reviews/phase-0.md, reviews/phase-1.md and reviews/phase-2.md.

## Plan

Written before any code, per PLAN.md step 1.

### What this phase is really about

Three of the five things below are safety surfaces rather than features, and they are ordered
that way deliberately:

1. **Prompt assembly is a compliance surface.** DESIGN.md 11.2 fixes nine layers in a fixed
   order and says packs can neither remove nor reorder them. That has to be *structural*: a
   pack must not be able to write into layer 1, to reorder anything, or to escape its own
   layer, and retrieved passages, tool results, state values and customer messages must land
   in delimited data blocks that are never instructions (principle 7, section 14).
2. **The `llm` node's decision is constrained by the graph, not by the prompt** (principle 2).
   An undeclared edge, malformed output, or output failing the node's `output_schema` is
   retried once and then handed off. It is never guessed at, and low confidence routes to
   `unclear` rather than to the model's best guess (11.3).
3. **The tool loop inside an `llm` node cannot become a way to act.** DESIGN.md 8.2 says WRITE
   and HIGH are *never* callable from a model loop. Phase 4 owns tool execution, so phase 3
   leaves a seam - but the seam has to force the policy rather than offer it.

### Task breakdown

1. **`support_core/llm/types.py`** - the wire types: `PromptMessage`, `CompletionRequest`
   (system blocks, messages, model-facing tool specs, cache breakpoint), `CompletionResponse`
   (text, tool calls, usage, stop reason), `Usage`, and the error hierarchy
   (`LLMError`, `LLMUnavailableError`, `StructuredOutputError`). `CompletionRequest.fingerprint()`
   is the canonical sha256 the fake provider keys on and the trace records.

2. **`support_core/llm/provider.py`** - `LLMProvider` protocol exactly as DESIGN.md 11.1 gives
   it (`complete`, `structured`).

3. **`support_core/llm/prompt.py`** - the nine layers of DESIGN.md 11.2 as an `IntEnum` and one
   assembler that walks it in order. Enforcement, concretely:
   - Layer 1 is a module constant. The assembler takes no argument that can reach it, so there
     is no code path by which pack data becomes core text.
   - A pack supplies *content for a named slot*, never a layer object and never an order, so
     "cannot reorder" is a property of the type signature rather than of a check.
   - Every pack-authored layer is fenced with core-written section markers, and pack text is
     sanitised so it cannot forge a marker: a line that looks like a section header or a data
     fence is neutralised before it is placed.
   - Untrusted content - retrieved passages, tool results, state values, the conversation
     transcript - goes inside `-----BEGIN UNTRUSTED DATA ...-----` fences with the same
     sanitisation, so a customer message containing a fence line cannot close its own block.
   - Per-layer token budgets (DESIGN.md 10, "explicit token budgets so long email threads do
     not blow up context"). Layers 1 to 5 are the contract and are *not* silently truncated:
     over budget is an error that becomes a handoff. Layers 6 to 9 are data and are truncated
     oldest-first with a visible marker inside the block.

4. **`support_core/llm/schemas.py`** - `LlmNodeOutput` from DESIGN.md 11.3, plus the per-node
   model builder: `decision` is a `Literal[...]` over exactly the node's declared edge labels
   and `state_updates` is validated against the node's `output_schema` (built with phase 1's
   `build_model`). The constraint is in the schema the provider is given *and* re-checked on
   the way back, because a provider that ignores the schema must not get a free pass.

5. **`support_core/llm/fake.py` and `recording.py`** - the fake provider and the recording
   format:
   - A `Cassette` is a JSON file: `{version, model, interactions: [{key, request, response}]}`,
     where `key` is `CompletionRequest.fingerprint()` and `request` is the whole canonical
     request. Storing the request beside the key is what lets a machine that *has* a key
     regenerate the file against the live API without a test changing: the generator replays
     the recorded requests (or re-drives the scenarios) and rewrites `response` and `key`.
   - `FakeProvider(cassette)` replays by fingerprint and raises a `CassetteMiss` naming the
     fingerprint and dumping the request on a miss, so a prompt change is a loud, actionable
     failure rather than a mysterious one.
   - `ScriptedProvider` answers from a small rule table keyed on the node id, for tests whose
     subject is engine behaviour rather than replay fidelity.
   - `RecordingProvider(inner)` wraps any provider and writes a cassette. The committed
     cassettes are produced by `tests/cassettes/build_cassettes.py`, which drives the scenarios
     through `RecordingProvider(ScriptedProvider(...))` offline and through
     `RecordingProvider(AnthropicProvider(...))` with `--live`. A test asserts the committed
     cassettes are what the builder produces, so drift is caught at the point it happens.

6. **`support_core/llm/anthropic_provider.py`** - `AnthropicProvider` on the Anthropic SDK,
   using tool-use for structured output (a single forced tool whose `input_schema` is the
   Pydantic model's JSON schema, `strict: true`) and `cache_control` on the last static system
   block, which is the layer 1 to 3 prefix DESIGN.md 11.2 says is identical across turns.
   It cannot be exercised here: there is no `ANTHROPIC_API_KEY` in this environment. Tests for
   it are unit tests over request *construction* plus an opt-in `live` group that skips
   cleanly.

7. **`support_core/llm/service.py`** - `LlmService`: assemble, call, validate, retry. It owns
   DESIGN.md 7.3's LLM failure ladder (retry with backoff, then the pack's
   `escalation_model`, then `llm_unavailable`) and 11.3's decision handling.

8. **`support_core/llm/tool_loop.py`** - the bounded READ-only loop of DESIGN.md 8.4 behind a
   gateway that phase 4 cannot opt out of. `ModelToolRunner` is the injectable seam (describe,
   invoke); `ReadOnlyToolGateway` wraps *every* runner and refuses a call that is not in the
   node's declared `tools`, is not READ tier, or exceeds the iteration bound. The default
   runner raises "tool execution arrives in phase 4", so nothing executes today.

9. **`support_core/memory/summary.py`** - the rolling conversation summary of DESIGN.md 10,
   updated every K turns. Durability, learning from phase 2's two must-fix bugs: the turn
   counter and the summary are **columns**, written in transactions the engine already has;
   nothing a turn depends on is read from the summary. The summary is prompt context in layer 9
   and nothing else, which is testable: deleting it between turns must not change a single
   durable outcome.

10. **Engine wiring** - `LlmRunner` in `runners.py` (so `llm` becomes executable, phase 3);
    `NodeRuntime` gains `llm` and `tools`; `EngineHooks` gains `summarize` with a default that
    does nothing; the executor counts turns and calls the summariser after a turn settles.
    `ask` keeps the phase-2 node and gets a real `extract_slots` implementation - the hook is
    replaced, not the node, as reviews/phase-2.md requires. The hook's signature grows into a
    request object because structured extraction needs the slots' declared *types*, which the
    name-only signature cannot carry.

11. **Migration `0004`** - `conversation.turn_count` and `conversation.summary_turn`.

12. **Manifest** - `llm.confidence_threshold`, `llm.max_tool_iterations`, and a `memory:` block
    (`summarize_every_turns`, budgets). Additions in the spirit of phase 2's `timeouts:`.

13. **`packs/acme_billing/graphs/root.yaml`** - the exit criterion: a classify `llm` node with a
    `small_talk` path, and a scripted conversation that runs through it against the fake
    provider.

14. **Tests** - injection attempts from persona, policies, node instructions, state values and
    conversation history; undeclared edge, malformed output, schema-violating output, low
    confidence, `needs_handoff`; a WRITE tool refused from the model loop; slot extraction;
    summary memory including the durability property; cassette replay and drift.

### Intended deviations from DESIGN.md and why

- **The `SlotExtractor` hook signature changes** from `(slots, reply, ctx)` to a request object.
  DESIGN.md 6.2 says slots are extracted "via structured output", and a structured schema needs
  the declared type of each slot, which the phase-2 signature cannot express. The node is
  unchanged, which is what reviews/phase-2.md asked for.
- **`pack.yaml` gains `memory:` and two `llm:` keys.** DESIGN.md 5.1 shows the manifest in full
  and has nowhere to put 10's "every K turns" or 11.3's "pack threshold". Same precedent, and
  same argument, as phase 2's `timeouts:`.
- **Layers 1 to 5 fail closed rather than truncating.** DESIGN.md 10 asks for explicit budgets
  but does not say what happens at the limit. Truncating a policy line silently is exactly the
  failure a compliance surface must not have, so an over-budget policy or persona is a node
  error that hands off. Data layers truncate, visibly.
- **State (layer 6) is rendered inside a data block**, not as bare YAML. DESIGN.md 11.2 calls
  layer 6 a "state summary" and layers 7 and 8 the delimited ones, but state fields hold
  customer-supplied text (an `ask` node writes the customer's own words into a slot), so under
  principle 7 they are data. Delimiting them costs nothing and closes the obvious hole.
- **`llm` node knowledge queries are assembled but retrieval is phase 5.** The knowledge layer
  is built from whatever the (absent) retriever returns, which today is nothing; the layer and
  its fences exist so phase 5 fills a shape rather than inventing one.
- **No guardrails.** DESIGN.md 14's inbound injection flag would lower the allowed decision
  confidence and disable the tool loop for a turn. Phase 7 owns guardrails; the two places it
  attaches are named in the code rather than stubbed.
- **`packs/acme_billing` stops being "empty but well-formed."** The phase 3 exit criterion
  requires a `root.yaml` there, so the three phase-0 tests asserting the empty-pack wording are
  updated to assert the pack now validates *with* graphs. The phase-0 exit criterion's wording
  in BACKLOG.md is annotated rather than rewritten.
- **The AnthropicProvider is unexercised.** No API key exists in this environment. Everything
  else in the phase runs against the fake provider, and the self-critique says plainly what
  that leaves unproven.

## Implementation notes

_Written after the code; see below._

## Self-critique

_Written after the code; see below._
