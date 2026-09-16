# Domain Pack — Schema Reference

Every file in a pack, and every property, with its type, whether it's required, its default, and
what it means. This is the field-level companion to the [authoring guide](authoring-a-pack.md); read
that first for the narrative. Types and defaults here are taken from the Pydantic models in
`support_core` — a typo in any key is a **startup failure**, and every model rejects unknown keys.

Conventions in the tables below: **Req?** = required (✓) or optional (—); **Default** is what you get
if you omit an optional field; **Constraints** are what validation enforces at load.

---

## `pack.yaml` — the manifest

Model: `support_core.graph.manifest.PackManifest`.

### Top level

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `id` | string | ✓ | — | Pack id. Pattern `^[a-z0-9][a-z0-9_-]*$`. |
| `version` | string | ✓ | — | Pack version. Must be a valid PEP 440 version (e.g. `0.1.0`). |
| `core` | string | ✓ | — | PEP 440 specifier the installed `support_core` must satisfy (e.g. `">=0.0.1,<1"`). Must name at least one constraint. |
| `entry_graph` | string | ✓ | — | Id of the graph a new conversation starts in (e.g. `root`). Non-empty. |
| `language` | string | — | `en` | BCP 47 tag (`en`, `pt-BR`); ≥ 2 chars, non-blank. |
| `channels` | list of enum | ✓ | — | Customer channels served. Each is `web_chat` or `email`; no duplicates; ≥ 1. |
| `llm` | block | ✓ | — | LLM behaviour. See **llm** below. |
| `interrupts` | block | — | empty | Topic-change rules. See **interrupts**. |
| `handoff` | block | ✓ | — | Human-handoff routing. See **handoff**. |
| `limits` | block | — | defaults | Per-turn/-conversation ceilings. See **limits**. |
| `timeouts` | block | — | defaults | Suspension timeouts. See **timeouts**. |
| `memory` | block | — | defaults | Rolling summary + window. See **memory**. |
| `guardrails` | block | — | defaults | Non-structural guardrails. See **guardrails**. |
| `ui` | block | — | empty | Branding + own front end. See **ui**. |

### `llm` (`LlmConfig`)

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `default_model` | string | ✓ | — | Model id every `llm` node uses unless it overrides. Non-empty. |
| `escalation_model` | string | — | = `default_model` | Handoff summaries, hard nodes, and the last retry rung. |
| `confidence_threshold` | float | — | `0.4` | 0.0–1.0. Below this on a decision, the node takes its `unclear` edge (or hands off), never the guess. |
| `max_tool_iterations` | int | — | `5` | 0–20. Bound on an `llm` node's read-tool loop. |
| `retries` | int | — | `2` | 0–5. Attempts per model before the escalation rung. |
| `max_output_tokens` | int | — | `4096` | ≥ 256. Max tokens a model may return. |
| `prompt_budget` | map<string,int> | — | `{}` | Per-layer token budgets; keys must be layer names (`persona`, `policies`, `node_instructions`, `conversation`, …); each ≥ 1. Unset layers keep the core default. |

### `interrupts` (`InterruptConfig`)

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `allowed_from` | list<string> | — | `[]` | Graph ids a customer may park (to switch topic) and be offered back afterwards. No blanks, no duplicates. |
| `blocked_in` | list<string> | — | `[]` | Graph ids that must finish before anything else (e.g. `verify_identity`). Same constraints. |

### `handoff` (`HandoffConfig`)

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `queue` | string | ✓ | — | Queue name handoff packets land on. Non-empty. |
| `sla_minutes` | int | — | `null` | Target response time; ≥ 0, or unset. |

### `limits` (`LimitsConfig`)

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `max_nodes_per_turn` | int | — | `25` | ≥ 1. Node executions before the turn is cut off. |
| `max_tool_calls_per_turn` | int | — | `10` | ≥ 0. Tool calls per turn. |
| `max_llm_cost_per_conversation_usd` | float | — | `2.0` | ≥ 0. Spend ceiling per conversation. |
| `max_node_errors` | int | — | `3` | ≥ 1. Consecutive failures of one node in a frame before handoff; reset on success. |

### `timeouts` (`TimeoutsConfig`)

Per-suspension-status rules. Each of `waiting_customer`, `waiting_human`, `waiting_async_tool`,
`waiting_timer` is a **`TimeoutRule`**; `channels` overrides them per channel.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `waiting_customer` | TimeoutRule | — | `{seconds: null, action: none}` | Waiting on the customer. |
| `waiting_human` | TimeoutRule | — | `{seconds: null, action: none}` | Waiting on a human (handoff). |
| `waiting_async_tool` | TimeoutRule | — | `{seconds: 900, action: handoff}` | Waiting on a long-running tool. |
| `waiting_timer` | TimeoutRule | — | `{seconds: null, action: none}` | Waiting on a timer. |
| `channels` | map<channel, ChannelTimeouts> | — | `{}` | Per-channel overrides; an unset status falls back to the pack rule. |

**`TimeoutRule`**: `seconds` (int ≥ 1, or `null` = wait forever) · `action` (`none` / `close` /
`handoff`, default `none`).

### `memory` (`MemoryConfig`)

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `summarize_every_turns` | int | — | `0` | ≥ 0. `K`; `0` disables the rolling summary. |
| `window_messages` | int | — | `12` | ≥ 1. Recent messages placed in the prompt window. |
| `max_summary_chars` | int | — | `1200` | ≥ 100. Cap on the rolling summary. |

### `guardrails` (`GuardrailsConfig`)

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `citations` | CitationPolicy | — | on | The outbound citation check. Structural guardrails (risk tiers, approval hashes, gates, limits) are **not** configurable here. |

**`citations` (`CitationPolicy`)**: `enabled` (bool, default `true`) · `extra_claim_patterns`
(list<regex>, additive only — you can't remove a core pattern) · `allow_uncited_questions` (bool,
default `true` — a sentence ending in `?` isn't treated as a claim).

### `ui` (`PackUI`)

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `title` | string | — | `null` | Front-end title. |
| `subtitle` | string | — | `null` | Front-end subtitle. |
| `accent` | string | — | `null` | Accent colour; **hex only**, pattern `^#[0-9a-fA-F]{3,8}$` (it's echoed to a browser). |
| `suggestions` | list<string> | — | `[]` | One-click starter messages. |
| `dir` | string | — | `ui` | Static-assets folder, served at `/app`. Must be a **single segment inside the pack** — no `/`, `\`, `.`, or `..`. |

---

## `graphs/*.yaml` — a workflow graph

Model: `support_core.graph.schema.GraphFileSchema`.

### Top level

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `id` | string | ✓ | — | Graph id. Pattern `^[a-z][a-z0-9_]*$`. Unique across the pack. |
| `description` | string | — | `null` | Human-readable purpose (shown in traces). |
| `inputs` | map<string,typestr> | — | `{}` | Values a caller passes in; value is a **type string** (e.g. `str \| None`). |
| `outputs` | map<string,typestr> | — | `{}` | Values this graph returns to its caller. |
| `state` | map<string,typestr> | — | `{}` | The graph's own state fields and their types. All are treated as **optional** (may be null mid-run). |
| `start` | string | ✓ | — | Id of the first node. Non-empty. |
| `nodes` | map<string,node> | ✓ | — | Node id → node block; ≥ 1. Node ids match `^[a-z][a-z0-9_]*$`. |

**Type strings** in `inputs`/`outputs`/`state` are Python-style annotations resolved at load:
`str`, `int`, `float`, `bool`, `str | None`, `Literal["a","b"]`, and pack tool types like `Charge`.

**Values vs expressions.** In `args`/`into`/`inputs`/`outputs`/`edges` keys, a scalar is an
**expression** if it starts with a root — `state.`, `ctx.`, or `result.` — otherwise it's a
**literal** (`"refunded"`, `false`, `12`). A string that looks like an expression but starts with an
unknown root (`stat.charge_id`) is rejected as a probable typo.

### Fields every node may carry (`NodeBase`)

| Property | Type | Req? | Meaning |
|---|---|---|---|
| `type` | enum | ✓ | One of the ten node types below. |
| `description` | string | — | Purpose of the node. |

### Node types (all fields)

**`gate`** — assert a predicate; on false push a graph as a sub-frame, then continue.

| Property | Type | Req? | Meaning |
|---|---|---|---|
| `predicate` | expression | ✓ | Boolean expression; e.g. `ctx.customer.identity_verified`. |
| `redirect` | graph id | ✓ | Graph pushed when the predicate is false (a **graph** id, not a node id). |
| `next` | node id | ✓ | Where to go once the predicate holds. |

**`llm`** — prompted step: fill state, optionally call read tools / attach knowledge, choose an edge.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `instructions` | string | ✓ | — | The node's task text (prompt layer 4). |
| `tools` | list<tool name> | — | `[]` | READ-tier tools the model may call in this node's bounded loop. |
| `output_schema` | map<string,typestr> or `null` | — | `null` | State fields (and types) this node may write. `null` = **undeclared** = a validation error; `{}` = writes nothing (explicit). |
| `knowledge` | KnowledgeQuery | — | `null` | Retrieval attached to this node (see **Forms & knowledge** ↓). |
| `edges` | map<label,node id> | ✓ | — | Decision label → target; ≥ 1. The model chooses exactly one label. |
| `model` | string | — | pack default | Per-node model override. |

**`ask`** — ask the customer for slots; suspends until they reply.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `slots` | list<string> | ✓ | — | State fields to fill from the reply; ≥ 1. |
| `prompt` | string (Jinja) | ✓ | — | What to ask. Rendered as a template. |
| `next` | node id | ✓ | — | Where to go after the reply. |
| `form` | FormSchema | — | `null` | A form the client renders instead of a free-text reply (see **Forms**). |

**`tool`** — deterministic tool call.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `tool` | tool name | ✓ | — | The tool to call (must be in `TOOLS`). |
| `args` | map<string,scalar> | — | `{}` | Arguments; each value an expression or literal. |
| `into` | string \| map \| `null` | — | `null` | Where the result goes: a path (`state.charge`) or `{state field: result.x or literal}`. |
| `requires_approval` | confirm node id | — | `null` | The `confirm` node whose `ActionApproval` hash must match. Required for `write`/`high` tools. |
| `on_error` | node id | — | `null` | Where to route if the call fails. |
| `next` | node id | ✓ | — | Where to go on success. |

**`confirm`** — present a proposed action; require an explicit yes.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `action` | ConfirmAction | ✓ | — | The action being authorised (hashed into the approval). |
| `prompt` | string (Jinja) | ✓ | — | The confirmation question. |
| `edges` | map | ✓ | — | Exactly `yes` and `no` → target node ids. (Bare YAML `yes:`/`no:` are accepted.) |
| `form` | FormSchema | — | `null` | A form rendered as the review the customer edits and submits (see **Forms**). |

`ConfirmAction`: `tool` (tool name, ✓) · `args` (map<string,scalar>, default `{}`).

**`router`** — deterministic branch on state predicates.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `edges` | map<predicate,node id> | ✓ | — | Predicate expression → target; first true wins; ≥ 1. |
| `default` | node id | — | `null` | Target when none match (absent = may dead-end at run time). |

**`say`** — emit a templated message, no LLM call.

| Property | Type | Req? | Meaning |
|---|---|---|---|
| `message` | string (Jinja) | ✓ | The text to send. |
| `next` | node id | ✓ | Where to go next. |

**`subgraph`** — invoke another graph.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `graph` | graph id | ✓ | — | The graph to run. |
| `inputs` | map<callee input,scalar> | — | `{}` | Callee input ← caller expression/literal. |
| `outputs` | map<caller field,callee output> | — | `{}` | Caller state ← callee output name. |
| `next` | node id | ✓ | — | Where to go when it returns. |

**`handoff`** — build a packet and suspend until a human resumes/closes.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `reason` | string | ✓ | — | Why it's going to a person (rides on the packet; triages the queue). |
| `message` | string (Jinja) | — | core default | What to tell the customer at handover. |
| `next_steps` | list<string> | — | `[]` | Suggested actions for the human, overriding core's reason-keyed checklist. |
| `edges` | map | ✓ | — | Exactly `resumed` and `closed` → target node ids. |

**`end`** — pop the frame, return outputs.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `outputs` | map<name,scalar> | — | `{}` | Declared outputs → literal/expression. |

---

## Forms — `form:` on an `ask`/`confirm` node

Model: `support_core.graph.forms`. Every field key is unique across the whole form.

**`FormSchema`**

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `title` | string | ✓ | — | Form heading. Non-empty. |
| `intro` | string | — | `null` | Text under the title. |
| `submit_label` | string | — | `Submit` | Submit-button text. |
| `sections` | list<FormSection> | ✓ | — | ≥ 1 section. |

**`FormSection`**: `title` (string, ✓, non-empty) · `fields` (list<FormField>, ✓, ≥ 1).

**`FormField`**

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `key` | string | ✓ | — | Field key; pattern `^[a-z][a-z0-9_]*$`; unique across the form. |
| `label` | string | ✓ | — | Visible label. Non-empty. |
| `type` | enum | — | `text` | `text` / `email` / `date` / `select` / `radio` / `checkbox` / `checklist`. |
| `required` | bool | — | `false` | Whether a value is required. |
| `hint` | string | — | `null` | Helper text under the field. |
| `pattern` | regex | — | `null` | Value must match; **compiled at load** (a broken regex fails startup). |
| `options` | list<FormOption> | — | `[]` | **Required** for `select`/`radio`/`checklist`; **forbidden** otherwise. |
| `show_if` | FormCondition | — | `null` | Show only when another field equals a value. |
| `required_if` | FormCondition | — | `null` | Required only when another field equals a value. |

**`FormOption`**: `value` (string, ✓, non-empty) · `label` (string, ✓, non-empty).
**`FormCondition`**: `field` (other field's key, ✓) · `equals` (string, ✓).

---

## `tools/__init__.py` + `tools/*.py`

`tools/__init__.py` must export `TOOLS: list[Tool]`. Each tool is a `FunctionTool` (or a `Tool`
subclass). Model: `support_core.tools.base`.

**`Tool` / `FunctionTool`**

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `name` | string | ✓ | — | Tool name; pattern `^[a-z][a-z0-9_]*$` (used in YAML, prompts, approval hash). |
| `description` | string | ✓ | — | Shown to the model (neutralised like all pack text). Non-empty. |
| `input_model` | Pydantic model | ✓ | — | Argument schema (`extra="forbid"`). |
| `output_model` | Pydantic model | ✓ | — | Return schema. |
| `risk` | Risk | ✓ | — | `read` / `write` / `high` (see below). |
| `idempotent` | bool | — | `true` | `false` = at-most-once: never replayed after a crash. |
| `requires_human_approval` | bool | — | `false` | A second, human, approval (for `high` tools). |
| `confirm_exempt` | bool | — | `false` | WRITE only: no `confirm` node needed. Requires `confirm_exempt_reason`. |
| `confirm_exempt_reason` | string | — | `null` | Required iff `confirm_exempt`; listed in the validator report. |
| `patches_context` | set<string> | — | `∅` | `CustomerContext` fields this tool may change (e.g. `identity_verified`). READ tools may not declare any; unknown field names fail at load. |
| `timeout_s` | float | — | `15.0` | > 0. Call timeout. |
| `async_` | bool | — | `false` | Long-running: the `tool` node suspends `waiting_async_tool`. |
| `handler` | coroutine | ✓ (FunctionTool) | — | `async (input_model, ctx) -> output_model \| mapping`. |

**`Risk`** (`support_core.tools.risk.Risk`): `read` (no side effects; callable from an `llm` loop; no
confirm) · `write` (side effect; needs confirm unless `confirm_exempt`; never from an `llm` loop) ·
`high` (money/access/irreversible; always needs confirm; may need human approval; never from an `llm`
loop).

**`ToolContext`** (what a handler receives besides its input): `idempotency_key`, `conversation_id`,
`run_id`, `step_id`, `node_id`, `frame_seq`, `risk`, `customer` (a `CustomerContext` — verified or
not), `channel` (default `web_chat`), `approval` (the authorising record, if any). A WRITE/HIGH tool
may call `ctx.patch_customer(**fields)` to change the customer context (only for declared
`patches_context` fields).

### MCP tools (`support_core.tools.mcp`)

Build with `McpToolAdapter(client, risk_map, ...)` then `.discover()` (async) or `wrap_tools(...)`
(sync); add the results to `TOOLS`.

| Adapter arg | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `client` | McpClient | ✓ | — | Object with `list_tools()` and `call_tool(name, args)`. |
| `risk_map` | map<tool name, Risk> | ✓ | — | Tier per tool. **A tool not named here is `HIGH`.** |
| `prefix` | string | — | `""` | Prepended to each local tool name (namespacing). |
| `timeout_s` | float | — | `15.0` | Per-call timeout. |
| `idempotent` | map<tool name,bool> | — | `{}` | Overrides; default is idempotent only for READ. |
| `confirm_exempt` | map<tool name,reason> | — | `{}` | WRITE-only exemptions, with a reason. |

MCP results come back as **`McpOutput`**: `content` (string, verbatim server text) · `is_error`
(bool) · `structured` (dict or `null`). Treated as untrusted data — fenced in the prompt, never
instructions.

---

## `knowledge/sources.yaml`

Model: `support_core.knowledge.sources.KnowledgeSources`. A missing file = no sources.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `documents` | list<DocumentSource> | — | `[]` | Corpora to index. |
| `knowledge_graph` | list<KnowledgeGraphSource> | — | `[]` | Entity/relation sources (parsed now, loaded in a later phase). |
| `live_lookups` | list<LiveLookup> | — | `[]` | READ tools the retriever may call for dynamic facts. |

A `DocumentSource` is discriminated by `type`:

**`markdown_dir`** (`MarkdownDirSource`)

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `id` | string | ✓ | — | Source id; pattern `^[a-z][a-z0-9-]*$` (dashes); unique across all sources. |
| `type` | `"markdown_dir"` | ✓ | — | Discriminator. |
| `path` | string | ✓ | — | Directory relative to the pack; absolute or escaping paths refused. |
| `refresh` | enum | — | `manual` | `manual` / `hourly` / `daily` / `weekly` (scheduler hint). |

**`html_crawl`** (`HtmlCrawlSource`)

| Property | Type | Req? | Default | Meaning / constraints |
|---|---|---|---|---|
| `id` | string | ✓ | — | As above. |
| `type` | `"html_crawl"` | ✓ | — | Discriminator. |
| `url` | string | ✓ | — | Start URL; must be `http`/`https`. |
| `depth` | int | — | `1` | 1–3. Links deep to follow (start page = 1). |
| `max_pages` | int | — | `50` | 1–2000. Crawl ceiling. |
| `refresh` | enum | — | `daily` | As above. |

**`KnowledgeGraphSource`**: `id` (✓) · `type` `"yaml"` (✓) · `path` (file in the pack, ✓).

**`LiveLookup`**

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `tool` | tool name | ✓ | — | A READ tool the retriever may call. |
| `triggers` | list<string> | — | `[]` | Lowercase terms; the lookup fires when the query contains one. Empty = every query. |
| `args` | map<string,`ctx.` expr> | — | `{}` | Arg name → a `ctx.` expression (only `ctx`, never `state` or the query text). |

### `KnowledgeQuery` — the `knowledge:` block on an `llm` node

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `query` | string (Jinja) | ✓ | — | Retrieval query; may interpolate state (`"refund policy {{ state.denial_reason }}"`). |
| `k` | int | — | `3` | 1–20. Passages to retrieve. Land in prompt layer 7, each keeping its id for citation. |

---

## `persona.md` and `policies.md`

Plain Markdown, no schema. They fill **prompt layer 2 (persona)** and **layer 3 (policies)**
respectively (see the guide, §6). Keep both tight — layers 1–5 are the prompt's contract and are
never silently truncated; over budget (`llm.prompt_budget.persona` / `.policies`) is an error that
becomes a handoff. Everything in them is neutralised like all pack text.

- **`persona.md`** — voice and tone: who the assistant is, how it addresses the customer, length.
- **`policies.md`** — hard behavioural rules (what never to say or do). The citation rule here pairs
  with the `guardrails.citations` structural check.

---

## `ui/` — the pack's own front end (optional)

Not schema-validated (they're static assets), but they have a contract:

- **`ui/index.html`, `ui/app.js`** (and any assets) are served at `/app` when
  `AppConfig.serve_client` is on and `pack.yaml`'s `ui.dir` points here.
- The client reads its branding (`title`, `subtitle`, `accent`, `suggestions`) from
  `GET /channels/ag_ui` — so a pack can be themed with **no assets at all**, via the `ui:` block.
- It drives the conversation over the AG-UI SSE endpoint and renders `render_form` tool calls as
  forms. See `packs/acme_billing/ui/` for a self-contained, no-build example.

---

## Deployment config (`SUPPORT_APP_CONFIG` JSON)

Not part of the pack, but it's what selects and runs one. Model: `support_core.api.config.AppConfig`.
A JSON file named by the `SUPPORT_APP_CONFIG` env var, with env overrides.

| Property | Type | Req? | Default | Meaning |
|---|---|---|---|---|
| `pack` | path | — | `packs/acme_billing` | The pack to serve (relative to cwd). |
| `provider` | enum | — | `auto` | `auto` / `replay` / `anthropic` / `glm` / `none`. `auto` = live if an API key, else `replay` if cassettes, else `none`. |
| `models` | ModelOverride | — | `{}` | `default` / `escalation` model-id overrides (keep the vendor out of the pack). |
| `cassette_dir` | path | — | `null` | Recorded responses for `replay`; an unseen request is refused. |
| `lock_wait_seconds` | float | — | `0.0` | ≥ 0. How long a handler waits for the conversation lock (0 = queue and return). |
| `max_concurrent_turns` | int | — | 26 | 1–256. Turns per replica (also sizes the DB pool). |
| `drain_workers` | int | — | `4` | 1–64. Background drain workers. |
| `drain_budget_seconds` | float | — | `60.0` | > 0. How long the drain worker retries a locked conversation. |
| `new_conversation_context` | object | — | `{}` | The `ctx` a new conversation starts with (the CRM lookup in production). **May never contain `identity_verified`.** |
| `suggestions` | list<string> | — | `[]` | One-click starters (with `replay`, these are the recorded conversations). |
| `serve_client` | bool | — | `true` | Serve the built-in demo page at `/` and the pack UI at `/app`. |
| `serve_desk` | bool | — | `false` | Serve the human desk API at `/desk`. **Requires `desk_token` or startup fails.** |
| `desk_token` | string | — | `null` | Desk bearer token; ≥ 16 chars. Secret — supply via `SUPPORT_DESK_TOKEN`. Never echoed. |
| `qdrant_url` | string | — | `null` | Vector store URL (`null` = `SUPPORT_QDRANT_URL` / compose default). |
| `use_qdrant` | bool | — | `true` | Whether this deployment has a Qdrant at all (off = Postgres retrieval only, a configuration, not a fault). |
| `handoff_webhook_url` | string | — | `null` | A second sink for handoff packets, beside the Postgres queue. |
| `transcript_url_template` | string | — | `/desk/conversations/{conversation_id}/transcript` | What a packet's `transcript_url` points at. |
| `title` | string | — | `support-core` | Deployment title. |

**Environment overrides**: `SUPPORT_APP_CONFIG` (the file), `SUPPORT_PACK`, `SUPPORT_LLM_PROVIDER`,
`SUPPORT_CASSETTE_DIR`, `SUPPORT_MODEL`, `SUPPORT_ESCALATION_MODEL`, `SUPPORT_DESK_TOKEN`,
`SUPPORT_QDRANT_URL`, `SUPPORT_MAX_CONCURRENT_TURNS`. Secrets stay in the environment: the database
URL and `ANTHROPIC_API_KEY` are never in this file.
