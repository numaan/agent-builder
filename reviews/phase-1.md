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

Environment: Windows 11, Python 3.13.14, pydantic 2.13.5, jinja2 3.1.6, PyYAML 6.0.3,
hypothesis 6.167.1 (added to the dev extras for this phase). The database is untouched by
phase 1 but the suite as a whole still needs it, so `support_test` stayed up throughout.

Commands run at the end of the phase, from the repository root with `.venv/Scripts/python.exe`:

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` |
| `python -m ruff format --check .` | `58 files already formatted` |
| `python -m mypy` (strict) | `Success: no issues found in 58 source files` |
| `python -m pytest -q` | `344 passed in 6.8s` (77 of them inherited from phase 0) |
| `support pack validate packs/acme_billing` | `acme-billing: empty but well-formed`, exit 0 |
| `support pack validate tests/packs/refund_pack` | `refund-pack: well-formed (5 warning(s))`, exit 0 |
| `python -m alembic upgrade head && python -m alembic check` | `No new upgrade operations detected.` |

### Shape of the code

```
support_core/graph/
  expr/          lexer.py, parser.py, syntax.py, filters.py, evaluate.py, typecheck.py
  types.py       type strings -> annotations -> Pydantic models (no eval)
  nodes.py       the node type registry (the single definition of a node type)
  templates.py   sandboxed Jinja, translated into the expression AST and type-checked
  schema.py      graph file schema, per-node parsing, literal-versus-expression rule
  tools_manifest.py  declarative tools/tools.yaml (phase 4 replaces it)
  rules.py       every DESIGN.md 5.2 rule plus the 8.2 approval binding
  pack.py        Pack and PackPin (6.7)
  loader.py      load_pack
  findings.py    Severity/Finding/ValidationReport, moved out of validator.py
  validator.py   phase 0's layout checks; validate_graphs now calls schema + rules
support_core/tools/risk.py   the Risk tiers, so phase 4 does not have to move them
tests/stepper.py             TEST UTILITY: the in-memory stepper for the exit criterion
tests/packs/refund_pack      the DESIGN.md 6.4 workflow, as a positive fixture
tests/packs/deterministic_pack  router/say/subgraph/end only, for the stepper
```

### Rule ids implemented

Graph structure: `graph.unreadable`, `graph.invalid_yaml`, `graph.invalid`,
`graph.duplicate_id`, `graph.id_mismatch`, `graph.start_missing`, `graph.no_end`,
`graph.node_type_unknown`, `graph.node_invalid`, `graph.node_id_invalid`,
`graph.edge_target_missing`, `graph.edges_incomplete`, `graph.node_unreachable` (warning),
`graph.router_no_default` (warning), `graph.router_predicate_not_bool` (warning),
`graph.gate_predicate_not_bool` (warning), `graph.unsuspended_cycle`, `graph.subgraph_cycle`.

Declarations: `graph.state_type_invalid`, `graph.state_type_unresolved` (warning),
`graph.state_field_invalid`, `graph.state_field_not_optional` (warning),
`graph.input_not_in_state` (warning).

Tools and safety: `graph.tool_unknown`, `graph.tool_arg_unknown`, `graph.tool_arg_missing`,
`graph.tool_into_invalid`, `graph.llm_tool_unknown`, `graph.llm_tool_not_read`,
`graph.llm_output_schema_invalid`, `graph.llm_output_not_in_state` (warning),
`graph.confirm_action_tool_unknown`, `graph.confirm_action_is_read` (warning),
`graph.unconfirmed_write`, `graph.approval_missing`, `graph.approval_unknown`,
`graph.approval_mismatch`, `graph.approval_unreachable`, `graph.approval_not_needed` (info),
`graph.confirm_exempt` (info), `tools.high_risk_exempt`, `tools.manifest_invalid`,
`tools.declaration_unresolved` (warning).

Sub-graphs and outputs: `graph.subgraph_unknown`, `graph.gate_redirect_unknown`,
`graph.subgraph_input_unknown`, `graph.subgraph_input_missing`, `graph.subgraph_output_unknown`,
`graph.subgraph_output_field_unknown`, `graph.subgraph_output_type`, `graph.end_output_unknown`,
`graph.end_output_missing`, `graph.ask_slot_unknown`.

Expressions and templates: `expr.parse_error`, `expr.type_error`, `expr.looks_like_expression`,
`expr.optional_attribute` / `expr.optional_filter_input` / `expr.optional_comparison` (warnings),
`template.syntax_error`, `template.unsupported`, `template.type`,
`graph.assignment_optional` (warning).

Cross-file: `manifest.interrupt_graph_unknown` (warning), `graph.node_not_executable` (info).
Phase 0's `layout.*`, `manifest.*`, `knowledge.*`, `policies.*`, `tools.no_export` and
`pack.empty` are unchanged; `graph.not_validated` is gone, replaced by real validation.

### Things worth knowing that came up while building

- **YAML 1.1 reads a bare `yes:` / `no:` key as a boolean**, and DESIGN.md 6.4 writes confirm
  edges exactly that way. Rather than making pack authors quote them, `_normalise_confirm_edges`
  translates `True`/`False` keys back before validation. A test pins both spellings.
- **PyYAML recurses once per nesting level.** `a: [[[[...` twenty thousand deep raised
  `RecursionError` out of `validate_pack`, whose documented contract is a report, not an
  exception. Every YAML load site now catches it and reports `*.invalid_yaml`. The alias
  ("billion laughs") variant is harmless here because the expanded document still has to be a
  graph mapping, which it is not.
- **A HIGH tool marked `confirm_exempt` escaped the confirm rule entirely** in my first cut.
  DESIGN.md 8.2 offers the exemption for WRITE ("such as sending a one-time passcode"), not for
  "money, access, irreversible". `needs_confirm` now ignores the flag for HIGH and the
  declaration itself is an error.
- **The `parse_type` results feed real `pydantic.create_model` calls**, so a graph's state is a
  genuine model: the expression type checker reads `model_fields`, and the stepper constructs
  and mutates instances. Unknown names such as `Charge` become `Any` with a warning.
- **`unparse` is the canonical form used to compare a confirm's declared arguments with the tool
  node's** (DESIGN.md 8.2 hashes the arguments). A hypothesis property test asserts that
  `unparse` is idempotent under re-parsing, which is what makes that comparison trustworthy.
- **The confirm analysis needed two lattices, not one.** A single set-valued must-analysis
  reports "no confirm covers this" when two different confirms cover two different branches,
  which is a real but *different* defect. The boolean lattice answers DESIGN.md 5.2 ("is there
  a confirm on every path") and the set answers 8.2 ("is *this* approval on every path"), so the
  two cases now get different rule ids.

## Self-critique

Written after re-reading DESIGN.md 3, 5.2, 6.1 to 6.4, 6.7 and 8.2, and PLAN.md.

### What did I skip or simplify?

- **Custom node types.** DESIGN.md 6.2 ends with "Custom node types are Python classes
  registered by name in the pack", and section 5 has an optional `nodes/` directory. Phase 1
  has no mechanism for that: an unknown `type:` is `graph.node_type_unknown`. Registering pack
  Python means importing pack code, which is phase 4's job (the same import that builds the tool
  registry). Not in the backlog for this phase, but it is a real gap in the "registry is the
  single place a node type is defined" claim: today the registry is also a closed set.
- **The type system is coarse.** It knows none, bool, number, str, model, collection and
  "other". `int` and `float` are one category, so assigning a float expression into an `int`
  field passes. A union of two real types (`str | int`) degrades to unknown rather than being
  checked. `list[str]` versus `list[int]` is not distinguished. `Decimal` is accepted as a
  number but nothing produces one yet.
- **`into: state.charge` (the string form) is not type-checked.** The validator confirms the
  target is a declared state field and stops; it does not compare the tool's output model with
  the field's type. The mapping form (`into: { eligible: result.eligible }`) *is* checked. In
  the reference pack this is exactly where `Charge` is opaque anyway, which hid it from me for
  longer than it should have.
- **`money` has no currency and no locale.** It renders `1,234.50`. DESIGN.md fixes neither and
  section 21 leaves multi-language open, but a customer-facing refund message that omits the
  currency symbol is not shippable; whichever phase adds per-pack currency config owns it.
- **Filter arguments are literals only**, and `default` has no "treat falsy as missing" mode.
  Both are deliberate, both are narrower than Jinja.
- **`inputs` versus `state` is my reading, not the design's.** DESIGN.md 6.4 declares `inputs`
  and `state` separately and never says how a node reads an input. I chose "an input lands in
  the state field of the same name", warned (`graph.input_not_in_state`) when there is no such
  field, and added `charge_hint` to the reference refund graph's state so the design's own
  example is coherent. Phase 2 owns the real answer and may overrule this.
- **No `nodes/`, no evals, no knowledge validation beyond phase 0's section check.**
- **The `graphs/` directory is flat.** A pack cannot organise graphs into sub-directories,
  because the entry-graph lookup and `graph.id_mismatch` both assume `graphs/<id>.yaml`.

### Where does the code diverge from the design?

- **Key names DESIGN.md does not give.** `say` uses `message`; `subgraph` uses
  `graph`/`inputs`/`outputs`/`next` with `outputs` mapping *caller state field to callee output
  name*; `router` gained an optional `default`. All are additions, none contradicts the
  document, but a reviewer should confirm the `subgraph.outputs` direction reads naturally,
  because the opposite direction is equally defensible and silently different.
- **Literal versus expression.** DESIGN.md 6.4 uses one key for both (`into: { eligible:
  result.eligible }` and `into: { outcome: "refunded" }`). My rule: a scalar starting with a
  root must parse as an expression; a scalar that merely resembles one is an error rather than
  a silent literal; anything else is a literal. This is a language decision the design does not
  make, and it can still be wrong in both directions (below).
- **`router` evaluation order.** DESIGN.md does not say what happens when two branches are true
  or none is. I chose "first declared branch wins" (so `edges` ordering is significant, which
  YAML preserves) and warn when there is no `default`. The stepper raises rather than guessing.
- **Two rule families are mine, not the design's text**: `graph.node_unreachable` and the
  approval-binding trio derived from 8.2. `graph.confirm_action_is_read` is a judgement call
  (a confirmation the customer cannot meaningfully refuse trains them to say yes) and is only
  a warning.
- **`support_core/__init__.py` now imports `load_pack` after `__version__`.** That is a real
  ordering dependency: `support_core.graph.manifest` reads `__version__` from the
  partially-initialised module. It works and is commented, but it is fragile and a reviewer
  should say whether they would rather move `__version__` into its own module.
- **`Risk` lives in `support_core/tools/risk.py`** although phase 4 owns `tools/`. Putting the
  enum where it belongs now avoids a move later; phase 4 must extend that file, not replace it.

### Which validator rules are weakest, and what could a hostile pack author still sneak past?

Ordered by how much I would worry.

1. **The approval-argument comparison is textual, not semantic.** `graph.approval_mismatch`
   compares the canonical source of the confirm's `action.args` with the tool node's `args`.
   Identical text that evaluates differently passes: put a `tool` node between the confirm and
   the call that rewrites `state.charge.amount`, and the validator sees
   `{amount=state.charge.amount}` on both sides and is happy. Only the run-time hash check
   (phase 4, DESIGN.md 8.2) catches that, which is precisely the gap 8.2 says the hash exists to
   close. A "no state write to any field the approved arguments read, between the confirm and
   the call" rule is implementable with the same dataflow machinery and is the single most
   valuable thing to add next.
2. **`confirm_exempt` on a WRITE tool is an unchecked escape hatch.** It is reported as INFO,
   as DESIGN.md 8.2 asks, but INFO does not fail `--strict` and nothing forces a human to look.
   A pack that marks `charge_card` as WRITE + `confirm_exempt` gets no error. (HIGH is now
   blocked, see above.) Options for the reviewer: make the exemption a WARNING, or require an
   explicit `confirm_exempt_reason` string.
3. **Risk tiers are self-declared data.** The whole confirm rule rests on `tools/tools.yaml`,
   which the pack author writes. Declaring `issue_refund` as `read` removes every check *and*
   makes it callable from an `llm` node's tool loop. Phase 4 must make the imported `TOOLS`
   authoritative and report drift; until then the rule protects against mistakes, not malice.
   This is the biggest single caveat of the phase.
4. **The interprocedural analysis is context-insensitive**, and it does not model interrupts at
   all. Return edges go from a callee's `end` to *every* call site, which is conservative for
   the confirm rule (more predecessors can only shrink the covered set) but can produce a false
   `graph.unconfirmed_write` for a sub-graph called from both a confirmed and an unconfirmed
   site. More seriously, DESIGN.md 6.6 lets a customer interrupt a suspended frame and return to
   it later; those push and resume edges are not in my control-flow graph, so an approval given
   before an interrupt is assumed still to hold afterwards. Phase 6 owns interrupts and must
   revisit this rule, not only the engine.
5. **A `handoff` does not clear the approval set.** A human agent can act between the confirm
   and the call. I treated only `ask` and `confirm` as customer input, which is literally what
   5.2 says, but "last customer input" is arguably the wrong boundary once a human is involved.
6. **Unreachable code is not analysed for confirms.** The coverage analysis only visits points
   reachable from an entry, so a WRITE call behind an unreachable node is a
   `graph.node_unreachable` warning and nothing else. That is defensible (it cannot execute) but
   it means a warning-only finding is load-bearing.
7. **The literal-versus-expression heuristic can still be wrong both ways.** `outcome: refunded`
   is a literal, `outcome: stat.charge_id` is an error, but `outcome: state` (a bare root) parses
   as an expression and types as the state model. And a literal containing ` or ` or ` and `
   (`"call us and wait"`) is pushed through the parser and, on failure, reported as
   `expr.looks_like_expression` - a false positive that an author can only resolve by rewording.
   It is at least always an error and never a silent misreading.
8. **Template validation covers only `{{ }}` and `{% if %}`.** That is by construction (loops,
   `set`, includes and calls are rejected), but the rejection list is a denylist inside
   `convert`: a Jinja node type I did not think of falls through to the generic "not supported"
   arm, which is the right default, and I confirmed the obvious escapes
   (`__class__`, `lipsum`, `cycler`, `range`, `self`) are rejected at load time. The sandboxed
   environment with cleared globals and filters is the second lock.
9. **`graph.unsuspended_cycle` is intra-graph only.** Cross-graph loops get the coarser
   `graph.subgraph_cycle`, which only fires when *no* graph in the cycle contains any suspending
   node anywhere - not "on the path". A mutual recursion that passes through a graph with an
   unrelated `ask` in a different branch is missed.
10. **`graph.no_end` does not check that an `end` is reachable**, only that one exists, and
    nothing checks that a graph declaring `outputs` has an `end` producing them on every path
    (only per-end-node completeness). A graph can therefore terminate without producing declared
    outputs if some end node is unreachable.

### Which tests are weak?

- **The property tests are shallow in the good direction.** They prove the parser, the type
  checker and the evaluator never raise the wrong exception type, and that `unparse` round-trips
  - but the generators are biased towards short strings from a fixed alphabet, so they mostly
  explore rejection paths. There is no property asserting that a parsed expression *means* what
  it should; the evaluation examples are hand-written cases only.
- **No differential test against Python semantics.** For the subset the languages share
  (comparisons, boolean logic, literals) I could evaluate the same expression with `eval` in a
  test-only harness and assert agreement. That would have caught, for example, a wrong
  short-circuit value in `and`/`or`. I did not write it.
- **The confirm analysis has six behavioural tests but no property test.** A generator that
  builds random small graphs and cross-checks the dataflow result against a brute-force
  enumeration of all paths up to a depth bound would be a much stronger guarantee, and is the
  test I would write first if I had another hour. As it stands, a fixpoint bug that under-reports
  on a graph shape I did not think of would pass.
- **The stepper tests exercise one pack and seven node visits.** They pin the exit criterion and
  nothing more. They do not test nested sub-graphs more than one deep, a sub-graph invoked twice,
  or output mapping collisions.
- **Nothing tests two pack versions loaded side by side**, because nothing loads two pack
  versions; `PackPin` is tested as a data structure only (hashes, comparison, order
  independence), which is all phase 1 promised.
- **`test_reference_pack_reports_the_expected_warnings_and_notices` asserts an exact warning
  set**, so any new warning rule breaks it. That is deliberate (a new warning on the design's own
  example should be a conscious decision) but it will annoy phase 3 and 4.
- **No test asserts the CLI exit code on a pack with graph errors** end to end; the CLI tests
  cover the well-formed and `--strict` paths and the loader tests cover the error path.

### What would break under concurrency or a crash mid-step?

Phase 1 has no runtime, no database and no shared mutable state, so the honest answer is
"nothing yet". Concretely:

- `validate_pack` and `load_pack` do file reads and pure computation. Two processes validating
  the same pack cannot interfere. Nothing is cached, so nothing goes stale.
- A crash mid-validation loses the report and nothing else. A crash mid-`load_pack` leaves no
  partial state anywhere; the `Pack` is only returned once every graph parsed.
- **The one shared object is `support_core.graph.templates.ENVIRONMENT`**, a module-level Jinja
  environment created at import. It is read-only after construction and Jinja environments are
  documented as thread-safe for rendering, but its template cache is shared process-wide. If a
  later phase mutates it per pack (adding a pack-supplied filter, say) that becomes a real
  concurrency bug. It should be per-pack from the moment anything varies.
- **Pack files are read outside any transaction.** A pack edited on disk while `load_pack` runs
  can produce a `PackPin` whose hashes describe a mix of two versions. DESIGN.md 6.7 relies on
  the pin identifying one coherent version, so phase 2's hot-reload must snapshot the directory
  (or read once and hash the same bytes it parsed) rather than re-reading.
- **The step limit in the stepper is a counter, not the pack's `max_nodes_per_turn`.** The real
  executor must use `manifest.limits` and hand off with `limit_exceeded` (DESIGN.md 7.3), which
  the stepper deliberately does not do.
- PLAN.md's standing rule - no WRITE or HIGH tool without an `ActionApproval` - holds trivially:
  no code path in phase 1 executes a tool at all, and the stepper refuses every node type that
  could (`NotExecutableError`, naming the phase).

### What I fixed while writing this

Three defects above were found by the self-critique and fixed in `074d092` rather than recorded:
the `RecursionError` escaping `validate_pack`, the HIGH-risk `confirm_exempt` escape, and the
`"Mr. Smith"` false positive in the literal heuristic. The rest stands as written.
