# Phase 5 review: knowledge layer and citations

Design references: DESIGN.md section 3 (principles 5 and 7), 9.1 to 9.3, 11.2 (layer 7), 14
(the outbound citation guardrail), 17 (`doc_source`, `doc_chunk`). Backlog: BACKLOG.md
"Phase 5", plus the deferred findings assigned to this phase. Inherits every settled decision
in reviews/phase-0.md to reviews/phase-4.md.

## Plan

Written before any code, per PLAN.md step 1.

### What this phase is really about

Four things, in the order of the risk they carry:

1. **A retrieved passage is untrusted text that arrives from outside the conversation.** It is
   the one untrusted slot in the prompt that nobody in the loop has read: a customer message is
   at least written by the person in front of you, and a tool result comes from the pack's own
   code, but a document is fetched from a help centre and may have been edited by anyone with
   commit access to it. Phase 3's review broke the prompt boundary and the fix was to invert it
   - a per-render delimiter token the writer of the data cannot know (`nonce_for`). Passages
   must go down **that** path and no other: `PromptInputs.knowledge`, rendered by `_knowledge`
   through `data_block`. Phase 5 adds no second way to put text in a prompt, and the passage
   *ids* the model is asked to cite are core-generated (`<source_id>#<chunk_index>`), never a
   heading or a title lifted out of the document, so a document cannot even choose its own name.

2. **A citation has to be checked or it is decoration.** DESIGN.md 9.2 is specific: a factual
   claim about policy, pricing or timing with no citation is rejected, the model is re-prompted
   once, and then it hands off. Phase 3 recorded `LlmNodeOutput.citations` on the trace and
   checked it against nothing, and said so. The check is rule-based, which means it has false
   negatives by construction; the self-critique has to say which ones rather than imply a
   classifier.

3. **A wrong answer must be traceable to a source version.** The exit criterion. This is a
   property of the *data model*, not of a test: chunks must be written under an immutable
   `source_version`, superseded chunks must be marked stale rather than deleted, and the
   versions used must be written into the checkpoint transaction alongside the step. Then the
   test is a demonstration rather than the mechanism.

4. **The pipeline and the search.** Fetch, normalise, chunk by headings at 300 to 500 tokens,
   embed, upsert, mark stale (9.3); pgvector plus Postgres full-text hybrid search (9.1). This
   is the largest part by volume and the smallest by risk.

### Task breakdown

1. **`support_core/knowledge/types.py`** - `Passage` exactly as DESIGN.md 9.1 writes it
   (`text`, `source_id`, `source_version`, `locator`, `score`) plus the `id` the model cites by
   and which phase 3's prompt layer already renders; the `Retriever` protocol with the design's
   signature `retrieve(query, ctx, k) -> list[Passage]`; `RetrievalResult` carrying the passages
   and any retriever failures, so a backend that fell over is visible rather than silent.

2. **`support_core/knowledge/embedding.py`** - the `Embedder` protocol (`dimensions`,
   `embed(texts)`) and `HashingEmbedder`, a deterministic local embedder: token hashing into a
   fixed number of buckets, sublinear term frequency, L2 normalised. It is not a semantic
   embedder and the self-critique will say so plainly. `EMBEDDING_DIMENSIONS = 384` is a module
   constant that the migration, the store and the embedder all read, and a sync whose embedder
   disagrees with it is refused with a message naming the migration. There is no
   `ANTHROPIC_API_KEY` here and the deployment's GLM endpoint is Anthropic-compatible for
   messages, which says nothing about embeddings, so a real embedder is a seam and not a
   promise.

3. **`support_core/knowledge/sources.py`** - Pydantic models for `knowledge/sources.yaml`
   (`DocumentSource` with `type: markdown_dir | html_crawl`, `LiveLookup`, and the
   `knowledge_graph` section parsed and carried but unused until phase 9), and the loader that
   turns the file into them. Reads through the validator's `_read_utf8` helper, reports through
   the existing `knowledge.*` rule family.

4. **`support_core/knowledge/chunking.py`** - normalise to markdown and chunk by headings at a
   300 to 500 token target, with a heading-path locator (`refunds.md#refund-window`). A section
   longer than the ceiling is split on paragraph boundaries and keeps its heading path with a
   part suffix; sections shorter than the floor are merged with the next sibling under the same
   parent. Includes a small HTML to markdown normaliser built on `html.parser` (stdlib), since
   `html_crawl` has to become markdown before it can be chunked.

5. **`support_core/knowledge/fetch.py`** - the `Fetcher` seam. `markdown_dir` reads `*.md`
   under the declared path (inside the pack, path traversal refused). `html_crawl` walks from a
   start URL within the same host and path prefix, bounded by page count and depth, through an
   injectable fetcher; the default one uses `urllib.request` in a worker thread and is only
   reachable when the CLI is given `--allow-network`. Without that flag a network source is
   *skipped with a warning*, so `support pack knowledge sync packs/acme_billing` works offline
   and in CI, which is where it has to work.

6. **`support_core/knowledge/store.py`** - `DocumentStore` over `doc_source` and `doc_chunk`:
   `sync_source` (upsert the source, insert the new version's chunks, mark every earlier
   version's chunks stale, in one transaction) and `search` (the hybrid query). Repository-style
   module functions in `storage/repositories.py` for the SQL, per the settled boundary that all
   engine SQL lives there; `store.py` is the pipeline logic above them.

   Hybrid search is two ranked lists - cosine distance over the HNSW index, and
   `ts_rank_cd` over the existing generated `tsv` column - merged by reciprocal rank fusion,
   which needs no score calibration between two incomparable scales. Ties break on
   `(source_id, chunk_index)` so the same corpus and the same query always produce the same
   passages in the same order; the fake provider replays by prompt fingerprint, so
   non-deterministic retrieval would be non-deterministic prompts.

7. **`support_core/knowledge/retrievers.py`** - `DocumentRetriever` (the store),
   `LiveLookupRetriever` (READ tools) and `CompositeRetriever` (fan out, merge, deduplicate).

   `LiveLookupRetriever` calls tools **through phase 3's `ReadOnlyToolGateway`**, built from
   the live-lookup tool names by the same `tool_gateway` factory an `llm` node's model loop
   uses. There is no second path to the tool runtime and no way for a knowledge query to reach
   a WRITE or HIGH tool: the gateway refuses on tier before the runtime refuses again.
   Routing - DESIGN.md 9.1's "entity-specific questions" - is rule-based: the design's own
   example is *"what does my plan include"*, so a first-person possessive in the query routes,
   and a source may narrow it with `triggers:`. That is a crude rule and the self-critique says so.

8. **`support_core/knowledge/ingest.py`** - the 9.3 pipeline, and `source_version`. The version
   is `sha256` over the normalised document texts and their locators, truncated: re-syncing
   unchanged content produces the same version and writes nothing, and any edit produces a new
   one. Chunks of older versions are marked `stale = true` and kept, because "an older trace
   still names the old version" is only true if the row it names is still there.

9. **`support_core/guardrails/outbound.py`** - `CitationGuardrail`. A rule-based detector over
   the sentences of a customer-facing message, in DESIGN.md 9.2's own three categories: policy,
   pricing, timing. A sentence that trips a category is a claim; a message with claims and no
   citation fails, and so does a citation naming a passage id that was not offered this turn -
   a fabricated citation is worse than a missing one, because it looks checked.

   The re-prompt lives in `LlmService.decide`, which already owns "one retry with the reason
   stated back to the model" and already has the passages. The correction is drawn from a fixed
   vocabulary (phase 3 finding V5: nothing model-written may reach layer 5), and the second
   failure raises, which `LlmRunner` turns into a `NodeError` with a new reason
   `citation_missing`, which is a handoff.

10. **Wiring** - `NodeRuntime.retriever: Retriever | None` (the precedent is
    `llm: LlmService | None`, and the null case must be a node error rather than silence);
    `LlmRunner` renders the node's `knowledge.query` template through the *pack's* environment,
    retrieves, and passes the passages into `NodeRequest`. The passages used are written to
    `trace_step.llm_response` under `passages`, in the checkpoint transaction, which is where
    the exit criterion's evidence lives.

11. **Migration `0008`** - `doc_chunk.embedding` becomes `vector(384)` with `USING`, so a row of
    another dimension fails the migration loudly instead of being dropped (the settled rule
    from phase 2's R8: a back-fill fails, it does not delete); an HNSW index with
    `vector_cosine_ops`. Phase 0's N2 is settled in the same commit: `0001`'s downgrade stops
    dropping the `vector` extension, because dropping a shared extension an administrator
    pre-installed is worse than leaving one behind, and `CREATE EXTENSION IF NOT EXISTS` makes
    the upgrade idempotent either way.

12. **CLI** - `support pack knowledge sync PATH [--only ID] [--allow-network] [--dry-run]`,
    replacing the stub that exits 3. Same output contract as `pack validate`: findings printed
    with their rule ids, a summary line, exit 0 or 1.

13. **Manifest** - a `knowledge:` block (`citations.enabled`, `k`, `min_score`) so a pack can
    configure the guardrail DESIGN.md 14 says packs configure. Structural guardrails stay
    non-negotiable; this is an outbound one and 14 lists it as configurable.

14. **Sample pack** - `knowledge/docs/refund-policy.md` and `knowledge/docs/processing-times.md`,
    declared in `sources.yaml` as a `markdown_dir` source, plus the `html_crawl` help-centre
    source from DESIGN.md 9.1's own example (skipped offline) and a `live_lookups` entry.
    `refund.yaml`'s `tell_done` and `explain_denial` nodes get `knowledge:` blocks, because they
    are the two nodes in the pack that make timing and policy claims - `tell_done` currently
    asserts "five to seven business days" out of the node's own instructions, which is exactly
    the uncited factual claim this phase exists to refuse.

15. **Tests** - chunking and normalisation; ingestion and versioning against real Postgres;
    hybrid search including a vector-only and a text-only hit; a **hostile corpus** (documents
    that try to close the fence, forge a header, carry the delimiter token, or instruct the
    model) driven end to end through sync, retrieve and assemble, with the phase-3 matrix's own
    judge; the phase-3 injection matrix re-run with a *retrieved* passage as the injection point;
    the citation guardrail's detector and its re-prompt-then-handoff behaviour through the real
    `LlmRunner`; the live-lookup gateway refusing a WRITE tool; the CLI; and the exit criterion.

### Intended deviations from DESIGN.md, and why

- **`Passage` gains an `id`.** DESIGN.md 9.1's class has no id, and 9.2 requires "every passage
  handed to the model carries an inline id". Phase 3 already built layer 7 around one. It is
  `<source_id>#<chunk_index>`, generated by core from the pack's source id and an integer, so no
  document text reaches it.

- **`live_lookups` entries gain optional `triggers` and `args`.** DESIGN.md 9.1's example is
  `- tool: get_plan_details` and nothing else, which does not say *when* to route or what to
  pass. The default keeps the bare form working (first-person possessive routes it, no
  arguments), and the two keys are how a pack narrows it.

- **`html_crawl` is skipped without `--allow-network`.** The design says "fetch"; it does not
  say a sync must reach the internet during a CI run on a pack change, and CI has no network to
  `help.acme.com`. The type is implemented and tested against an injected fetcher.

- **A `knowledge:` block on an `llm` node with no retriever configured is a node error**, the
  same shape as an `llm` node with no provider. Silently retrieving nothing would mean the
  citation guardrail then hands off for a reason that names the model rather than the missing
  configuration.

- **The citation guardrail is called from the LLM layer, not from the executor's
  `guardrails.outbound` point.** That point is phase 7's and stays empty rather than
  half-filled. DESIGN.md 9.2's "re-prompts once" is only expressible where the prompt is, and
  putting it in `LlmService.decide` reuses the retry that already exists instead of adding a
  second one. The two failures share one retry budget: two bad answers in a row is a handoff,
  whichever kind they were.

- **The rule-based detector is enabled by default** (`knowledge.citations.enabled: true`).
  DESIGN.md 9.2 states the rejection unconditionally, so the default is on and a pack turns it
  off deliberately.

- **A third GLM-shaped defect is assumed and coded against.** BACKLOG's decisions log records
  two live findings of one family - the model spelling absent or nested values as text. The
  third instance in this phase's surface is `citations`: a model that answers `"k1"`,
  `"[\"k1\"]"` or `"null"` instead of a list. Those are read as what they mean and then
  validated exactly as before. Nothing about the decision constraint is relaxed.

### What this phase deliberately does not build

The knowledge graph retriever and `kg_entity`/`kg_relation` loading (phase 9), model reranking
of passages (DESIGN.md 9.1's "reranking by the model when `k` is small" - noted, not built, and
the self-critique says what that costs), the interrupt check and the handoff node (phase 6),
the email channel, tracing and replay (phase 7), PII redaction of retrieval queries (DESIGN.md
14, phase 7 - the seam is named where the query is built), and the scheduler that would run
`knowledge sync` "on a schedule in production".
