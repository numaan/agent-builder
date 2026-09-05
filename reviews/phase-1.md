# Phase 1 review: Graph model, loader, validator, expression language

Design references: DESIGN.md sections 5.2, 6.1 to 6.4, 6.7 (principles from 3 and 8.2).
Backlog: BACKLOG.md "Phase 1". Inherits the settled decisions in reviews/phase-0.md.

## Plan

Written before any code, per PLAN.md step 1.

### Task breakdown

1. **Expression language** (`support_core/graph/expr/`), the highest-risk item. A hand-written
   tokenizer, a recursive-descent parser producing a frozen AST, and then two independent
   walkers over that AST: an evaluator and a static type checker. No `eval`, no `exec`, no
   `compile`, no `ast.literal_eval`. Grammar:

   ```
   expression := or_expr
   or_expr    := and_expr ("or" and_expr)*
   and_expr   := not_expr ("and" not_expr)*
   not_expr   := "not" not_expr | comparison
   comparison := unary (("==" | "!=" | "<" | "<=" | ">" | ">=") unary)?   # non-associative
   unary      := "-" unary | postfix
   postfix    := primary ("." NAME | "|" filter)*
   primary    := "(" expression ")" | literal | ROOT
   filter     := NAME ("(" literal ("," literal)* ")")?
   ROOT       := "state" | "ctx" | "result"
   ```

   Filters bind tighter than comparisons and looser than attribute access, as in Jinja, so
   `state.x | len > 3` is `(state.x | len) > 3`. Filter arguments are literals only.
   Comparison chaining (`a < b < c`) is rejected rather than silently given Python semantics.
   Every failure is a `ParseError` carrying the offending token text and its character
   position. Guard rails against pathological input: maximum source length, maximum token
   count, and a maximum nesting depth so `"(" * 10000` is a `ParseError`, not a
   `RecursionError`.

2. **Type strings and state schemas** (`support_core/graph/types.py`). Graph files declare
   `state:`, `inputs:` and `outputs:` as `name: <type string>` (DESIGN 6.4). A second small
   recursive-descent parser turns `str | None`, `list[str]`, `dict[str, int]`,
   `Literal["a", "b"]` into real annotations, and `pydantic.create_model` builds a real
   Pydantic model per graph. Unknown names (`Charge`, a model exported by pack tools) become
   an opaque placeholder typed `Any` plus a warning finding, because pack tools are stubs
   until phase 4.

3. **Node type registry** (`support_core/graph/nodes.py`). One `NodeTypeSpec` per type
   holding: the Pydantic config model for that node's YAML, `executable`, `suspends`,
   `chooses_edge`, and which fields are edge targets. `router`, `say`, `end`, `subgraph`
   are executable; `llm`, `ask`, `tool`, `gate`, `confirm`, `handoff` are declared but not
   executable. The registry is the single place a node type is defined: the schema, the
   validator, the CLI and the test stepper all read it.

4. **Graph schema and loader** (`support_core/graph/schema.py`, `loader.py`). Pydantic model
   for a graph file (`id`, `description`, `inputs`, `outputs`, `state`, `start`, `nodes`);
   `load_pack(path)` reads `pack.yaml`, every graph, `persona.md`, `policies.md` and the tool
   manifest, resolves sub-graph references, runs the validator, and raises on any error
   finding. Edges live inside node definitions in DESIGN 6.4, so the backlog's "edges" key is
   satisfied by the per-node `edges` / `next` / `on_error` / `redirect` fields.

5. **Tool manifest** (`support_core/graph/tools_manifest.py`). A minimal declarative
   `tools/tools.yaml` giving each tool a name, risk tier, input and output field types, and
   the `confirm_exempt` / `idempotent` / `requires_human_approval` / `async` flags from
   DESIGN 8.1 and 8.2. Phase 4 replaces it with the real registry built from the imported
   `TOOLS` list; the manifest exists now so the confirm-on-all-paths rule can be real.

6. **Templates** (`support_core/graph/templates.py`). `jinja2.sandbox.SandboxedEnvironment`
   with `StrictUndefined`, globals cleared, and exactly the four expression-language filters.
   At load time every template is parsed and its Jinja AST walked: root names must be
   `state`/`ctx`/`result`, filters must be in the allowed set, and every attribute chain is
   type-checked against the graph's state model with the same type checker the expression
   language uses.

7. **Validator** (`support_core/graph/rules.py`, wired through the existing
   `validate_graphs` hook in `validator.py`). Every rule in DESIGN 5.2 with a stable dotted
   rule id in the phase-0 style, plus the DESIGN 8.2 approval-binding rules. The
   confirm-on-all-paths rule is a real interprocedural forward dataflow analysis, not a
   heuristic: a must-analysis whose value at a node is the set of `confirm` nodes that
   certainly cover it, cleared by every `ask` node and by graph entry, intersected over
   predecessors, iterated to a fixpoint over a graph that inlines `subgraph` calls and `gate`
   redirects as (graph id, node id) pairs.

8. **Version pinning** (DESIGN 6.7). `PackPin` on the loaded pack: pack id, pack version,
   and per graph a content hash and a state-shape hash, so a future engine can pin a
   conversation to a graph version and detect an incompatible state shape. Data only.

9. **CLI**. `support pack validate` gains node ids in finding locations
   (`graphs/refund.yaml:issue_refund`). The phase-0 output contract (rule ids printed,
   summary line, exit codes, `--strict` changes only the exit status) is unchanged.

10. **Tests**. Hypothesis property tests for the parser (no input crashes with anything but
    `ParseError`), a failing fixture per validator rule, a loader round-trip over a realistic
    pack, and the exit-criterion stepper test.

### Intended deviations from DESIGN.md and why

- **DESIGN 6.4 does not give a key name for `say`, `subgraph` mappings or `end` outputs.**
  Chosen: `say: {message: <template>, next: X}`; `subgraph: {graph, inputs, outputs, next}`
  where `inputs` maps the callee's input name to a caller expression and `outputs` maps the
  caller's state field to the callee's output name; `end: {outputs: {name: <literal or
  expression>}}` exactly as in the 6.4 example.
- **Expression versus literal ambiguity.** DESIGN 6.4 writes both
  `into: { eligible: result.eligible }` (expression) and `into: { outcome: "refunded" }`
  (literal) in the same key. Rule adopted: a scalar starting with `state`, `ctx` or `result`
  followed by a word boundary must parse as an expression; a scalar that merely *looks* like
  one (contains a comparison, a boolean operator, a filter pipe, or a dotted lowercase
  identifier) but does not start with a root is an error (`expr.looks_like_expression`) so a
  typo such as `stat.charge_id` cannot silently become the string `"stat.charge_id"`;
  anything else is a literal.
- **`packs/acme_billing` keeps no graphs.** The backlog does not ask for pack graphs in this
  phase and phases 3 and 4 own the node types those graphs need. The realistic pack that
  exercises the loader and the validator, including the DESIGN 6.4 refund graph, lives under
  `tests/packs/`. This keeps the phase-0 exit criterion (`empty but well-formed`) true.
- **`support_core/__init__.py` now exports `load_pack`**, which phase 0 deliberately did not
  stub. The rest of the section 18 public API still does not exist.
- **The in-memory stepper is a test utility**, at `tests/stepper.py`, clearly marked. Phase 2
  owns the real executor. Only genuinely reusable pieces (expression evaluation, template
  rendering, the node registry) live in `support_core`; the frame-stack loop does not.
- **`hypothesis` is added to the dev extras** for the parser property tests.
- **Two rules are additions, not DESIGN 5.2 text**: `graph.node_unreachable` (warning) and
  the DESIGN 8.2 approval-binding rules (`graph.approval_missing`,
  `graph.approval_mismatch`, `graph.approval_unreachable`). They are cheap and they protect
  the one rule PLAN.md says reviewers check on every phase.

## Implementation notes

(Filled in after the code.)

## Self-critique

(Filled in after the code.)
