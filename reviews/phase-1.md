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

## Independent review

Reviewer: independent agent, 2026-09-05. Wrote none of the phase-1 code. Read DESIGN.md
sections 3, 5.2, 6.1 to 6.4, 6.7 and 8.2, PLAN.md, BACKLOG.md, reviews/phase-0.md, this file,
every module under `support_core/graph/`, `support_core/cli/`, `support_core/tools/`,
`tests/` and `packs/acme_billing`, and the five phase-1 commits (`917a450..0648ee9`).

### Verdict

Phase 1 is strong work and the exit criterion substantially holds: every command in the
implementation notes reproduces exactly (ruff, format, mypy strict, 344 tests green three times,
`support pack validate` on both packs, alembic check), the stepper really executes
`router`/`say`/`subgraph`/`end` with assertions on path, messages, outputs and determinism, and
the malformed fixtures are rejected with specific rule ids. **The expression sandbox held under
every attack I could construct**: 135 hand-written adversarial inputs plus 2000 hypothesis-generated
ones produced no unexpected exception type and no attribute outside a declared Pydantic model
field; dunders, calls, subscripts, unicode homoglyphs, null bytes, comment syntax, huge literals,
deep nesting and every Jinja escape I know (`__class__`, `attr()`, `lipsum`, `cycler`, `self`,
`include`, `set`, `for`) are all rejected, at load time by `convert`/the parser and again at run
time by the sandbox. **I did not break the confirm-on-all-paths analysis on any reachable path**:
a differential test of 400 random graphs against an independently written product-automaton
reachability agreed exactly, and hostile fixtures using a one-branch sub-graph confirm, mutual
recursion, a gate redirect, an `on_error` edge, an `ask` between confirm and call, two call sites
of one sub-graph, and an `end` returning past the confirm were all caught. Two things stop this
being a clean pass. `graph.approval_missing` and `graph.approval_unknown` together make a HIGH or
WRITE tool inside a sub-graph **impossible to validate** - the analysis blesses a caller-side
confirm and then the approval rules reject every spelling of it, and the implementer's own test
documents the contradiction. And five rule ids listed in "Rule ids implemented", two of them ERROR
severity, have no test asserting on them, so the backlog's "one failing fixture per rule" and the
exit criterion's "with the right rule name" are not true as delivered. Everything else is
should-fix or smaller.

### Findings

| id | severity | location | finding | suggested fix |
|----|----------|----------|---------|---------------|
| F1 | must-fix | `support_core/graph/rules.py:536` | A WRITE/HIGH tool node in a sub-graph cannot be made valid. `approval_binding` requires `requires_approval` and then requires it to name a `confirm` **in the same graph**; but `confirm_coverage` deliberately inlines calls so a caller-side confirm satisfies DESIGN 5.2. Omitting `requires_approval` gives `graph.approval_missing`; naming the caller's confirm gives `graph.approval_unknown` **and** a spurious `graph.approval_unreachable` (reproduced, case R). `test_a_confirm_in_the_calling_graph_covers_a_call_in_the_sub_graph` asserts exactly this unsatisfiable pair. DESIGN 8.2 never says the confirm must be in the calling graph, and the `covered` set is already keyed by `(graph id, node id)`, so the information needed to allow it is already computed. | Let `requires_approval` name `graph.node`, or resolve a bare id against `covered[point]`, which is already cross-graph: accept when the named confirm is in that set; keep `approval_unknown` for a name no graph defines. Add the sub-graph case to the confirm tests. |
| F2 | must-fix | `reviews/phase-1.md` "Rule ids implemented"; `tests/test_graph_validator.py` | Five listed rule ids have no test asserting on that id: `graph.end_output_unknown` and `graph.end_output_missing` (both ERROR - I verified by hand that they do fire), `graph.approval_not_needed`, `expr.optional_filter_input`, `expr.optional_comparison`. BACKLOG's checklist says "validator tests with one failing fixture per rule" and the exit criterion says "rejects each malformed fixture with the right rule name". | Add one fixture per id. Cheap; the two ERROR ones matter most, because nothing would notice if `end_outputs` regressed. |
| F3 | should-fix | `support_core/graph/rules.py:644`, `control_flow_graph` entries at `:745` | The coverage analysis visits only points reachable from an entry, and `entries` excludes any graph that appears in `returns` - so a graph called **only from an unreachable node** is checked by nothing. Case G2: an orphaned `subgraph` node in `root` calls `worker`, whose router routes around `worker`'s own `confirm` straight into a HIGH `issue_refund` that names it in `requires_approval`. Result: zero errors, `load_pack` succeeds, and the only signal is a `graph.node_unreachable` WARNING on a node in a different file. The rule set is inconsistent here: a graph *nobody* calls is treated as an entry and fully checked, while a graph called only from dead code is not checked at all. | Intersect `returns` with reachable call sites before computing `entries`, so a callee whose only call sites are dead becomes an entry again. (Making `graph.node_unreachable` an error would also work but is blunter.) |
| F4 | should-fix | `support_core/graph/rules.py:673-690` | A loop that re-enters a WRITE/HIGH tool after its confirm validates clean, so one `ActionApproval` authorises unbounded calls. Case E: `confirm_it --yes--> issue_refund --> handoff --resumed--> issue_refund`. `handoff` is a pass-through for both lattices (admitted, item 5) *and* suspends, so `graph.unsuspended_cycle` stays quiet too; the phase-4 hash check will also pass, because the arguments never change. This is phase-0 finding N1 (approvals are not single-use) made reachable from a statically valid graph. | Report a cycle that re-enters a `needs_confirm` tool node without passing its confirm - the CFG and the `covered` map already hold everything needed - and make phase 4's approval single-use. Cross-reference N1 in BACKLOG. |
| F5 | should-fix | `support_core/graph/expr/parser.py:189`, `MAX_DEPTH` at `parser.py:50` | `postfix` never calls `_descend`, so attribute and filter chains are the one AST shape `MAX_DEPTH = 32` does not bound; they are capped only incidentally by `MAX_TOKENS = 500`, at about 249 links. The recursive walkers then use **749 Python frames** for one such expression (measured), against a 1000-frame limit: `infer` raises `RecursionError` once ~500 frames of caller stack are already in place (measured). The parser docstring's "never raises `RecursionError`" holds today only because `MAX_TOKENS` happens to be 500 and the validator's stack is shallow; raising either limit, or calling from an async server stack, breaks it. | Count `postfix` links against `MAX_DEPTH` too (a 32-link attribute chain is already absurd), or make `walk`/`unparse`/`_infer`/`evaluate` iterative. Add a test that pins the frame cost. |
| F6 | should-fix | `support_core/graph/expr/syntax.py:139`; `tests/test_expressions.py:309-372` | `unparse` is not a canonical form, so the property the self-critique leans on ("`unparse` is idempotent under re-parsing, which is what makes that comparison trustworthy") is false. `parse("1e311")` unparses to `inf`, which does not re-parse; any string literal containing a non-printable character (`'\x07'`, U+202E, the NUL produced by `'\0'`) unparses to a `\x`/`\u` escape the lexer rejects. The existing hypothesis test misses this because `_SOURCE` draws short strings from `string.printable`, so it essentially never produces a float overflow or a control character inside quotes; adding `st.characters()` and an exponent builder falsified it in seconds. Impact today is bounded - `_canonical_args` applies the same transform to both sides - but two distinct argument texts (`1e400`, `2e400`) do compare equal. | Reject a numeric literal that overflows to `inf` in the lexer, and render string literals with an escape set the lexer accepts (or reject control characters in literals). Widen the property generator and keep the round-trip assertion. |
| F7 | should-fix | `support_core/graph/rules.py:1051` | `_canonical_args` classifies scalars by different rules than `parse_value`, which is what the engine will actually use. It pushes every string through `parse`, so `{amount: 100}` (a YAML int) and `{amount: "100"}` (a string literal to `parse_value`) canonicalise identically, as do `{note: true}` and `{note: "true"}`. A confirm and a tool node that disagree in exactly that way pass `graph.approval_mismatch` and then fail the phase-4 hash check at run time - the failure 8.2 exists to prevent statically. | Build the canonical form from `parse_value`: `unparse(expression)` when it is an expression, `repr(literal)` otherwise. |
| F8 | should-fix | `support_core/graph/expr/typecheck.py:251` vs `evaluate.py:141-146` | The evaluator traverses `Mapping`s but the type checker refuses to, so any expression through a dict is a load-time error and the evaluator's mapping support is unreachable dead code. Concretely `ctx.customer.attributes.<anything>` - the CRM record DESIGN 10 says the context carries - can never appear in a graph. | Type a `dict[str, X]` read as `X` (or as unknown) to match the evaluator, or drop mapping traversal from the evaluator so the two walkers agree. Decide before phase 3 writes prompts against `ctx`. |
| N1 | nit | `support_core/graph/templates.py:90` | `render` documents "Raises :class:`TemplateError`" but catches only `jinja2.TemplateError`. `{% include 'x' %}` raises `TypeError: no loader for this environment specified`; `{{ state.count ** 99999 }}` raises `ValueError: Exceeds the limit (4300 digits)`. Unreachable from a validated pack, but DESIGN 7.3 wants node failures rather than crashes, and phase 2's hot reload may render before validating. | Catch `Exception` and re-raise as `TemplateError`, or state that the contract holds only for validated templates. |
| N2 | nit | `support_core/graph/templates.py:59` | `TemplateIssue.fatal` is never read: `_Rules.template` reports every issue as ERROR. The field implies a distinction that does not exist. | Delete it, or use it (a template `type` issue against an unresolved pack model is arguably a warning). |
| N3 | nit | `support_core/graph/rules.py:253` | `graph.subgraph_cycle` fires only when *no* graph in the cycle contains any suspending node anywhere (admitted, item 9). Reproduced: `a` and `b` recurse through each other and `a` has an `ask` on a branch the cycle never takes; zero findings, not even a warning. | Check the cycle path rather than the whole graph, or at least warn. |
| N4 | nit | `BACKLOG.md:51` | The exit criterion records "343 tests green"; the implementation notes and reality say 344. Same class as phase-0 N7. | Correct the number when closing. |
| N5 | nit | `support_core/graph/nodes.py:76-86` | `subgraph.inputs` is keyed by the callee's name and `subgraph.outputs` by the caller's, which the implementer asked a reviewer to confirm. It is consistent under "target: source" and the docstrings say so. | No change. Recorded so the question is closed rather than re-litigated in phase 2. |

Severity counts: 2 must-fix, 6 should-fix, 5 nits.

### Sandbox escape attempts

Every input below was run through `parse`, then `unparse`/`walk`, then `infer`, then `evaluate`
against a real Pydantic state model (`reviews/scratch-phase-1/attack_expr.py`, 135 cases). The
contract under test: any string yields `ParseError`, `TypeError_` or `EvaluationError` and never
anything else, and no attribute outside a declared model field is ever reachable. **No input
falsified either half.** The only unexpected outcomes were the three `unparse` round-trip failures
of F6.

| Attempt | Result |
|---------|--------|
| `state.__class__`, `state.__class__.__mro__`, `state.__class__.__mro__[1].__subclasses__()`, `state.charge.__class__.__init__.__globals__`, `state.charge.__dict__`, `state.__init__`, `state . __class__`, `().__class__`, `ctx.customer.attributes.__class__` | `ParseError` "names starting with '_' are not addressable", raised in the lexer before the parser sees them |
| `state._private` | `ParseError`, same rule |
| `state.model_dump`, `state.model_fields`, `state.model_config` | parse; `TypeError_` "State has no field ..." at load time, `EvaluationError` at run time. Reachable only as a *declared* field, and `types.py` rejects a declared field starting with `model_` |
| `state.model_dump()`, `state.name.upper()` | `ParseError` "calls are not supported" |
| `getattr(state, '__class__')`, `open('x')`, `eval('1')`, `exec('1')`, `__import__('os')`, `range(10)`, `lipsum`, `cycler`, `self`, `namespace` | `ParseError` "unknown name ...; expressions may only start from ctx, result, state" |
| `state|attr('__class__')`, `state | unknown_filter` | `ParseError` "unknown filter" |
| `state.items[0]`, `state.mapping['secret']`, `state.items[0:1]`, `[].append`, `{}.keys` | `ParseError` "unexpected character '['/'{'" |
| `state.count + 1`, `*`, `**`, `%`, `~`, `lambda: 1`, `1 if 2 else 3` | `ParseError` |
| `state.mapping.secret` | parses; `TypeError_` at load time (the checker rejects dict traversal, F8), so unreachable from a validated pack |
| `"(" * 10000 + "1" + ")" * 10000`, `"(" * 100000` | `ParseError` "expression is longer than 2000 characters" |
| `"not " * 5000`, `"-" * 5000 + "1"`, `"1 or " * 400`, `"1 and " * 400`, `"state" + ".a" * 5000`, `" | default(2)" * 200` | `ParseError` (length) |
| `"-9" * 500` | `ParseError` "more than 500 tokens" |
| `"state" + ".a" * 249` (the deepest chain the limits allow) | parses and type-checks; 749 Python frames; `RecursionError` only with ~500 frames of caller padding (F5) |
| `"9" * 1999` | parses to a 1999-digit int; stays under CPython's 4300-digit conversion limit only because `MAX_LENGTH` is 2000 |
| `1e999999`, `1e-999999`, `1_000`, `0x41`, `0b1010`, `0o17`, `1j`, `1.2.3`, `1..2` | all `ParseError` except `1e999999`/`1e-999999`, which parse to `inf`/`0.0` (F6) |
| `'unterminated`, `"unterminated`, `'a\nb'`, `'''triple'''`, `'a' 'b'`, `b"bytes"`, `r'raw'`, `f'{state.charge_id}'` | `ParseError` |
| `'\x41'`, `'A'`, a dangling backslash | `ParseError` "unknown escape sequence" / "dangling backslash" |
| `'\0'` (yields a real NUL in the value) | parses; the NUL lives inside a string literal only, and `unparse` then breaks (F6) |
| Cyrillic `а`/`е`/`ѕ` homoglyphs, fullwidth `ｓ`, small-capital `ᴄ`, zero-width space U+200B, NBSP U+00A0, RTL override U+202E, Greek text | `ParseError` "unexpected character" - the lexer allowlists ASCII identifier characters, so no homoglyph can impersonate `state` |
| A literal NUL byte before and after an expression | `ParseError` "unexpected character '\x00'" |
| `# comment`, `-- comment`, `/* c */`, `;drop table x` | `ParseError` |
| `1 < 2 < 3`, `1 == 2 == 3` | `ParseError` "chained comparisons are not supported" |
| `state.count > > 1`, `><`, `=`, `!= !=`, `and`, `or or or`, `()`, `(,)`, `state.`, `.state`, `|`, `state |`, `state | len len`, empty string, whitespace only | `ParseError` |
| `state | default(state.count)` | `ParseError` "filter arguments must be literals" - filter arguments cannot smuggle an expression |
| `state | default(1, 2)`, `state | money(1)`, `state | default` | `ParseError` on arity |
| `state.state`, `state.and`, `state.not`, `state.true` | `ParseError` "reserved word ... cannot be an attribute name" |
| `result` with no result in scope | `TypeError_` / `EvaluationError` "not available here" |
| 2000 hypothesis examples over an alphabet including `\x00`, `\x0b`, `\x1b`, U+202E and exponent forms | no unexpected exception; falsified only the `unparse` round-trip (F6) |

Templates (`reviews/scratch-phase-1/attack_templates.py`, 57 cases). Every escape is caught twice -
`convert` rejects it at load time and the sandbox rejects it again at render time:

| Attempt | validate() | render() |
|---------|-----------|----------|
| `{{ state.__class__ }}`, `{{ state.__class__.__mro__ }}`, `{{ state['__class__'] }}`, `{{ ''.__class__.__mro__[1].__subclasses__() }}` | issue (`type` / `unsupported`) | `SecurityError` "access to attribute '__class__' ... is unsafe" |
| `{{ self }}`, `{{ self._TemplateReference__context }}` | `unsupported` | `self` renders as an opaque reference; the private attribute is refused |
| `{{ lipsum.__globals__ }}`, `{{ cycler }}`, `{{ joiner }}`, `{{ namespace() }}`, `{{ range(10) }}`, `{{ dict() }}`, `{{ config }}`, `{{ request }}` | `unsupported` | "'x' is undefined" - `env.globals` is cleared |
| `{% for %}`, `{% set %}`, `{% with %}`, `{% block %}`, `{% macro %}`, `{% call %}`, `{% filter %}`, `{% include %}`, `{% import %}`, `{% extends %}`, `{% do %}` | `unsupported` or `syntax_error` for every one | three of them raise a non-`TemplateError` (N1) |
| `{{ state|attr('__class__') }}`, `{{ x | upper }}`, `{{ x is defined }}`, `{{ x is none }}` | `unsupported` | filters and tests are cleared: "No filter named", "No test named" |
| `{{ a + b }}`, `{{ a ~ b }}`, `{{ '%s' % a }}`, `{{ a if b else c }}`, `{{ [1,2] }}`, `{{ {'a':1} }}`, `{{ (1,2) }}`, `{{ a.b() }}`, `{{ a['b'] }}` | `unsupported` | n/a |
| `{{ '{{ state.note }}' }}`, `{% raw %}{{ state }}{% endraw %}` | accepted | braces render as inert text; nothing is re-parsed, so DESIGN principle 7 holds |
| `{{ state.chrage }}`, `{{ state.charge.nope }}`, `{{ state.note|money }}` | `type` | `StrictUndefined` / `FilterError` |

Hostile packs against the confirm rule (`attack_confirm.py`, `attack_confirm2.py`). "Caught" means
the pack is rejected by `load_pack`:

| Hostile shape | Result |
|---------------|--------|
| A: sub-graph whose `confirm` covers only one router branch | caught, `graph.unconfirmed_write` |
| B: mutual recursion `a -> b -> a` with the write in `b` | caught, `graph.unconfirmed_write` + `graph.approval_missing` |
| C: write inside a `gate` redirect graph, caller confirms nothing | caught, both rules - redirect edges really are inlined |
| D: a `tool` node's `on_error` edge jumping past the confirm onto the write | caught, `graph.unconfirmed_write` |
| E: loop re-entering the write after the confirm, through a `handoff` | **not caught** (F4) |
| F: one sub-graph called from a confirmed and an unconfirmed site | caught - context-insensitive returns fail safe |
| G: write in a graph called only from an unreachable node, no confirm anywhere | caught by `graph.approval_missing` alone; the coverage analysis never visits it |
| G2: same, but the callee has a `confirm` the tool node can name, and a router around it | **not caught** (F3): zero errors, one unrelated warning |
| H: `tool` node rewriting `state.charge.amount` between the confirm and the call, identical argument text | **not caught** - the admitted textual-comparison hole, confirmed |
| I: `issue_refund` declared `risk: read` | **not caught** - the admitted self-declared-tier hole, confirmed |
| J: WRITE tool with `confirm_exempt: true` | **not caught** by design; INFO `graph.confirm_exempt` only (admitted) |
| K: confirm's `yes` and `no` branches both reaching the write through a shared sub-graph | caught, `graph.unconfirmed_write` |
| N: an `ask` between the confirm and the call | caught, `graph.unconfirmed_write` |
| R: confirm in the caller, tool in the callee, `requires_approval` naming the caller's confirm | "caught", but unsatisfiably so (F1) |
| T: mutual recursion with an `ask` on an untaken branch | not caught (N3, admitted item 9) |
| Deep-nested YAML (20k brackets) and an alias bomb in `graphs/*.yaml`, `tools/tools.yaml`, `pack.yaml`, `knowledge/sources.yaml` | all become findings (`graph.invalid_yaml`, `tools.manifest_invalid`, `manifest.invalid`, `knowledge.sources_invalid`); no exception escapes `validate_pack` or `load_pack_report` |

### Exit criterion

Both halves hold, with the F2 caveat.

- **Stepper.** `tests/test_stepper.py` runs `tests/packs/deterministic_pack` (only `router`, `say`,
  `subgraph`, `end`) and asserts the exact seven-visit path across two graphs, the rendered
  messages, the sub-graph outputs landing in the caller's state, byte-identical repeat runs, the
  router dead-end, the step limit, and `NotExecutableError` naming the phase. Real behavioural
  assertions, not restatements of the implementation.
- **Validator.** 38 tests in `tests/test_graph_validator.py`, each a minimal delta from a
  known-good fixture, asserting a specific rule id. Every ERROR-severity rule id in the source is
  asserted somewhere except `graph.end_output_unknown` and `graph.end_output_missing` (F2).
- **Quarantine.** `pyproject.toml` ships `packages = ["support_core"]` only, nothing under
  `support_core/` imports `tests.stepper`, and the module docstring lists what phase 2 must add.
  Genuinely a test utility, not a second executor.

### Design conformance

- **6.2 node vocabulary, row by row.** All ten types are registered with the table's
  `chooses_edge` and `suspends` values, including `tool` suspending only for a declared `async`
  tool and `subgraph` being transparent. The one gap is the sentence after the table: custom node
  types registered by name in the pack are impossible because `NODE_TYPES` is a closed dict
  (admitted; phase 4 owns pack imports).
- **6.4, field by field.** I extracted the `refund.yaml` block from DESIGN.md verbatim (lines 244
  to 359), dropped it into a pack unchanged and validated it: **0 errors**, 7 warnings, all
  advisory (`Charge` unresolved until phase 4, `charge_hint` not in `state`, four `str | None`
  into `str` notes, no router `default`). Bare `yes:`/`no:` confirm edges, the mixed
  `into: { eligible: result.eligible }` and `into: { outcome: "refunded" }` forms,
  `requires_approval`, `knowledge: { query, k }` and `edges: { resumed: ..., closed: ... }` all
  load as written. That is the strongest conformance evidence available and it passes.
- **Deviations.** `router.default` (addition; improves determinism, warned when absent);
  `say.message` and `subgraph.graph/inputs/outputs/next` key names (DESIGN gives none);
  `graph.node_unreachable` and the approval trio (additions derived from 8.2); the flat
  `graphs/<id>.yaml` layout enforced by `graph.id_mismatch`; `ConversationContext` invented so
  `ctx` is type-checkable. All are documented in the plan and none contradicts the document. The
  literal-versus-expression rule is a language decision DESIGN.md does not make; the choice
  ("starts with a root, or it is an error if it merely looks like one") is the safe direction.
- **5.2.** Every listed check exists as a named rule. Two are weaker than the prose: "no graph is
  reachable from itself without passing through a suspending node" is exact per-graph but only
  coarse cross-graph (N3), and the confirm rule's reachability frontier has the F3 hole.
- **8.2.** The risk table is transcribed once, in `tools/risk.py`; `MODEL_CALLABLE` blocks
  WRITE/HIGH from `llm` tool loops; `needs_confirm` correctly refuses to honour `confirm_exempt`
  for HIGH. Good.

### Forward compatibility

- **Phase 2 (executor, checkpoints, frame stack).** The seams are clean: `NODE_TYPES` carries
  `suspends`/`chooses_edge`, `Graph` carries built Pydantic models, `parse_value` is the single
  literal-versus-expression decision, `PackPin` is ready. Concrete problems: (a) DESIGN 6.3's
  `NodeResult`/`Node` protocol does not exist, so phase 2 writes it from scratch and the node
  *config* models in `nodes.py` will need to sit beside *behaviour* classes - decide now whether
  `NodeTypeSpec` grows a `runner` field or a parallel registry appears; (b) "an input lands in the
  state field of the same name" is enforced only as a warning yet is baked into both the stepper
  and `graph.input_not_in_state`, so overruling it changes both; (c) `ENVIRONMENT` is a
  process-wide Jinja environment and must become per-pack the moment a pack supplies a filter or
  two pack versions are loaded side by side (6.7); (d) `load_pack` reads and parses every graph
  twice, so `PackPin` can hash a different byte sequence than it parsed if the directory changes
  underneath - snapshot once. (c) and (d) are admitted.
- **Phase 4 (real registry, gate and confirm execution).** `ToolManifest`/`ToolSpec` is a good
  shape to swap out. Concrete problems: (a) F1 must be fixed before any pack can put a write
  behind a sub-graph; (b) `needs_confirm` lives on `ToolSpec`, in the file phase 4 replaces - move
  the policy next to `Risk` so it is not reimplemented; (c) the validator must compare the imported
  `TOOLS` with `tools/tools.yaml` and report drift, or hostile case I (a HIGH tool declared `read`)
  survives into production; (d) the approval hash the engine computes must be built from the same
  `parse_value` classification the validator uses, or F7's collisions become run-time refusals.
- **Phase 6 (interrupts).** The CFG models neither the interrupt push nor the return-and-resume of
  DESIGN 6.6, so an approval given before an interrupt is assumed to survive it (admitted, item 4).
  With F4 that is now two ways an approval outlives the customer turn it was given in. Phase 6 must
  revisit `covered_out`, not only the engine.

### Test quality

Sampled `test_graph_validator.py` (38 tests), `test_expressions.py` (19), `test_templates.py` (9),
`test_loader.py` (11), `test_graph_types.py` (9), `test_pack_validate.py` (22). They assert
behaviour, not implementation: the validator tests mutate one exact substring of a known-good
fixture so each case shows only its own defect, and the accepted/rejected expression tables assert
canonical output and error fragments rather than internal structure. `pytest -q` three times:
`344 passed` each time, no ordering dependence; a four-file subset in isolation: `200 passed`.
Weaknesses: the confirm analysis has nine behavioural tests but none for gate-redirect inlining,
the `on_error` shape or the loop of F4 (I exercised all three by hand; only the loop is broken);
the parser property test's generator is too narrow to explore the space it claims to (F6) - I
falsified its round-trip property in 2000 examples with a two-line change to the strategy; and
there is no property or differential test for the dataflow, which the implementer identified as the
test they would write first. I wrote that test in scratch (400 random graphs, the dataflow versus
an independently written product-automaton reachability) and it found **zero mismatches**, which is
real evidence the intra-graph analysis is correct. It belongs in the suite.

### The every-phase rule

Holds. `grep` over `support_core/` finds no `eval`, `exec`, `compile`, `ast`, `importlib`,
`subprocess`, `pickle` or `os.system`, and no code path that invokes a tool. The stepper raises
`NotExecutableError` for `tool`, `confirm`, `gate`, `llm`, `ask` and `handoff` before doing
anything, and a test pins the message. Statically, the rules that make phase-4 enforcement possible
are present, and importantly `graph.approval_missing` fires on **every** WRITE/HIGH tool node
independently of the dataflow - that is what caught hostile case G when the coverage analysis did
not, and it is the right belt-and-braces design. F1 is the one place where those static rules are
currently self-contradictory.

### Missed by self-critique

The self-critique is unusually good: items 1 (textual approval comparison), 2 (`confirm_exempt`),
3 (self-declared risk tiers), 4 (context insensitivity, interrupts), 5 (`handoff` does not clear
approvals), 6 (unreachable code unanalysed) and 9 (coarse `subgraph_cycle`) all reproduce exactly
as described. What it missed:

1. `requires_approval` cannot name a confirm in another graph, which makes a write inside a
   sub-graph unvalidatable and contradicts the interprocedural analysis the same file implements
   (F1). Its own passing test encodes the contradiction.
2. Item 6 is worse than "a warning-only finding is load-bearing": given a same-graph confirm to
   name, the WRITE tool produces **no finding at all** except a warning about a different node in a
   different file (F3), and the entry rules are inconsistent about which uncalled graphs get
   checked.
3. Item 5 combined with a loop means one approval authorises unbounded calls, and neither the
   validator nor the phase-4 hash check will see it (F4).
4. `MAX_DEPTH` does not bound attribute and filter chains at all; the recursion safety of the
   walkers rests on `MAX_TOKENS` versus CPython's frame limit, with about 250 frames of headroom
   (F5).
5. The `unparse` round-trip property the approval comparison is said to rest on is false, and the
   property test's generator cannot find the counterexamples (F6).
6. `_canonical_args` does not use `parse_value`, so it compares scalars by different rules than the
   engine will evaluate them (F7).
7. The evaluator and the type checker disagree about mapping traversal, which makes
   `ctx.customer.attributes` - DESIGN 10's CRM record - unreadable from any graph (F8).
8. `render` violates its documented exception contract (N1) and `TemplateIssue.fatal` is dead (N2).
9. BACKLOG's exit criterion says 343 tests where the notes and reality say 344 (N4) - the same
   bookkeeping slip as phase-0 N7.

### Commands run and results

All from the repository root with `.venv/Scripts/python.exe`, Windows 11, Docker container
`customer-support-agent-db-1` healthy, database `support_test`.

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) |
| `python -m ruff format --check .` | `58 files already formatted` (exit 0) |
| `python -m mypy` | `Success: no issues found in 58 source files` (exit 0) |
| `python -m pytest -q` (run 1) | `344 passed in 7.26s` |
| `python -m pytest -q` (run 2) | `344 passed in 11.35s` |
| `python -m pytest -q` (run 3) | `344 passed in 9.98s` |
| `python -m pytest -q -p no:cacheprovider tests/test_graph_validator.py tests/test_expressions.py tests/test_stepper.py tests/test_loader.py` | `200 passed` (no cross-file ordering dependence) |
| `support pack validate packs/acme_billing` | `INFO pack.empty`, then `acme-billing: empty but well-formed`, exit 0 |
| `support pack validate tests/packs/refund_pack` | `refund-pack: well-formed (5 warning(s))`, exit 0 |
| `support pack validate --strict tests/packs/refund_pack` | exit 1 (warnings fail strict; every finding still printed) |
| `python -m alembic upgrade head && python -m alembic check` | `No new upgrade operations detected.` |
| `attack_expr.py` (135 adversarial expressions) | 0 unexpected exception types; 3 `unparse` round-trip failures (F6) |
| `falsify_roundtrip.py` (2000 hypothesis examples, wider alphabet) | round-trip property falsified by `1e311` and `'\x07'`; still no unexpected exception (F6) |
| deep attribute chain probe (`state` + `.a` * 249) | parses and type-checks; peak 749 Python frames; `RecursionError` at ~500 frames of caller padding (F5) |
| `attack_templates.py` (57 templates) | every escape rejected at load and at render; 3 non-`TemplateError` render exceptions (N1) |
| `attack_confirm.py` / `attack_confirm2.py` (16 hostile packs) | 11 caught, 5 accepted (E, G2, H, I, J) as tabulated above |
| `differential_confirm.py` (400 random graphs: dataflow vs product-automaton reachability) | `400 random graphs compared, 0 mismatches` |
| DESIGN.md 6.4 `refund.yaml` extracted verbatim into a pack and validated | 0 errors, 7 warnings, 7 infos |
| YAML deep-nest and alias bomb in each of `graphs/`, `tools/tools.yaml`, `pack.yaml`, `knowledge/sources.yaml` | all reported as findings; no exception escapes `validate_pack` or `load_pack_report` |
| `grep` for `eval(`, `exec(`, `compile(`, `ast`, `importlib`, `subprocess`, `pickle`, `os.system` under `support_core/` | no hits (one `def pack_eval` CLI stub that exits 3) |
| rule-id cross-check script (source vs `tests/`) | 5 listed ids with no test assertion (F2) |

Scratch files were created under `reviews/scratch-phase-1/` and deleted. No source, test or config
file was modified; the only repository changes are this section and the Phase 1 status cell in
BACKLOG.md, set to `in-review`. Nothing was committed. The database was left at `0001 (head)`.
