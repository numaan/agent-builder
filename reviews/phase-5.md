# Phase 5 review: knowledge layer and citations

Design references: DESIGN.md sections 3 (principles 5 and 7), 9.1 to 9.3, 11.2 (layer 7), 13
(the packet carries citations), 14 (the outbound citation guardrail), 17 (`doc_source`,
`doc_chunk`), 20 (the latency budget). Backlog: BACKLOG.md "Phase 5", the deferred findings
assigned to it (phase 0 N12 and N2, phase 6's forward-compatibility line about `gather()`), and
the two 2026-09-06 decisions that put ColBERT in this phase and Qdrant underneath it.

Inherits the settled decisions in reviews/phase-0.md through reviews/phase-4.md,
reviews/phase-w.md and reviews/phase-6.md. An earlier attempt at this phase was started and
discarded; this plan was written fresh against the amended backlog.

## Plan

Written before any code, per PLAN.md step 1.

### What this phase is really about

Four things, in the order they matter, because three of them are safety surfaces and only one is
a feature:

1. **A retrieved passage is untrusted text.** DESIGN.md principle 7 puts retrieved documents in
   the same sentence as customer messages and tool outputs, and phase 3's review broke the
   prompt boundary and rebuilt it around a per-render delimiter token. Passages must go through
   *that* path - `PromptInputs.knowledge`, layer 7, `data_block` with the nonce - and this phase
   must add no second way to put text into a prompt. The test that proves it is not a unit test
   of the assembler (phase 3 already has one): it is the injection matrix re-run with hostile
   documents in the *corpus*, ingested by the real sync and retrieved by the real retriever, and
   the forged count must be zero.

2. **A wrong answer must be traceable to a source version** (principle 5, section 9.2). That is
   the phase's exit criterion and it is the thing that decides the whole storage design. It is
   only true if a re-sync *adds* a version rather than mutating one: new `source_version` on new
   rows, old rows kept and marked stale, a new Qdrant collection with an alias flipped onto it,
   and the version of every passage used written into the trace step at the time it was used.
   "Approximately true" here means an old trace names a version whose content has since changed
   underneath it, which is worse than no trace at all.

3. **A factual claim with no citation is refused** (sections 9.2, 14). Rule-based first, one
   re-prompt, then handoff - and the handoff is phase 6's real one, not a stub.

4. **Retrieval itself**: ingestion, chunking, two vector backends and a lexical one, merged.

### Task breakdown

1. **`support_core/knowledge/types.py`** - DESIGN.md 9.1's `Passage` and `Retriever` protocol.
   `Passage` gains an `id`, because 9.2 requires "every passage handed to the model carries an
   inline id" and the design's own class has nowhere to put one. This is the canonical
   `Passage`: `support_core.handoff.packet.Passage` is replaced by it (it was a phase-6 seam
   with a note saying phase 5 should fill the shape rather than redefine it), so the repository
   has two passage types and not three. The third, `support_core.llm.prompt.Passage`, stays: it
   is the *render* shape, and `prompt.py` deliberately imports nothing from the retrieval side,
   because the prompt boundary must not move when retrieval changes.

2. **`support_core/knowledge/embedding.py`** - the encoder seam. Two protocols, because dense
   and late-interaction are different shapes and pretending otherwise would force one of them
   into the other's API: `Embedder` (one vector per chunk, for pgvector) and
   `LateInteractionEncoder` (one vector per token, for Qdrant's MaxSim).
   - `DeterministicEncoder` implements both with no model and no download: a hashed projection
     over tokens, seeded from `blake2b` (never Python's salted `hash`, which differs per
     process). It is what the tests and this deployment run.
   - `FastEmbedColbertEncoder` is the real local ColBERT (`colbert-ir/colbertv2.0` through
     `fastembed`), imported lazily behind an optional extra so neither CI nor a plain install
     pulls onnxruntime.
   - `Embedder` is also the seam a hosted embedding API drops into. There is none in this
     deployment - the GLM endpoint serves no embeddings - so nothing hosted is implemented; the
     protocol and a small `CallableEmbedder` adapter are what make it a few lines rather than a
     refactor.

3. **`support_core/knowledge/sources.py`** - a typed schema for `knowledge/sources.yaml`
   (section 9.1): `markdown_dir` and `html_crawl` document sources, the knowledge-graph section
   (parsed, not loaded - phase 9), and `live_lookups`. The validator's existing
   `_check_knowledge_sources` is widened to use it, so a bad source file is a load-time finding
   rather than a sync-time traceback.

4. **`support_core/knowledge/chunking.py`** - chunk markdown by heading, 300 to 500 tokens
   (section 9.3), carrying the heading path so a locator can name it. Over-long sections split
   on paragraph boundaries and keep the heading path; short sections merge with their sibling
   only under the same heading, so a locator never spans two headings.

5. **`support_core/knowledge/ingest.py`** and **`storage/knowledge_repo.py`** - the sync
   pipeline of section 9.3: fetch, normalise to markdown, chunk, embed, upsert, mark stale.
   - `source_version` is content-addressed: `r<n>-<sha256[:12]>` over the normalised documents.
     Re-syncing unchanged content is then a no-op that keeps the version, and any edit produces
     a new one. `n` is a monotonic `doc_source.revision`, which is what the Qdrant collection
     name needs.
   - `html_crawl` fetches through an injectable `Fetcher` so the tests never touch the network.
   - All SQL lives in a repository module, per the settled boundary; `doc_source` is flushed
     before `doc_chunk` because there are no relationships anywhere in `models.py`.

6. **`support_core/knowledge/qdrant.py`** - `QdrantStore`. Collections are
   `<prefix><source>_v<n>` with multivector `MAX_SIM` comparator; a sync builds the new
   collection, then flips the alias `<prefix><source>` onto it in one call. Old collections are
   kept (a retention count, not zero), which is what makes the exit criterion exact. Every call
   is wrapped so an unreachable Qdrant raises one core exception type and never an httpx one.

7. **Retrievers** - `DocumentRetriever` (Postgres: `ts_rank_cd` over the stored `tsv`, and
   pgvector cosine over `embedding`; the two halves separately callable so they can be measured
   against each other), `ColbertRetriever` (Qdrant MaxSim), `LiveLookupRetriever` (READ tools),
   and `CompositeRetriever` (reciprocal-rank fusion, dedupe, stable `k1..kn` ids). The composite
   fans out with `return_exceptions=True`: a backend that fails is logged and dropped, which is
   the graceful degradation the Qdrant decision was justified on. All backends failing returns
   nothing, and the citation guardrail then turns a claim into a handoff rather than a guess.

8. **`support_core/guardrails/outbound.py`** - the citation check of sections 9.2 and 14. A
   rule-based claim detector over the outbound sentence, three families (policy, pricing,
   timing), and two violations: a claim with no citation, and a citation naming a passage the
   node was never given. It is wired into `LlmService.decide`'s existing retry loop, so "the
   engine re-prompts once before routing to handoff" is the mechanism phase 3 already built and
   phase 3's review already hardened (the correction is drawn from a fixed vocabulary, never
   from model-written text). A second failure raises, `LlmRunner` turns it into
   `NodeError(reason="uncited_claim")`, and phase 6's `_route_error` does the rest.

9. **Engine wiring** - `NodeRuntime.retrieve`, a per-node closure built by the executor from the
   node's own `knowledge:` block, in the same shape as `tool_gateway` (the node cannot widen its
   own `k` or query anything the graph did not declare). `Executor(retriever=...)` is the seam.
   Retrieval happens during node execution, which is outside the checkpoint transaction; that is
   the property the second store depends on and it is asserted by a test rather than assumed.

10. **The trace** - every passage used is written to `trace_step.llm_response` under
    `retrieval`, with its id, source, `source_version`, locator and score. That is the exit
    criterion's other half.

11. **The handoff** - `gather()` takes a retriever, `HandoffSummaryRequest` gains the passages,
    and `HandoffPacket.citations` is filled from the node that failed (a citation failure
    carries its passages on the `NodeError`) or from retrieval on the customer's last message.

12. **Migration `0010`** - `doc_chunk.embedding` to `vector(128)` with an HNSW cosine index
    (phase-0 finding N12), `doc_source.revision` and `doc_source.checksum`. 128 because that is
    what ColBERT v2 emits per token and what the deterministic encoder is built to match, so the
    two sides cannot disagree; a dimension mismatch at sync time is a refusal naming the
    migration, not a runtime error at query time. Phase-0 finding N2's downgrade half is decided
    here too.

13. **`docker-compose.yml` and CI** get Qdrant beside Postgres.

14. **The sample pack** gets `knowledge/docs/refund-policy.md` and
    `knowledge/docs/processing-times.md`, a real `sources.yaml`, and `knowledge:` blocks on the
    two `llm` nodes that make claims (`explain_denial`, which is "why was my refund refused",
    and `tell_done`, which promises five to seven business days).

15. **Tests** - the hostile-corpus injection matrix; the source-version trace test; chunking;
    ingestion and stale marking; every retriever; degradation with Qdrant stopped; the claim
    detector including its false positives and negatives; the guardrail's re-prompt and handoff;
    and a retrieval-quality comparison across the four backends on a labelled query set.

### Intended deviations from DESIGN.md, and why

- **A second stateful dependency.** DESIGN.md 4.1 says "Postgres is the single stateful
  dependency" and Qdrant relaxes it. Already decided and recorded in the backlog on 2026-09-06;
  restated here because a reader of this file should not have to find it elsewhere. The property
  4.1 existed to protect is that a checkpoint and a resume depend on one transaction, and
  retrieval is a read outside it.

- **`Passage` gains an `id`, and the packet's `Passage` is replaced by it.** Section 9.1's class
  has five fields and none of them can be the inline id section 9.2 requires.

- **Chunking is by heading with a *paragraph* fallback.** Section 9.3 says "chunk by headings
  (target 300 to 500 tokens)" and does not say what to do with a 2000-token section. Splitting
  it is the only option that keeps the target; the alternative - one oversized chunk - would
  blow layer 7's budget and evict its own siblings.

- **`source_version` is content-addressed rather than a timestamp or a counter alone.** The
  design says only that a sync "re-indexes with a new `source_version`". A hash makes a re-sync
  of unchanged content idempotent, which matters because 9.3 says the sync "runs on a schedule".

- **The citation guardrail checks presence, not attribution.** Section 9.2 says "rejects factual
  claims about policy, pricing, or timing with no citation". It does not say each claim must be
  matched to the passage that supports it, and a rule-based detector cannot do that honestly. So
  the rule is: if the message contains at least one claim sentence, at least one valid citation
  must be present, and every citation must name a passage the node was actually given. What that
  misses is written down plainly in the self-critique rather than implied away.

- **`pack.yaml` gains a `guardrails:` block.** Section 5.1's manifest has nowhere to put
  section 14's "pack-supplied configuration". Same precedent and same argument as phase 2's
  `timeouts:`, phase 3's `memory:` and phase 6's `limits.max_node_errors`. The default is on:
  a guardrail a pack has to remember to enable is not a guardrail.

- **`LiveLookupRetriever` is narrow.** Section 9.1 routes "entity-specific questions" to READ
  tools, which in full needs a model call to extract the tool's arguments from the question -
  a second decision surface with no graph constraining it. This phase implements the part that
  needs no such call: a declared READ tool with no arguments, or arguments taken from `ctx`,
  invoked through the same gateway an `llm` node's tool loop uses. The rest is recorded, not
  faked.

- **The real ColBERT model is not exercised here.** `fastembed` installs, but this environment
  cannot complete TLS to the model host (`CERTIFICATE_VERIFY_FAILED`), so the weights cannot be
  downloaded and the encoder that ships as the default is the deterministic one. This is the
  same shape as phase 3's unexercised `AnthropicProvider`, and it gets the same treatment: the
  seam is real, the code path is written, and the self-critique says exactly what is unproven -
  including that the retrieval-quality comparison measures the plumbing and not an embedding
  model.

### What this phase deliberately does not build

The knowledge graph retriever and `kg_entity`/`kg_relation` loading (phase 9), the email channel
and replay (phase 7), the pack authoring tool (phase 12), per-conversation cost caps (phase 9),
and model reranking of passages - section 9.1 offers it "when `k` is small" as an option, and an
extra model call per retrieval is a straight charge against section 20's four-second p95 that
nothing here can measure the benefit of.
