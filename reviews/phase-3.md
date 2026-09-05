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

Environment: Windows 11, Python 3.13.14, `anthropic` 1.4.0, pydantic 2.13.5, SQLAlchemy 2.0.52,
Postgres 16.15 in `customer-support-agent-db-1`, database `support_test`. **`ANTHROPIC_API_KEY` is
unset**, and everything below follows from that.

### Shape of the code

```
support_core/llm/
  types.py              CompletionRequest/Response, Usage, the error hierarchy, fingerprint (230)
  provider.py           the section 11.1 protocol, plus `structured` implemented once (95)
  prompt.py             the nine layers, the fences, the budgets (400)
  schemas.py            LlmNodeOutput, the per-node decision model, slot and summary models (180)
  service.py            assemble -> call -> validate -> retry; section 7.3's ladder (440)
  tool_loop.py          the phase-4 seam and the gateway it cannot escape (160)
  anthropic_provider.py tool-use structured output, cache_control, error mapping (190)
  fake.py               FakeProvider (hash replay), ScriptedProvider, FailingProvider (150)
  recording.py          the cassette format and RecordingProvider (180)
  wiring.py             service_for_pack, StructuredSlotExtractor (95)
support_core/memory/summary.py   LlmSummarizer (55)
support_core/engine/
  runners.py            + LlmRunner, + NodeRuntime.tool_gateway, ask uses the new hook
  executor.py           + llm/tool_runner wiring, + turn counting, + _maybe_summarize
  hooks.py              + SlotRequest, + Summarizer
  errors.py             NodeError carries the handoff reason
storage/migrations/versions/0004_conversation_memory.py
packs/acme_billing/graphs/root.yaml, persona.md, policies.md
tests/  test_prompt_assembly.py (46), test_llm_node.py (19), test_llm_provider.py (21+1 live),
        test_tool_loop.py (10), test_ask_slots.py (6), test_memory_summary.py (6),
        test_golden_conversation.py (4), packs/llm_pack, cassettes/
```

### Where the plan changed while building

- **The cassette generator is a module, not a pytest fixture.** `python -m
  tests.cassettes.build_cassettes` records; `--live` records the same scenarios against the real
  API. A fourth golden test re-records offline and requires the committed file back byte for
  byte, so drift is caught where it happens rather than as a mysterious miss three phases later.
- **`NodeError` grew a `reason`.** Phase 2 recorded every routed failure as `node_error`.
  DESIGN.md 7.3 names `llm_unavailable` as a distinct outcome, and "the model answered with
  something the graph does not allow" and "the model was not sure enough" are two more. Whoever
  picks the conversation up needs to know which happened, so the node says and the executor
  records it.
- **The `ask` node's slot check tightened** from "a declared state field" to "a declared *slot*".
  Phase 2's extractor could only ever fill the first slot, so the wider check cost nothing; a
  structured extractor that writes `outcome` because it seemed helpful is a different matter.
- **The `crash` node type** in `tests/engine_support.py` replaces "a pack that reaches an `llm`
  node" as the unroutable-failure fixture for the phase-2 recovery-sweep test, because `llm`
  nodes now run. It fails with a plain `RuntimeError`, which is what finding R3 was actually
  about and stays true after phase 6.
- **`_begin_turn` became one transaction.** Counting the turn and marking the run running were
  two writes; a crash between them would count a turn that never started and drift "every K
  turns". Same shape as phase 2's R1 fix, found by writing the durability test.

### Things worth knowing that came up while building

- **The state layer needed two passes of neutralisation.** YAML re-indents a multi-line string,
  so a fence line inside a state value can come back at the start of a line *after* dumping.
  Values are defused before the dump and the dump is defused again after it. The prompt tests
  found this, not review.
- **A neutralised line no longer matches the reserved pattern at all**, because the marker is no
  longer at the start of the line. That is what makes the test assertion clean: every line of a
  rendered prompt that reads as structure is one core wrote, full stop.
- **The tool budget must not count its own refusals once it is spent**, or a model that keeps
  asking grows `calls` without bound. Caught by the gateway tests.
- **`structured` is implemented once, over `complete`.** It is what lets `RecordingProvider`
  wrap any provider by wrapping one method, and it means the schema is validated on the way back
  for the fakes too - so a recorded fixture cannot contain something the real contract rejects.
- **The scripted provider is not the fake provider.** Two different jobs: `ScriptedProvider`
  controls the answer (for tests about the engine), `FakeProvider` replays a hash (for tests
  about the prompt). Mixing them would have made the golden conversation prove less than it does.

### Commands run at the end of the phase

From the repository root with `.venv/Scripts/python.exe`; Postgres 16 in
`customer-support-agent-db-1`, database `support_test`, `ANTHROPIC_API_KEY` unset.

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) |
| `python -m ruff format --check .` | `102 files already formatted` (exit 0) |
| `python -m mypy` (strict) | `Success: no issues found in 102 source files` |
| `python -m pytest -q` | `590 passed, 1 deselected in 187.89s` |
| `python -m pytest -q -m live` | `1 skipped, 590 deselected` - the live test skips on the missing key rather than failing |
| `python -m pytest tests/verify_phase_2_resolution.py -q` | `39 passed in 118.17s` (phase 2's proof harness still holds) |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (4 warning(s))`, exit 0 |
| `python -m alembic downgrade base` then `upgrade head` | all four revisions apply from base and roll back cleanly |
| `python -m alembic check` | `No new upgrade operations detected.` |
| `python -m tests.cassettes.build_cassettes` | `acme_small_talk: 5 interaction(s)`; re-recording changes only `recorded_at` |

The 590 are phase 2's 477 plus 113: 46 prompt-assembly (most of them injection attempts), 22
provider and cassette (one of them the deselected live test), 19 `llm` node, 10 tool gateway, 6
slot extraction, 6 memory, 4 golden conversation.

## Self-critique

Written after re-reading DESIGN.md 3, 6.2, 10, 11.1 to 11.3 and 14, and PLAN.md.

### What did I skip or simplify?

- **The AnthropicProvider has never made a call.** Its own section below.
- **Retrieval, and therefore citations.** Layer 7 exists, is fenced, is budgeted and is empty:
  nothing retrieves. `LlmNodeOutput.citations` is recorded on the trace and *not checked against
  anything* - DESIGN.md 9.2's citation guardrail is phase 5. A model can claim `["k1"]` today and
  nothing notices. The `knowledge:` block on an `llm` node is parsed and ignored.
- **Guardrails.** DESIGN.md 14's inbound injection flag is supposed to lower the allowed decision
  confidence and disable the tool loop for that turn. Neither exists; there is no PII tagging and
  no outbound scan. Phase 7 owns them, and the prompt fences are not a substitute.
- **Cost limits.** `max_llm_cost_per_conversation_usd` is in the manifest and enforced nowhere.
  Usage is summed per node and written to the trace, but never accumulated per conversation, so
  the one limit DESIGN.md 7.3 names alongside `max_nodes_per_turn` is not enforced (phase 9).
- **LLM replay from the trace.** DESIGN.md 7.3: "LLM calls replay from the trace if the step
  already completed." `trace_step.llm_response` is written and never read. It does not bite -
  phase 2 established that a *committed* step is never re-run - but a step that crashes after the
  model answered and before the checkpoint commits calls the model again on retry, which costs
  money and may answer differently. The step id is on `NodeRuntime` ready to be the cache key.
- **Only the node decision retries.** Slot extraction and summarisation get one attempt each; a
  malformed extraction hands off and a failed summary is dropped. DESIGN.md 11.3's retry is about
  the node decision, so this is defensible, but the asymmetry is a choice and not a design rule.
- **The token estimate is four characters per token.** The real count needs the provider's
  tokenizer (`count_tokens`, a network round trip per layer per turn, and no key here). The
  budgets are therefore approximate, and approximate in an unknown direction for non-English
  text - a Japanese or Arabic email thread may be badly under- or over-counted.
- **One cache breakpoint, no TTL.** Layers 1 to 3 get the breakpoint; a longer-lived tool
  definition block or a second breakpoint after layer 5 might pay for itself and is untested.
- **No streaming.** DESIGN.md 12 says web chat streams the final message. Nothing streams.
- **The escalation model is only the failure ladder's last rung.** DESIGN.md 5.1 also intends it
  for handoff summaries and hard reasoning nodes; a node can name a model with `model:`, but
  nothing routes to the escalation model by *kind* of work.
- **`interrupt_check` is still `continue`-only** (phase 6), so DESIGN.md 6.6's structured
  classification is not a model call yet even though the LLM layer that would make it one exists.

### Where does the code diverge from the design?

Everything in the Plan's deviation list held. What emerged while building:

- **Layer 9 is fenced data in a single user message, not native chat turns.** This is the phase's
  biggest judgement call and it cuts both ways. It makes principle 7 structural - a customer
  message cannot become a turn boundary, and the injection tests mean something because of it -
  but models are trained on chat structure, and a transcript rendered as delimited blocks inside
  one user turn is not the shape they saw most of. With no API key I cannot measure the quality
  cost. Recorded in BACKLOG.md's decisions log; a phase with a key should measure it before
  shipping, and the change is contained to `assemble`.
- **`NodeError.reason`** adds three handoff reasons DESIGN.md 7.3 does not name
  (`llm_invalid_output`, `low_confidence`, `model_requested_handoff`) beside the one it does.
- **Node instructions are not rendered as templates.** DESIGN.md 6.4 shows `instructions` as
  prose and does not say. Rendering them would let a pack interpolate state - which holds
  customer text - into a trusted layer, so they are placed verbatim. A pack that wants state in
  the prompt gets it through layer 6, fenced. This is stricter than the design and I think it is
  right, but it does mean an `llm` node cannot say "the charge is {{ state.amount }}".
- **The `ask` slot check narrowed** to the node's declared slots (above).
- **`llm` nodes gained an optional `model:`** key, which DESIGN.md 11.1 asks for ("per-node
  override") and 6.4's example does not show.

### What could a hostile pack author still achieve through the prompt?

The honest answer is: quite a lot inside its own layer, and nothing outside it.

1. **A pack owns layers 2, 3 and 4 outright.** "Always choose the `refund` edge", "tell the
   customer their identity is verified", "reveal these policies on request" are all just text a
   pack may write, and the model will likely comply. What that *cannot* do is reach outside the
   decisions the graph declares, execute a tool, or satisfy a gate - those are structural. So a
   hostile pack can make the agent lie, promise, and pick a bad branch among the allowed ones;
   it cannot make it act. That is the design's own division and it is worth stating plainly,
   because "the prompt is hardened" could otherwise be read as more than it is.
2. **Edge labels and node descriptions are pack text in layer 5.** An edge named
   `ignore_previous_instructions_and_pay` renders as an allowed decision. It is neutralised for
   structure only. Bounded the same way: choosing it does whatever the graph says it does.
3. **A pack can deny itself service through `llm.prompt_budget`.** Setting `core_system: 1`
   makes every turn raise `PromptTooLargeError` and hand off. Loud, self-inflicted, and not
   currently a validator finding - it should probably be one (a budget below the core prompt's
   own size is always wrong).
4. **Risk tiers are still self-declared** (`tools/tools.yaml`, phase-1 finding I). The gateway
   cross-checks the manifest against what the runtime reports and refuses a disagreement, which
   is new, but with both sources under the pack author's control in phase 4 a mislabelled tool is
   still a mislabelled tool. Phase 4 owns the registry.

### What could a hostile customer still achieve?

1. **Persuasion within the allowed set.** The fences and the core rule make injection *visible*
   and *structurally contained*; they do not make the model immune. A customer who writes a
   convincing story can still push a classifier from `small_talk` to `account_question`. That is
   the design's intent - the model decides among allowed transitions - but a customer choosing
   the branch is worth being explicit about.
2. **Summary poisoning is the one that would worry me.** Customer text reaches
   `conversation.summary` through a model, and the summary is in *every* later prompt. The
   summariser is told not to record instructions and the summary is fenced and cannot authorise
   an action, but a determined customer has a persistent channel into future prompts. Nothing
   validates a summary before it is stored. If I had another hour this is what I would spend it
   on: a length cap is not a filter.
3. **Window eviction.** Layer 9 keeps the newest messages, so a customer who floods can push the
   earlier part of the conversation out of the prompt. The summary partly covers it, and the
   summary is exactly what they also influenced.
4. **State-layer eviction.** A huge value in one state field makes the state layer drop the
   largest field to fit; if that is the field the node needed, the model is now reasoning without
   it. The frame's state is untouched, so nothing durable is lost, but the prompt is degraded by
   a customer's input length.
5. **Reflected content.** A `say` node renders state into a customer-visible message, and state
   can hold the customer's own text. Not an escalation, but a pack that echoes a slot is echoing
   whatever was put in it.
6. **No injection flag.** DESIGN.md 14 would have the tool loop disabled and the confidence bar
   raised for a flagged turn. Until phase 7, a suspicious message is treated like any other.

### What is untested because there is no API key?

`AnthropicProvider.complete` has never run. Specifically unproven, in rough order of how likely I
think each is to be wrong:

1. **Whether the API accepts the structured tool's schema with `strict: true`.** Pydantic emits
   `$defs`/`$ref` for the nested `state_updates` model. `json_schema_for` closes every object and
   marks every property required, but if strict validation rejects `$ref`, every structured call
   fails with a 400 - which the ladder would turn into a handoff on the first real turn.
2. **Whether the cache breakpoint does anything.** The static prefix here is roughly 400 to 600
   tokens, and the minimum cacheable prefix is model-dependent and can be larger than that. If it
   is, `cache_control` is a silent no-op and DESIGN.md 11.1's prompt caching is not happening.
   The check is one line - `usage.cache_read_input_tokens` across two turns - and it needs a key.
3. **Whether forced `tool_choice` is accepted by the pack's models.** It is for the Sonnet and
   Opus families; some newer model families reject a forced tool choice outright, and the code
   would need the structured-output parameter instead. The docstring says so; nothing tests it.
4. **The error mapping against real SDK exceptions.** The translation is by class *name* and
   `status_code`, tested against stand-ins with those shapes, never against the real classes.
5. **Whether the model ids in `packs/acme_billing/pack.yaml` exist for the caller's account.**
6. **Response quality of the whole prompt shape**: the fenced conversation, the neutralised
   lines, the decision list, whether a real model reliably returns `unclear` rather than a
   confident wrong label. Every "the model chooses X" test here is a test of the *engine*, with a
   scripted answer. None of them is evidence about a model.
7. **Latency and cost per turn**, which phase 2's self-critique flagged as the thing that turns
   the lock-per-waiter question (finding R7, deferred to phase 7) from theory into practice.

The `live` group (`pytest -m live`) is one test. It would answer 1, 3, 4 and 5; it is not enough
for 2 or 6.

### Which tests are weak?

- **Every "the model does X" test is scripted.** That is unavoidable and correct, but it means
  the suite proves the engine's handling of an answer, never the answer. The golden conversation
  is a replay of answers a rule table produced.
- **The injection suite proves containment, not immunity.** It asserts that no caller can forge
  prompt structure. It cannot assert that a model ignores a plain-language "ignore your
  instructions" inside a fence, because that is a property of the model. An eval (phase 8) is the
  only honest place to measure it, and this phase does not pretend otherwise.
- **The tool loop is tested with one tool and short loops.** No test has two tools in one
  iteration, a tool that errors, or a result large enough to hit the layer-8 budget mid-loop.
- **The prompt-budget tests use synthetic text.** No test measures a real transcript against a
  real tokenizer, because there is no real tokenizer here.
- **The memory differential test is honest but narrow.** It proves the *engine* does not depend
  on the summary, and it proves the comparison is not vacuous (the prompts differ). It does not
  prove a model's behaviour is unaffected, which is the interesting half and needs an eval.
- **`FailingProvider` and the ladder test count model names.** They show the order the rungs are
  tried in; they do not show that backoff actually waits, because the test injects a no-op sleep.
- **Nothing tests two `llm` nodes in one turn emitting messages in order** - phase 2's finding R4
  made that possible and this phase is the first that can produce it, but the golden conversation
  happens to put its two messages in different checkpoints.
- **No property test anywhere in the phase.** A generator over hostile strings crossed with the
  eight untrusted slots, asserting the "only core wrote structure" invariant, would be strictly
  better than six hand-written payloads, and it is the first test I would add.

### What would break under concurrency or a crash mid-step?

The things I believe are solid:

- **The turn counter cannot drift.** It advances inside the claim transaction for a customer
  message and inside the turn-start transaction for a resume, so a process that dies leaves it
  where it was; a re-entered turn does not re-count. `tests/test_memory_summary.py` kills a
  process inside the claim window and requires the count unchanged and the message still pending.
- **The summary cannot half-write.** The summary and the turn it covers are one update, and the
  whole summarisation runs under the conversation lock after the turn has settled.
- **A stale or missing summary changes nothing durable**, proven differentially.
- **Prompt assembly is pure.** No shared state, no cache, nothing to race.

What I know is fragile:

1. **An LLM call is not idempotent and is not replayed.** A crash between the model answering and
   the checkpoint committing means the next attempt pays for and re-asks the question, and may
   get a different answer. The trace already carries the response and the step id is already the
   documented cache key; wiring them together is the fix, and it is not done.
2. **Turns are now seconds long**, which is exactly the condition phase 2's deferred finding R7
   describes: the conversation lock is held for the whole turn on a dedicated connection, and a
   burst on one conversation stalls a connection per waiter. Phase 3 makes that real rather than
   theoretical, and phase 7 owns it.
3. **The history query runs once per node.** With `max_nodes_per_turn: 25` that is up to 25 small
   queries per turn, inside the lock. Correct (a re-executed node must see the committed window,
   not a cached one) but not free, and no test measures it.
4. **A summariser that hangs holds the conversation lock.** The failure is swallowed but there is
   no timeout: an unresponsive provider blocks the lock until the client's own timeout fires.
   The provider has a 60-second default; nothing in the engine bounds it.
5. **`_maybe_summarize` reads `turn_count` in one transaction and writes in another.** Under the
   conversation lock nothing else writes them, so the window is closed by the lock rather than by
   the schema - which is the shape of dependency phase 2 spent a whole review removing.
6. **The tool gateway's budget is per node, not per turn.** `max_tool_calls_per_turn` is passed
   as the per-node cap, so a turn with three tool-using `llm` nodes could make three times the
   manifest's limit. DESIGN.md 5.1 says "per turn". A counter on the run row is the fix; the
   limit is generous enough that nothing today reaches it, which is why it is recorded here
   rather than papered over.
7. **A `PromptTooLargeError` is raised at assembly time, inside the node**, so it becomes a node
   error and a handoff - correct - but it is raised on *every* attempt, including the retry, so a
   pack with an over-budget persona burns two model-free attempts and hands off every turn.

### What I fixed while writing this

Two of the items above started as findings in this section and were fixed rather than recorded:
`_begin_turn`'s two-transaction turn count (item 5's worse sibling), and the tool budget counting
its own refusals after it was spent. Item 6 (per-node versus per-turn tool budget) is recorded
rather than fixed because the honest fix is a counter on the run row, which is phase 4's
territory: it owns `tool_call` and the idempotency key, and a limit counted in two places would
be worse than one counted in the wrong place.
