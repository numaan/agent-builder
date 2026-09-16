# Authoring a Domain Pack — Developer Guide

`app.py` is `create_app(load_pack(config.pack))`. **`support_core` is the library; the pack is the
domain.** To run this agent for a new domain you write a *pack* and point configuration at it — you
never fork core. `packs/acme_billing` is the worked example this guide draws every snippet from; the
authoritative design rationale is in [`DESIGN.md`](../DESIGN.md), and the shorter operational guide
is the README's [Running for a different domain](../README.md).

**What core already gives you** (so a pack never builds it): the turn loop and durability, both web
transports (WebSocket at `/channels/web_chat/ws` and AG-UI SSE at `/channels/ag_ui`), the demo
pages, static hosting of a pack's own UI at `/app`, the human desk + handoff queue, knowledge
retrieval + the citation guardrail, rolling memory, approval binding, and the 9-layer prompt
assembly. **A new domain is graphs, tools, knowledge, prompts, and a manifest.**

---

## 1. Anatomy of a pack — every file

```
packs/mydomain/
  pack.yaml            Manifest: identity, channels, LLM, interrupts, handoff, limits, memory,
                       guardrails, and (optional) UI branding.
  graphs/
    root.yaml          Entry graph: a `classify` node routes intents to workflow sub-graphs.
    verify_identity.yaml   The ONLY graph allowed to set identity_verified.
    <workflow>.yaml    One graph per task (refund, update_address are the samples).
  tools/
    __init__.py        Exports `TOOLS: list[Tool]` — the whole contract with the tool runtime.
    <integration>.py   Your real calls to billing / CRM / passcode provider, each with a risk tier.
  knowledge/
    sources.yaml       What to index (markdown dirs, crawls) + live lookups.
    docs/*.md          The documents the citation guardrail answers from.
  persona.md           Prompt layer 2: voice.
  policies.md          Prompt layer 3: rules.
  ui/                  (optional) The pack's own front end, served at /app.
    index.html, app.js
  evals/               (optional) Golden conversations + node evals for `support pack eval`.
    golden/  nodes/
```

Everything a browser sees or that runs at load is validated: an unknown key in `pack.yaml` is a
**startup failure**, not a silent default, and `support pack validate` refuses a pack whose graphs,
tools, forms, or knowledge don't hang together.

---

## 2. The manifest — `pack.yaml` field by field

From `packs/acme_billing/pack.yaml`:

```yaml
id: acme-billing               # stable pack id (kebab-case)
version: 0.1.0                 # pack version (semver)
core: ">=0.0.1,<1"            # the support-core version this pack targets
entry_graph: root             # which graph a new conversation starts in
language: en
channels: [web_chat, email]   # the channels this pack serves

llm:
  default_model: claude-sonnet-5
  escalation_model: claude-opus-5    # handoff summaries + hard reasoning; the retry "ladder" rung
  confidence_threshold: 0.45         # below this a `classify` node takes its `unclear` edge
  max_tool_iterations: 5             # bound on an llm node's read-tool loop
  retries: 2                         # attempts per model before escalation
  prompt_budget:                     # per-layer token budgets (see §6)
    persona: 600
    policies: 1200
    conversation: 3000

interrupts:                          # topic-change handling (DESIGN.md 6.6)
  allowed_from: [root, refund, update_address]   # graphs a customer may park to switch topic
  blocked_in: [verify_identity]                  # graphs that must finish before anything else

handoff:
  queue: billing-tier-1              # the queue name handoff packets land on
  sla_minutes: 30

limits:                              # hard per-turn / per-conversation ceilings
  max_nodes_per_turn: 25
  max_tool_calls_per_turn: 10
  max_llm_cost_per_conversation_usd: 2.00

memory:                              # rolling summary (DESIGN.md 10)
  summarize_every_turns: 2
  window_messages: 10
  max_summary_chars: 600

guardrails:
  citations:
    enabled: true                    # refuse+re-prompt any answer that states a fact with no citation

ui:                                  # optional; see §7
  title: Acme Billing
  subtitle: Customer support
  accent: "#1f6feb"                  # hex only (echoed to the browser, so it is validated)
  dir: ui                            # static assets folder, served at /app
  suggestions:                       # one-click starter messages the front end shows
    - "I got charged twice ..."
```

---

## 3. Describing the flow — graphs

A graph is a YAML file in `graphs/`. Its skeleton:

```yaml
id: refund
description: >
  Human-readable purpose. Shown in traces and tooling.
inputs:                       # values the caller (a subgraph node) passes in
  charge_hint: str | None
outputs:                      # values this graph returns to its caller
  outcome: Literal["refunded", "denied", "escalated", "abandoned"]
state:                        # the graph's own namespace — typed fields nodes read/write
  charge_id: str | None
  charge: Charge | None
  eligible: bool | None
  outcome: str | None
start: identity_gate          # the first node
nodes:
  identity_gate: { ... }
  ...
```

### Node types

Every node has a `type` and a `description`. The ten core types (`support_core/graph/nodes.py`):

| Type       | What it does | Chooses edge? | Suspends? |
|------------|--------------|---------------|-----------|
| `gate`     | Assert a `predicate`; on false push `redirect` (a **graph id**) as a sub-frame, then continue to `next`. Fires on **every** entry to the frame. | no | no |
| `llm`      | Prompted step: fill state via `output_schema`, optionally call READ `tools`, optionally attach `knowledge`, and choose one `edges` label. | yes | no |
| `ask`      | Ask the customer for `slots`; `prompt` is shown; suspends until they reply; may carry a `form:`. | no | waits for customer |
| `tool`     | Deterministic tool call. `args` from expressions, result mapped by `into`, `on_error` route, optional `requires_approval`. | no | only if the tool is async |
| `confirm`  | Present a proposed `action` and require an explicit yes; `edges: {yes, no}`; may carry a `form:`. | yes | waits for customer |
| `router`   | Branch on state predicates in `edges` (first true wins), else `default`. | yes | no |
| `say`      | Emit a templated `message` (no LLM call), then `next`. | no | no |
| `subgraph` | Invoke another `graph` with `inputs`/`outputs` mapping, then `next`. | no | no |
| `handoff`  | Build a handoff packet, tell the customer, suspend until a human resumes/closes; `edges: {resumed, closed}`. | yes | waits for human |
| `end`      | Pop the frame and return `outputs`. | no | no |

### Expressions and templates

- **Expressions** (in `predicate`, `args`, `into`, `edges` keys, `inputs`/`outputs`) read from three
  namespaces: `ctx.*` (the conversation context — `ctx.customer.ref`, `ctx.customer.identity_verified`),
  `state.*` (this graph's state), and `result.*` (a tool's return value, inside `into`).
- **Templates** (Jinja, sandboxed) are used in `say.message`, `ask.prompt`, `confirm.prompt`,
  `handoff.message`: `{{ state.line1 }}, {{ state.city }}`, with filters like `| money` and `| default(...)`.

### The entry graph (`root.yaml`) pattern

`root` starts at a `classify` **llm node**. Its `output_schema` writes an `intent`, and its `edges`
map each intent label to a target — usually a `subgraph` node per workflow, plus small-talk, a
refusal path, and an `unclear` path. Adding a workflow is: one graph file, one `classify` edge, one
`subgraph` node, and its tools. Nothing in `support_core` changes.

```yaml
classify:
  type: llm
  instructions: |
    Decide which path this message belongs on: refund / update_address / small_talk /
    account_question / refused / finished / unclear. Set `intent`; do not answer here.
  output_schema: { intent: str | None, charge_hint: str | None }
  edges:
    refund: do_refund
    update_address: do_update_address
    unclear: puzzled
do_refund:
  type: subgraph
  graph: refund
  inputs: { charge_hint: state.charge_hint }
  outputs: { refund_outcome: outcome }
  next: anything_else
```

### Identity and the gate

Any workflow that touches the account starts with a `gate` whose `redirect` is the
`verify_identity` graph:

```yaml
identity_gate:
  type: gate
  predicate: ctx.customer.identity_verified
  redirect: verify_identity      # a GRAPH id, pushed as a sub-frame when the predicate is false
  next: find_charge
```

### Two safety rules a graph cannot skip (enforced by the validator)

1. **Every `write`/`high`-risk tool needs a `confirm` node on every path from the last customer
   input.** The `tool` node that runs it carries `requires_approval: <confirm_node>`, and the engine
   re-checks a hash of the exact arguments at call time — a field edited between the confirm and the
   call is a mismatch and is refused.
2. **`identity_verified` is set only by the pack's verification workflow** — never by config, never
   by the client, never by an ordinary tool. Only a tool that declares `patches_context={"identity_verified"}`
   may set it (that is `verify_otp`).

---

## 4. "Skills" = capabilities (how the agent knows how to do a thing)

This framework has no separate "skills" primitive — a capability is expressed as the combination of:

- **a workflow graph** in `graphs/` (the steps), reachable from `root`'s `classify` edges;
- **the tools** that graph calls (`tools/`), each with a risk tier;
- **the knowledge** it cites (`knowledge/`), for any claim the customer will act on;
- **the prompt layers** that shape how the `llm` nodes talk (persona/policies + each node's `instructions`).

So "add a skill" means: write `graphs/<skill>.yaml`, add its intent label + `subgraph` node to
`root.yaml`'s `classify`, export the tools it needs from `tools/__init__.py`, and (if it makes
claims) add documents to `knowledge/`. `update_address` is the worked example of exactly this — the
whole "skill" cost one graph file, two tools, and one edge in `root`.

---

## 5. Tools — the domain's real integrations

`tools/__init__.py` exports `TOOLS: list[Tool]`; the registry is built from that list, and it is the
**only** source of a tool's risk tier at run time.

```python
from support_core.tools import Tool
from .billing import ISSUE_REFUND, GET_CHARGE, CHECK_REFUND_ELIGIBILITY, LIST_RECENT_CHARGES

TOOLS: list[Tool] = [LIST_RECENT_CHARGES, GET_CHARGE, CHECK_REFUND_ELIGIBILITY, ISSUE_REFUND, ...]
```

A tool is a `FunctionTool` wrapping a coroutine, with typed Pydantic input/output models and a risk
tier (`support_core/tools/base.py`, `risk.py`):

```python
from support_core.tools import FunctionTool, Risk, Tool, ToolContext, ToolFailed

async def _issue_refund(payload, ctx: ToolContext) -> RefundOutput:
    if not ctx.customer.identity_verified:            # graph gate is the first line; this is the second
        raise ToolFailed("refusing to refund on an unverified identity")
    refund = await billing.refund(payload.charge_id, payload.amount)
    return RefundOutput(refund_id=refund.id, status=refund.status)

ISSUE_REFUND: Tool = FunctionTool(
    name="issue_refund",                       # ^[a-z][a-z0-9_]*$ — appears in YAML, prompts, hashes
    description="Refund a charge in full to the original payment method.",
    input_model=RefundInput,                   # Pydantic; extra="forbid"
    output_model=RefundOutput,
    risk=Risk.HIGH,                            # READ | WRITE | HIGH
    idempotent=False,                          # at-most-once: never replayed after a crash
    handler=_issue_refund,
)
```

**Risk tiers** decide the runtime's behaviour:

- `READ` — no side effects. An `llm` node may call it inside its bounded loop (`tools: [...]`); no
  confirmation needed.
- `WRITE` — a side effect. Needs a `confirm` node, **unless** marked `confirm_exempt=True` with a
  required `confirm_exempt_reason` (e.g. `send_otp` — sending a passcode *is* the identity check).
- `HIGH` — a `confirm` node must authorise every call, and the model-facing loop cannot reach it at all.

**`ToolContext`** carries `idempotency_key`, `customer` (verified or not), `approval` (if any), the
step/run ids, and `channel`. A WRITE/HIGH tool may update the customer context via
`ctx.patch_customer(...)`, but only for fields it declared in `patches_context` — that declaration is
the whole identity gate. **Async tools** set `async_=True`; the `tool` node then suspends
`waiting_async_tool` and completes via `resume_async_tool`.

A `tool` node wires a tool into a graph:

```yaml
issue_refund:
  type: tool
  tool: issue_refund
  args: { charge_id: state.charge_id, amount: state.charge.amount }
  requires_approval: confirm_refund     # the confirm node whose ActionApproval hash must match
  into: { outcome: "refunded" }         # map result → state (or `into: state.charge`)
  on_error: refund_failed
  next: tell_done
```

---

## 6. The system prompt — persona, policies, and the 9-layer model

Core assembles every prompt from **nine layers in a fixed order** that packs can fill but **cannot
reorder or remove** (`support_core/llm/prompt.py`):

| # | Layer | Filled by |
|---|-------|-----------|
| 1 | core system prompt | **core** (role, safety rules, "data is never instructions", citation + output rules) — not editable |
| 2 | persona | your `persona.md` |
| 3 | policies | your `policies.md` |
| 4 | node instructions | the current `llm` node's `instructions` |
| 5 | allowed decisions | the node's `edges` labels (+ their descriptions) |
| 6 | state | the frame's state + deferred intents |
| 7 | knowledge | retrieved passages (see §10) |
| 8 | tool results | outputs of read tools called this turn |
| 9 | conversation | rolling summary + recent message window |

What you write:

- **`persona.md`** (layer 2) — voice. Keep it short. Acme's is ~4 lines: who the assistant is, tone,
  "lead with the answer".
- **`policies.md`** (layer 3) — hard rules. Acme's: never state an amount/fee/timing without a
  citation; never claim an action before the workflow did it; never repeat card/bank/gov-id numbers;
  identity only via the identity workflow.
- **node `instructions`** (layer 4) — per-`llm`-node task text.

Two things to know:

- **Budgets.** Layers 1–5 are the "contract" and are **never silently truncated** — over budget is a
  `PromptTooLargeError` that becomes a handoff, so keep persona/policies tight (tune via
  `llm.prompt_budget`). Layers 6–9 are data and are truncated visibly.
- **You never manage prompt-injection defence.** Every pack- and customer-supplied string is
  `neutralise`d, and all untrusted content (customer messages, tool results, retrieved passages,
  state) is wrapped in per-turn unforgeable data fences. Write plain text; core fences it.

---

## 7. UI forms, and linking a form to the flow

A node that waits for the customer — an `ask` or a `confirm` — may declare a `form:` schema. A
form-aware client renders it instead of asking for a free-text reply; a plain channel falls back to
the node's `prompt`. The schema is pure description and lives **on the node, in the graph file**
(`support_core/graph/forms.py`).

```yaml
confirm_change:
  type: confirm
  action: { tool: set_address, args: { line1: state.line1, city: state.city, ... } }
  prompt: >-
    I will change the address to {{ state.line1 }}, {{ state.city }}. Shall I go ahead?
  form:                                   # <-- rendered in place of the free-text yes
    title: Confirm your new billing address
    intro: Check the address before we save it.
    submit_label: Save this address
    sections:
      - title: New address
        fields:
          - { key: line1, label: Address line 1, type: text, required: true }
          - key: postcode
            label: Postcode
            type: text
            required: true
            pattern: '^[A-Za-z]{1,2}\d[A-Za-z\d]? ?\d[A-Za-z]{2}$'    # compiled at load
            hint: 'UK format, for example EH7 4AH'
          - key: country
            label: Country
            type: select                  # select/radio/checklist REQUIRE options
            required: true
            options:
              - { value: GB, label: United Kingdom }
              - { value: IE, label: Ireland }
      - title: Confirmation
        fields:
          - { key: authorized, label: I confirm this is correct, type: checkbox, required: true }
  edges: { yes: apply_change, no: abandon }
```

**Field model** (`FormField`): `key` (`^[a-z][a-z0-9_]*$`, unique across the whole form), `label`,
`type` (`text | email | date | select | radio | checkbox | checklist`), `required`, `hint`,
`pattern` (a regex, compiled at load — a broken one fails startup), `options` (required for
choice types, forbidden otherwise), and conditionals `show_if` / `required_if`
(`{ field: <other_key>, equals: <value> }`).

**How a form links back into the flow:**

- On an **`ask`** node, the form collects the node's `slots`; the client submits the values, which
  fill those state slots and the graph continues to `next`.
- On a **`confirm`** node, the form is the review the customer edits and submits; submitting it is the
  explicit "yes" that authorises the bound `action`, and the graph takes the `yes` edge.
- Over AG-UI the form is emitted as a **`render_form` tool call** whose args are the schema
  (`support_core/channels/ag_ui.py`); a client with no form support answers the same gate with text.

**Serving your own front end.** Put static assets in `ui/` and add the `ui:` block (§2). When
`serve_client` is on, they're served at `/app`; the front end reads its branding
(title/subtitle/accent/suggestions) from `GET /channels/ag_ui`. See `packs/acme_billing/ui/` for a
self-contained, no-build client that renders `render_form` forms.

---

## 8. LLM and model configuration

**In the pack** (`pack.yaml` → `llm`, §2): `default_model`, `escalation_model`,
`confidence_threshold`, `max_tool_iterations`, `retries`, `prompt_budget`. A single `llm` node can
override the model with `model:`:

```yaml
hard_reasoning_node:
  type: llm
  model: claude-opus-5      # per-node override; unset = llm.default_model
  instructions: ...
  edges: { done: next_node }
```

**Providers** are a deployment choice, resolved from `AppConfig.provider`
(`support_core/api/config.py`): `auto | replay | anthropic | glm | none`.

- `auto` (the deployable default): live Anthropic if `ANTHROPIC_API_KEY` is set; else GLM if
  `GLM_API_KEY`/`ZAI_API_KEY`; else `replay` if a `cassette_dir` is configured; else `none`.
- `replay` plays recorded cassettes deterministically (the demo). A request the recording has never
  seen is **refused**, not guessed — which is why the demo is worth watching, and why driving a novel
  path in replay mode errors (see the "cassette gap" note below).
- `none` is valid for a pack with no `llm` nodes.

**Keeping the vendor out of the pack.** A pack names models it was written for; when the same pack
runs against a different vendor, override the ids per deployment via `AppConfig.models`
(`ModelOverride { default, escalation }`) or env `SUPPORT_MODEL` / `SUPPORT_ESCALATION_MODEL` —
`claude-sonnet-5` is not a name GLM answers to.

**Deployment config** is a JSON file named by `SUPPORT_APP_CONFIG`, with env overrides. The demo's
(`demo/acme_web_chat.json`):

```json
{
  "title": "Acme Billing support",
  "pack": "packs/acme_billing",
  "provider": "auto",
  "cassette_dir": "tests/cassettes",
  "new_conversation_context": { "customer": { "ref": "cus_acme_1", "name": "Sam", "email": "me@example.com" } },
  "suggestions": ["I got charged twice ...", "..."]
}
```

`new_conversation_context` is what a fresh conversation starts knowing — in production this is the
CRM lookup's result. It **can never contain `identity_verified`**. Other useful fields:
`serve_client`, `serve_desk` (+ `SUPPORT_DESK_TOKEN`), `qdrant_url`/`use_qdrant`,
`max_concurrent_turns`, `handoff_webhook_url`.

> **Cassette gap caveat (replay mode).** In `replay`, a step whose exact request wasn't recorded —
> e.g. a `confirm` interpretation reached by a path the cassettes don't cover — raises
> `LLMUnavailableError` and burns retry/backoff before failing. Re-record with
> `python -m tests.cassettes.build_cassettes` (add `--live` to record against the real API). With a
> live provider this doesn't arise.

---

## 9. MCP configuration

MCP servers are wrapped as ordinary `Tool`s (`support_core/tools/mcp.py`), so the same registry, risk
policy, approval binding, and idempotency apply — the runtime cannot tell an MCP tool from a native
one. Two rules are structural:

- **The pack must supply a risk tier per MCP tool.** Any tool the `risk_map` does not name is
  **HIGH** — meaning a `confirm` node must authorise it and the model loop can't reach it. A server
  that adds a tool overnight adds something the graph has no way to call, which is the failure mode
  to want.
- **MCP results are untrusted data.** They come back as `McpOutput.content` (a string) and reach a
  model only inside the standard data fence — never as instructions.

Wire it in `tools/__init__.py` by building wrapped tools and adding them to `TOOLS`:

```python
from support_core.tools import Tool
from support_core.tools.mcp import McpToolAdapter, wrap_tools
from support_core.tools.risk import Risk

RISK_MAP = {                     # every tool you intend to use, tiered explicitly
    "search_orders": Risk.READ,
    "create_ticket": Risk.WRITE,
    # anything omitted -> HIGH
}

# Option A — discover at load (async): tools = await McpToolAdapter(client, RISK_MAP).discover()
# Option B — wrap a known, offline-declared list (sync, testable):
MCP_TOOLS = wrap_tools(client, KNOWN_TOOL_INFOS, RISK_MAP)

TOOLS: list[Tool] = [*NATIVE_TOOLS, *MCP_TOOLS]
```

`McpToolAdapter` also takes `prefix` (namespacing), `timeout_s`, `idempotent` (per-tool overrides;
default is idempotent only for READ), and `confirm_exempt` (WRITE-only, with a reason). The transport
(stdio / SSE / streamable HTTP) is yours to wire — the adapter only needs an object with
`list_tools` and `call_tool`. **Note:** MCP servers requiring OAuth must be authorized in an
interactive session; a non-interactive server run can't complete that flow.

---

## 10. RAG / knowledge calls

Knowledge answers questions and **backs every claim with a citation**. Declare sources in
`knowledge/sources.yaml`:

```yaml
documents:
  - id: policy-docs
    type: markdown_dir            # or html_crawl (url/depth/refresh)
    path: ./knowledge/docs
    refresh: weekly
knowledge_graph: []               # entities/relations (phase 9); parsed now so typos fail at load
live_lookups: []                  # READ tools the retriever may call for a fact no document holds
```

Index them (chunk by heading, embed, write to `doc_chunk`, build a fresh Qdrant collection and flip
its alias):

```bash
support pack knowledge sync packs/mydomain
```

Then **attach retrieval to an `llm` node** with a `knowledge:` block — the query is a Jinja template,
so it can interpolate state:

```yaml
tell_done:
  type: llm
  instructions: |
    Tell the customer the refund is on its way and say when the money arrives. Take the timing
    from the knowledge block and cite the passage you took it from; do not state a number the
    passages do not give you.
  knowledge: { query: "refund processing time card statement", k: 3 }   # k: 1..20, default 3
  output_schema: {}
  edges: { done: done }
```

At run time the retrieved passages land in **prompt layer 7**, each keeping its `id`. The **citation
guardrail** (enabled in `pack.yaml` → `guardrails.citations`) refuses and re-prompts any answer that
states a fact without citing a passage id — which is why `policies.md` also says "never state an
amount/fee/timing without a citation". Retrieval spans Postgres (lexical + dense) and Qdrant; if
Qdrant is down it **degrades to keyword search** rather than failing the turn (`use_qdrant: false`
turns the vector side off deliberately; a Qdrant that's *down* is a per-call fault that's logged and
survived).

Use `live_lookups` only for a dynamic fact no document can hold; a fact the workflow establishes
(like the specific charge) is fetched by a `tool` node instead, where the argument comes from state,
not the customer's wording.

---

## 11. Validate, run, and test

Before serving, the pack must pass the same gate `make check` runs — graphs type-check against the
real tool signatures, every write sits behind a confirm, forms compile, and the interrupt graphs
exist:

```bash
support pack validate packs/mydomain
```

Then point config at the pack and bring it up (the four commands, from the README):

```bash
export SUPPORT_APP_CONFIG=deploy/mydomain.json
sh scripts/db-up.sh                              # Postgres + pgvector, Qdrant
python -m alembic upgrade head
support pack knowledge sync packs/mydomain       # index the pack's documents
python -m uvicorn app:app --port 8000
```

Golden conversations and node evals (in `evals/`) run with `support pack eval packs/mydomain`.

---

## 12. New-pack checklist

1. `cp -r packs/acme_billing packs/mydomain` and edit `pack.yaml` (`id`, `version`, `entry_graph`,
   `llm`, `handoff`, `guardrails`).
2. Rewrite `graphs/root.yaml`'s `classify` for your intents; add a `subgraph` node + edge per workflow.
3. Write one graph per workflow. Start account-touching graphs with a `gate` → `verify_identity`.
   Put a `confirm` before every `write`/`high` tool, with `requires_approval` on the tool node.
4. Replace `tools/` with real integrations; export them from `TOOLS` with correct risk tiers. Wrap
   any MCP servers (§9) with an explicit `risk_map`.
5. Swap `knowledge/docs/*` for your policy/pricing/timing docs; wire `knowledge:` blocks on the
   `llm` nodes that make claims.
6. Rewrite `persona.md` and `policies.md`; keep layers 2–3 within budget.
7. (Optional) Add `ui/` + a `ui:` block for a branded front end and `form:` schemas on `ask`/`confirm`
   nodes.
8. `support pack validate packs/mydomain` → green, then run.

For the design rationale behind any of this, see [`DESIGN.md`](../DESIGN.md) (sections referenced
throughout the pack files themselves).
