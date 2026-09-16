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

### What changed about this plan, and why

The plan above was written by an earlier agent that was stopped mid-implementation. It was
inherited rather than rewritten, because it still describes what is being built: every numbered
item was delivered. This section records the four places where doing the work changed the plan
rather than followed it, and the one thing the plan is silent about.

1. **Item 4, chunking, is one rule shorter.** The plan says a short section "merges with the next
   section *only while the heading path still describes it*", and promises that "two sibling
   sections under different headings never become one chunk". The code did the first and
   therefore broke the second. Merging a short section forward under an ancestor path makes the
   *next* sibling a descendant of the merged block too, so the merge cascades: the sample pack's
   refund policy came out as three chunks with four of its five sections inside one of them,
   located to the document's title. Every locator was true and none was useful, which is the
   failure the exit criterion exists to prevent one level up. The merge pass is gone. A short
   section is now its own chunk with its own locator, and it is legible on its own because
   `Chunk.with_heading` puts the whole heading path above the text. Measured on the sample pack:
   4 chunks became 11, and every locator names a heading.

2. **Item 7's lexical half needed a change the plan did not anticipate.**
   `websearch_to_tsquery` joins bare words with `&`, so a retrieval query built from a customer's
   sentence - "how many days do I have to ask for a refund" - became
   `'mani' & 'day' & 'ask' & 'refund'` and matched only a passage containing all four. On the
   labelled query set that is 3 of 12. Relaxing the conjunction to a disjunction and letting
   `ts_rank_cd`'s cover density order the matches gives 12 of 12. The rewrite is textual and
   preserves a quoted phrase's `<->`; what it gets wrong is a leading negation, which is accepted
   rather than fixed, because the alternative is parsing tsquery text in a repository module.

3. **Item 9 grew a per-node half.** The plan has one retriever built at startup and captured in a
   closure. That is right for the document backends and wrong for `LiveLookupRetriever`, which
   calls READ tools: a tool call belongs to the turn whose budget it spends and to the node whose
   allow-list it obeys. The live backend is therefore composed per node, around the same gateway
   that node's own model loop gets, and `CompositeRetriever.plus` returns a new composite rather
   than mutating the long-lived one.

4. **Item 15 gained a golden conversation.** "Why was my refund refused" is the question the
   phase's documents exist for, and nothing exercised it end to end. `acme_refund_denied` is a
   real denial - a June charge outside the 60-day window - whose `explain_denial` node retrieves
   the rule behind the refusal from the corpus and cites it.

The plan is also silent about the composition root, which turned out to be a third of the work.
`Pack` now carries the parsed `knowledge/sources.yaml` so the sync, the validator and the service
work from one reading of one file; `pack.yaml` gains the `guardrails:` block item 8 promised;
`support_core/knowledge/wiring.py` is the one place that turns a pack into a retriever; and
`build_runtime` hands that retriever to both the executor and the handoff service.

## Inherited state

What was in the working tree when this phase was picked up, and what happened to it. (The library
half of it was committed by another process while this phase was in flight, as
`2a28eaf phase-5: snapshot the in-flight knowledge layer`; everything below was written before
that commit existed and describes the same code.)

### Kept

Nearly all of it, and it was worth keeping. The module layout; `Passage` with its `id` and
`backend`; the two encoder protocols with one deterministic local implementation of both; the
Qdrant store with a collection per revision and an atomic alias flip; the reciprocal-rank merge
and its argument for rank fusion over score fusion; the content-addressed `source_version`; stale
marking rather than deletion; the repository split that keeps retrieval SQL out of the engine's;
the citation guardrail riding phase 3's existing retry loop rather than inventing a second one;
`NodeError(reason="uncited_claim")` reaching phase 6's real handoff carrying the failing node's
passages; and migration `0010`. The reasoning in those module docstrings is the reasoning this
phase would have arrived at, and where a later section of this file argues for a decision it is
usually restating theirs.

### Fixed

Nine defects. Seven of them only running the code could find, which is the honest summary of what
a stopped implementation leaves behind: the design was sound and nothing had been executed.

1. **The chunker's merge pass cascaded**, as above. The regression is the first test in
   `tests/test_knowledge_chunking.py` and it fails against the inherited code.
2. **The lexical half ANDed every query term**, as above, so a natural-language question matched
   nothing at all on a policy corpus.
3. **`LiveLookupRetriever` never called `gateway.specs()`.** The gateway resolves a tool's risk
   tier on that call and refuses anything it has not resolved, so every live lookup was a
   refusal - and a retriever that returns nothing looks exactly like one that is being refused.
4. **Nothing constructed a live-lookup retriever at all.** The class existed and no code path
   built one, because building one needs a tool gateway and the plan had the retriever built at
   startup where there is none. `Executor._retrieval` composes it per node now.
5. **`crawl` did not stay under the start URL's path.** The boundary was
   `start.rsplit("/", 1)[0]`, which for `https://help.example/billing` is the origin - so a crawl
   of a help centre would follow `/pricing`, and marketing copy would enter a policy corpus the
   agent cites. Replaced with an explicit boundary rule, and the test names the page it must not
   fetch.
6. **`create_collection` failed permanently on a name it already held.** Collection names carry
   `doc_source.revision` from Postgres, and the two stores drift after a sync that crashed
   between building a collection and flipping the alias, or after a database restored from backup
   or reset in development. The result was a 409 on every subsequent sync and no vector side at
   all, for a reason nobody would find. It now replaces the collection it is about to fill, which
   is safe there and only there, because no alias points at it until the flip.
7. **The claim detector named policy claims "pricing".** The three families are tried in order
   and overlap on the word "charge", and pricing was first. Policy goes first now, because its
   patterns are the specific ones - they need a modal or an eligibility word beside the noun.
   This changes what a correction says and what a trace reads like; it never changes whether a
   message is refused.
8. **`build_ingestor` could not be told there is no Qdrant.** A default argument cannot tell "I
   did not pass a store" from "there is no store", so a cassette recording and an application
   test both tried to reach one. Both now pass `use_qdrant=False`, which is a configuration and
   is not the same thing as an outage.
9. **The Qdrant client ran a version handshake in its constructor**, so an unreachable instance
   was reported twice: once as a warning on stderr that nobody can act on, once as the
   `RetrieverUnavailable` that the composite exists to catch.

### Discarded

Nothing. No inherited file was rewritten wholesale and no decision recorded in one was reversed.

### The edit to `0001_initial_schema.py`

**Kept, and it is the only place it can go.** The change is one line of `downgrade()`: the
initial migration no longer runs `DROP EXTENSION IF EXISTS vector`. That is phase-0 review
finding N2, whose downgrade half was deferred to this phase by name.

The standing objection to editing a released migration is that a deployment which has already run
it will never see the change. That objection does not apply here, and the reason is worth writing
out rather than waving at:

* `upgrade()` is **byte-identical**. Nothing any deployment has already executed differs from
  what the file now says it executed, so there is no divergence to discover later.
* The change is to `downgrade()`, and a downgrade is by definition run *later*, with whatever
  code is installed at that point. A deployment that downgrades tomorrow runs tomorrow's file.
* It could not go into a new migration. Alembic runs downgrades newest-first, so `0010`'s
  `downgrade()` executes *before* `0001`'s and no later revision can stop an earlier one from
  dropping an extension. Editing `0001` is not the convenient answer; it is the only one.

What it buys: `CREATE EXTENSION vector` needs superuser, so on a managed instance an
administrator usually installs it once for the whole database before this application exists. The
upgrade's `IF NOT EXISTS` then creates nothing, and a `DROP EXTENSION` on the way down would
remove something this migration did not create - taking every `vector` column in that database,
including another application's, with it. `downgrade base` means "remove this application's
schema", not "remove pgvector". The cost is an extension left behind, which the next upgrade's
`IF NOT EXISTS` ignores.

The **dimension** fix - phase-0 finding N12, `doc_chunk.embedding` to `vector(128)` with an HNSW
cosine index - is deliberately *not* in `0001`. It is migration `0010`, where a new column type
belongs, and it refuses to run against a non-empty `doc_chunk` rather than casting rows it cannot
interpret or deleting somebody's index without being asked. The table being empty in this
repository made the easier and wrong thing available, and it was not taken: a deployment that has
rows gets a message naming `DELETE FROM doc_chunk` and `support pack knowledge sync`, and decides
for itself.

## Implementation notes

### The trust boundary, and the number

A retrieved passage reaches a model through `PromptInputs.knowledge` - layer 7, fenced by phase
3's per-render delimiter token - and through no other path. This phase adds no second way to put
text into a prompt, and that is a structural claim rather than a careful one: `Passage.for_prompt`
is the single conversion, it drops the score and the backend on the way, and
`support_core.llm.prompt` imports nothing from the retrieval side.

The evidence is `tests/test_knowledge_injection_matrix.py`, which re-runs phase 3's twenty-eight
payloads with the injection point in the corpus. Each payload is written to disk as markdown,
ingested by the real `Ingestor`, retrieved by the real retriever and rendered by the real engine
into the prompt a provider receives; the judge is phase 3's, imported rather than rewritten, and
is deliberately looser than the assembler's own matcher. **Forged lines: 0 of 28 renderings.**

Two things about how that number was arrived at, because the first draft of the test was worth
nothing. Each case asserts the payload reached **layer 7** before it asserts that nothing was
forged - the first version asserted the marker was somewhere in the prompt, which the customer's
own message satisfies, so the whole file would have passed against a retriever that returned
nothing. And the marker is a token only the document contains, checked against a knowledge block
sliced out of the rendered prompt, so "it got there" means what it says.

Two routes that did not exist before this phase get their own cases: a payload in a *heading*,
which the chunker copies into the indexed text and into the locator, and the negative -
`wobblefish` appears in layer 7 and nowhere else in the prompt.

### Versioning, and what makes the exit criterion exact

`source_version` is `r<revision>-<sha256[:12]>` over the normalised corpus, so a scheduled sync of
unchanged content keeps the version and rewrites nothing, and any edit - one word, or a file
rename, which changes every locator - produces a new one. Old chunks are marked stale, never
deleted, so the version a trace names still has text behind it. The Qdrant side versions by
*collection*: a sync builds `<prefix><source>_v<n>` and flips the alias onto it in one call, and
three revisions are retained.

The exit criterion is therefore checkable in its strong form, and
`tests/test_source_version_trace.py` checks all four parts: the old trace names A and the new one
names B; the text A names is still readable and still says what it said; the *same* long-lived
executor picks B up on its next retrieval with no restart; and the Qdrant collection that produced
the old answer still exists and still contains only version A.

`--force` exists for the one change content-addressing cannot see: the *chunker*. A code change is
a deploy, the corpus hash is unchanged, and the stored chunks are the old shape.

### Degradation, twice

With Qdrant unreachable, a **sync** commits the Postgres halves and reports `vector_error`,
exiting 0, because a pack that could not correct its knowledge while a secondary index was down
would be worse than one that degrades. A **query** drops the backend that raised
`RetrieverUnavailable`, answers from the rest, and records `retrieval_degraded` on the trace step -
"these are the best passages available *because Qdrant was down*" is a materially different fact
from "these are the best there are" when somebody is later asked why an answer was wrong.

The degradation test does not use the `qdrant` fixture and never skips: it points a store at a
port nothing listens on, so an outage is testable on a machine that has no Qdrant at all. Only
`RetrieverUnavailable` is caught; a backend that raises anything else has a bug rather than an
outage and the composite lets it through.

With *every* backend down the result is empty, and that is not papered over: an empty knowledge
block plus a factual claim is exactly what the citation guardrail refuses.

### The citation guardrail

Rule-based, three families (policy, pricing, timing), tried in that order because they overlap on
the word "charge" and policy's patterns are the specific ones. It refuses the two shapes that are
checkable: a message containing a claim that cites nothing the node was offered, and a message
citing an id the node was never given - a fabricated citation, which is worse than no citation
because it looks like grounding.

It rides phase 3's retry loop rather than adding a second one: `UncitedClaimError` is a
`StructuredOutputError` subclass, so the reject-correct-retry ladder is the one phase 3 built and
phase 3's review hardened, and the correction is drawn from a closed vocabulary - the *kind* of
claim and the ids that were offered, both of which core knows independently of what the model
said. A second failure becomes `NodeError(reason="uncited_claim")` and reaches phase 6's real
handoff, carrying the passages the node was looking at when it decided to assert something. The
reason is its own rather than `llm_invalid_output`, because "the model answered with something the
graph does not allow" and "the model asserted a policy it could not support" send a conversation
to the same place for very different causes.

`pack.yaml` gains `guardrails.citations`, on by default. A pack may turn the check off in one
visible line and may *add* patterns; there is deliberately no way to remove a core pattern,
because a pack that could delete the timing family could make "your refund arrives tomorrow" not a
claim.

What it misses is in the self-critique, in detail.

### The sample pack, and a demo that would otherwise be a lie

`tell_done`'s instructions used to *contain* the answer - "say that it takes five to seven business
days" - which is knowledge in a prompt, the thing decision Q8 forbids, and meant a change to the
published wait needed a deploy. It now takes the timing from layer 7 and cites it, and
`explain_denial` retrieves the rule behind a refusal. Editing
`packs/acme_billing/knowledge/docs/processing-times.md` and re-running the sync changes what the
next conversation says, with no restart.

That makes `support pack knowledge sync` a required step of the demo rather than an optional one,
and the README says so: against an unindexed database the agent re-asks itself once and then hands
the conversation to a person - correct, and unhelpful.

### Measured retrieval quality

Twelve hand-labelled questions over a three-document corpus, all four paths, one sync
(`tests/test_retrieval_quality.py`, printable with `pytest -s -k printable`):

| backend | recall@1 | recall@3 |
|---|---|---|
| lexical (`ts_rank_cd`) | 12/12 | 12/12 |
| dense (pgvector cosine) | 8/12 | 11/12 |
| colbert (Qdrant MaxSim) | 8/12 | 11/12 |
| composite (RRF) | 10/12 | 12/12 |

The backlog required ColBERT to be "measured against the pgvector path on the same corpus before
making it the default", and it is not the default: the composite merges all three and none of them
decides alone. On this corpus the two vector paths tie and the lexical path beats both, which is
the expected result of running a hashed-projection stand-in against a real BM25-family ranker and
is *not* a result about ColBERT - see the self-critique.

The merge is at least as good as its best input at k=3 and is worse than the lexical half at k=1.
That is written into the test as an assertion in both directions rather than left as a pleasant
surprise: fusion trades a first-place for robustness, which is the right trade at the k a
`knowledge:` block asks for and would be the wrong one at k=1.

### Composition

`support_core/knowledge/wiring.py` is the only place that turns a pack into a retriever, in the
same shape as `support_core/llm/wiring.py`, so the service, the sync CLI and the tests compose the
layer identically. A pack with no sources gets `None` and its `llm` nodes get an empty layer 7,
which is a legitimate configuration rather than a hole - the citation guardrail is what stops it
becoming an ungrounded answer. `use_qdrant=False` is a *configuration* and is kept distinct from a
Qdrant that is down, which is a *fault*: the first is silent and decided once, the second degrades
and logs on every call.

## Self-critique

Per PLAN.md step 3: what was skipped or simplified, where the code diverges from the design, which
tests are weak, and what breaks under concurrency or a crash mid-step.

### What retrieval quality is, and is not, measured against

This is the first item because it is the largest limitation of the phase and the table above will
otherwise be read as more than it is.

**The encoder is not a language model.** `DeterministicEncoder` is a hashed projection of tokens
and their character trigrams onto the unit sphere. It knows that "refunds" and "refunded" are
close; it does not know that "reimbursement" and "refund" mean the same thing, and it never will,
because there is no meaning in it. Everything the dense and ColBERT paths score is therefore a
smoothed lexical overlap wearing a vector's clothes.

**The real ColBERT model has never run here.** `FastEmbedColbertEncoder` is written and unexercised:
`fastembed` installs, and this environment cannot complete TLS to the model host
(`CERTIFICATE_VERIFY_FAILED`), so the weights cannot be fetched. This is the same shape of gap as
phase 3's unexercised `AnthropicProvider` and it gets the same treatment - the seam is real, the
code path is written, and nothing here should be read as evidence that it works. In particular the
`_check` that refuses a model whose width is not 128 has never refused anything.

So what the comparison measures is **plumbing**: that each backend returns rows, that the Qdrant
collection is built with the right comparator, that MaxSim rescoring runs, that the merge does not
lose to its inputs, and that a regression in any of those shows up as a number. It does not
measure whether late interaction is better than dense retrieval on this corpus, and the fact that
the two tie at 8/12 and 11/12 says nothing about ColBERT - it says the same encoder produced both.
The backlog's requirement was that ColBERT not become the default without a measurement, and it
has not: the composite merges all three.

**The query set is weak in two further ways.** It is twelve queries, and it was written by the
same person who wrote the corpus - so it is a test of recall against phrasings that a corpus
author found natural, which is the friendliest possible distribution. Real customers write worse
questions and the corpus was not written to answer them. Phase 8's eval harness is where a
labelled set that somebody else wrote belongs.

**A note on the lexical result.** 12/12 at both k values is a good number and not a surprising
one: the corpus is small, the queries share vocabulary with it, and `ts_rank_cd` is a proper
ranker. It should not be read as "the vector side is unnecessary". It should be read as "on a
corpus this small, with a stand-in encoder, the keyword ranker wins", which is exactly the
condition the decision to bring ColBERT forward was meant to escape and did not escape.

### What the citation check misses

It is a keyword detector over sentences. It will have false positives, which cost a turn and then
a handoff - annoying and safe - and false negatives, which are claims reaching a customer uncited,
which is the failure it exists to prevent. Named cases, all verified against the code:

* **A claim whose vocabulary is not in the families.** "Setup work is excluded from this." is a
  policy claim and passes uncited: `excluded` is in no pattern. So does "The answer is no.", which
  is a claim about whatever was asked.
* **A number written as a word.** "It should reach you in a fortnight" is not detected; "five to
  seven business days" is, because `five` is in the timing list and `fortnight` is not. The list
  is a list.
* **A claim split across sentences.** "That depends on your plan. Yours is the annual one, so no."
  is two sentences and neither is a claim on its own.
* **A claim shaped as a question.** `allow_uncited_questions` defaults to on, so "Did you know
  refunds take 90 days?" is exempt. The default is right on balance - a workflow repeating a
  number back for confirmation asserts nothing - and it is a hole.
* **Any language but English.** The patterns are English and the tokeniser is English. A pack with
  `language: pt-BR` gets no citation guardrail at all and is not told so. This one deserves a
  load-time warning and does not have one.
* **Attribution.** The largest one, and it is by design rather than by omission: one valid citation
  clears a message containing four claims, including claims the cited passage says nothing about.
  Per-claim attribution needs a model, and a model marking its own homework is not a guardrail;
  DESIGN.md 16.1's node evals and section 8's LLM judge are where that belongs. It is a test
  (`test_presence_is_checked_and_attribution_is_not`) rather than a footnote, so a later change
  that fixes it has something to delete.

The check also cannot tell a *correct* citation from an incorrect one, and it cannot tell whether
the passage was relevant. What it guarantees is narrower than it looks: that the model was shown
passages, that it named one, and that the one it named exists.

### What was skipped

* **The knowledge graph retriever** and `kg_entity` / `kg_relation` loading, which is phase 9. The
  `knowledge_graph` section of `sources.yaml` is parsed and loaded nowhere, deliberately, so a
  pack that declares one gets its typos caught now.
* **Model reranking of passages**, which section 9.1 offers "when `k` is small". An extra model
  call per retrieval is a straight charge against section 20's four-second p95 and nothing here
  can measure the benefit.
* **`LiveLookupRetriever` in full.** Section 9.1 routes "entity-specific questions" to READ tools,
  which properly needs a structured model call to extract the tool's arguments from the question -
  a second decision surface with no graph constraining it. What is built is the part where the
  *pack* has made that decision: a declared tool, trigger words, and arguments taken from `ctx`. A
  question whose answer needs an identifier only the question contains ("what is charge ch_9912
  for") does not reach a live lookup. The sample pack declares none, so this code path is
  exercised only by its own tests.
* **`refresh:` does nothing.** A source may declare `hourly`, `daily` or `weekly` and nothing in
  core schedules anything; the field exists so the scheduler phase 7 owns finds an answer rather
  than a key to invent.
* **Retrieval is not replayed from the trace.** The phase-3 deferred finding about replaying LLM
  calls has an exact analogue here: a re-executed step after a crash retrieves again, and may get
  different passages if a sync landed in between. The trace records what *was* used, so an
  investigation is not harmed; a replay is not byte-identical. Phase 7 owns replay.
* **The ColBERT multivector has no HNSW index** (`m=0`), on Qdrant's own guidance that late
  interaction is a reranker over an indexed first stage. The two-stage path is implemented and
  `prefetch=0` (a linear scan) is what the tests use, so the *tuned* path - dense prefetch then
  MaxSim rescoring - is written and only exercised through the composite's default. On a corpus of
  eleven chunks the distinction is invisible; on a large one it is the whole latency story, and
  nothing here has measured it.

### Where the code diverges from DESIGN.md

Beyond the deviations the plan already listed and the decisions log already settled:

* **Chunking does not merge short sections**, against the plan's own promise. Argued above.
* **The lexical query is a disjunction**, which section 9.1 does not discuss because it does not
  discuss tsquery at all. It is a retrieval-quality decision made in a repository module and it is
  the kind of thing that belongs in a pack's configuration eventually.
* **`Passage.for_prompt` drops the score.** The model is told a passage's id, text, source and
  version, and not how well it matched or which backend found it. Neither is evidence, and both
  would be numbers a model might reason about.
* **Second stateful dependency**, already decided (BACKLOG.md, 2026-09-06) and restated in the
  plan.

### Which tests are weak

* **The retrieval-quality set**, above.
* **`html_crawl` has never met a web server.** It is tested against a stub fetcher, which is what
  keeps the suite off the network, and that means the parts a real server exercises - redirects,
  encodings, compressed responses, a page that is 40 MB - are untested. `http_fetch` itself is
  marked `no cover` and has never run in this repository.
* **The injection matrix relies on the deterministic encoder to retrieve the hostile passage.**
  Each case aims a unique marker token at the lexical half, which is reliable, but it means the
  matrix proves the *rendering* is safe rather than that a hostile document is easy to retrieve.
  That is the right property to test, and it is worth being explicit that the test does not model
  an attacker who has to win a ranking competition to be seen.
* **Nothing tests two syncs at once** (see below).
* **The `qdrant` fixture skips when Qdrant is absent.** That is deliberate - a suite that could not
  run without the vector side would contradict the phase's own degradation claim - but it means a
  CI job whose Qdrant failed to start reports green with eight fewer assertions. The skip is
  visible in the summary and nothing enforces it.
* **The claim-detector matrix is my own vocabulary.** Eleven claims and seven non-claims, written
  by the person who wrote the patterns. It is a regression net, not a measurement.

### What breaks under concurrency or a crash mid-step

Retrieval itself is a read outside the checkpoint transaction, and that is now asserted rather
than assumed (`test_retrieval_happens_outside_the_checkpoint_transaction`), so nothing here can
cost a conversation its durability. The failure modes are all in the *sync*, which is not in a
turn's write path and is correspondingly less defended:

1. **Two concurrent syncs of one source duplicate the corpus.** `sync_source` reads
   `doc_source.revision` in one transaction and writes chunks in another, with nothing held
   between them. Two syncs started together both compute revision *n+1*; if they read the same
   corpus they compute the same `source_version` and both insert a full set of chunks under it, and
   `mark_stale` keeps both because they match `keep_version`. The result is every passage
   duplicated in the live version - a retrieval returning the same text twice, and a `k` of 3
   carrying two distinct facts instead of three. Nothing detects it. The fix is a transaction-level
   advisory lock keyed on the source id, which is the same device the engine uses per conversation,
   and it is not in this phase because the sync is a scheduled job that a deployment runs once at
   a time and the fix deserves its own test rather than a hurried one. **Recorded as a deferred
   finding.**
2. **A crash between the chunk commit and the alias flip leaves the two stores on different
   versions.** Postgres has version *n+1* live; the Qdrant alias still points at *n*. Retrieval
   then merges passages carrying two different `source_version` values in one answer. It is not
   silent - the trace records both, which is exactly what the trace is for - and the next sync
   fixes it, but a passage set that straddles a revision is a thing a reader of the trace has to
   understand rather than a thing that cannot happen. Making it impossible needs the vector index
   inside the same transaction as the rows, which is not available across two stores.
3. **The retention sweep can outrun a trace.** Three revisions are kept. A source synced four times
   between an answer and an investigation has lost the collection *and* the rows the answer used,
   and the trace then names a version nothing can show. `KEEP_COLLECTIONS` is a constructor
   argument and the default suits a weekly refresh; a daily one wants more, and nothing warns the
   operator of the relationship between the refresh interval and how long a wrong answer stays
   explicable.
4. **A `doc_chunk` row deleted while a Qdrant point survives** is handled (the retriever logs and
   skips a point whose row is gone) but the reverse - a point whose payload version disagrees with
   the row's - is logged and *used*. The row wins, which is the right choice, and the log line is
   the only trace of the disagreement.

### A note on the sample pack's manifest

`packs/acme_billing/pack.yaml` writes its `guardrails:` block out although `enabled: true` is the
default. That is not redundancy: the argument for adding the key at all was that a pack's
guardrail configuration should be visible to somebody reading the pack, and a block that only
appears when it is turned *off* would make the safe case the invisible one.

### Verification

Every command below was run from a clean tree on 2026-09-07, in this order, against the
docker-compose Postgres and Qdrant. Results are recorded in the "Resolution" section a reviewer
will add; the run this phase closes on is summarised there.

```
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
.venv/Scripts/python.exe -m mypy support_core
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pytest -q -m live
.venv/Scripts/python.exe tests/verify_phase_2_resolution.py
.venv/Scripts/python.exe -m support_core.cli.main pack validate packs/acme_billing
.venv/Scripts/python.exe -m alembic downgrade base && .venv/Scripts/python.exe -m alembic upgrade head && .venv/Scripts/python.exe -m alembic check
```

The cassettes were regenerated with `python -m tests.cassettes.build_cassettes` because this phase
changes a prompt they cover: `tell_done`'s instructions no longer state the timing, and layer 7 is
no longer empty for the two nodes that carry a `knowledge:` block.

### Two notes for whoever reviews this

**The duplicate plan commit.** `d7c49c8 phase-5: plan (PLAN.md step 1)` has a twin at `0514b4b`
with the same message, left by the first attempt at this phase that was discarded. Both are
published, so neither is being rebased or dropped - rewriting history that somebody else may have
is a worse problem than a confusing log. The plan that governs this phase is the one in this file,
which is `d7c49c8`'s, amended by the section above.

**Concurrent work.** While this phase was in flight, another process committed the library half of
it as `2a28eaf phase-5: snapshot the in-flight knowledge layer` and added two review documents
(`46ff6c3`, `25e6d35`) and a demo-client fix (`ee0db00`). Nothing in those commits contradicts
this work - the security review explicitly read the in-flight knowledge layer for context and
recorded no finding against it - and nothing here was rebased or reverted.

## Independent review

Reviewed by an agent that did not write this code, against DESIGN.md sections 3 (principles 5 and
7), 9.1 to 9.3, 11.2 (layer 7), 13, 14, 17 (`doc_source`, `doc_chunk`), 20, BACKLOG.md's Phase 5
section, PLAN.md, and reviews/phase-0.md through phase-4.md, phase-w.md and phase-6.md. The
self-critique is unusually candid and names most of what a careless review would have found first
(concurrent syncs duplicating a corpus, the crash window between the chunk commit and the alias
flip, retention outrunning a trace, non-English packs getting no guardrail, citation-check
attribution). This review does not re-litigate those; it verifies the exit criterion independently
and hunts in places the self-critique does not look: the sync CLI's actual file-system boundary
(as opposed to the validator's), what the citation guardrail's own `_ACTION` exemption lets through
in a single sentence, and whether a node's `knowledge:` block narrows what it can search the way
the plan and DESIGN.md 9.1 both say it does.

**Exit criterion: met.** `tests/test_source_version_trace.py` was read in full and re-run as part
of the full suite (below); it checks the strong reading BACKLOG.md asks for - an old trace still
names the old `source_version`, the old chunks are still readable and still say what they said, a
single long-lived `Executor` picks up a re-sync's new version on its very next retrieval with no
restart, and the Qdrant collection that produced the old answer still exists and still contains
only that version, provable by querying it directly through its own name rather than through the
alias. All five assertions in that file hold, and the injection matrix (`tests/test_knowledge_
injection_matrix.py`) re-confirms 0 forged lines across 28 payloads ingested and retrieved through
the real pipeline. The exit criterion being met is a narrower claim than "the phase is safe" -
three of the four findings below are must-fix or should-fix precisely because they sit next to the
exit criterion rather than inside it: a corpus that was never supposed to be in the index at all
(K1), a claim category the citation check was told to ignore and that nothing else has caught since
(K2), and a containment promise the design makes that the shipped schema has no field for (K3).

**What was run.** The full verification list at the bottom of this file, against the same
docker-compose Postgres and Qdrant this phase's own author used (both confirmed healthy via
`docker compose ps` before starting): `ruff check`, `ruff format --check`, `mypy support_core`, the
full `pytest -q` suite (**1600 passed, 2 deselected, 0 failed**, 15m21s - the 2 deselected are the
`-m live` cases that need an API key this environment does not have, same as every prior phase),
`support pack validate packs/acme_billing` (well-formed, 18 warnings - the same 18 phase 6's review
recorded, none of them phase 5's), and `alembic downgrade base` / `upgrade head` / `check` (clean
both ways, `No new upgrade operations detected.`).

**What was attacked.** Read every module under `support_core/knowledge/` and `support_core/
guardrails/` line by line rather than sampling; wrote and ran a real pack whose `knowledge/
sources.yaml` names a `markdown_dir` path that walks out of the pack directory, and watched
`read_markdown_dir` - the function the shipped `support pack knowledge sync` CLI actually calls -
read this repository's own internal engineering docs (`docs/authoring-a-pack.md`, `docs/pack-
schema-reference.md`) as if they were the pack's citable corpus; called `support_core.guardrails.
outbound.check_citations` directly with sentences that combine a first-person action with a
pricing or timing fact in the one sentence, to see whether the detector's own tested exemption for
action sentences (`tests/test_citation_guardrail.py::test_a_first_person_action_is_somebody_else_
s_guardrail`) also exempts the fact riding alongside the action; and read `support_core/graph/
nodes.py`'s `KnowledgeQuery`, `support_core/knowledge/wiring.py`'s `build_retriever`, and `support_
core/engine/executor.py`'s `_retrieval` together to check the plan's own claim that a node "cannot
widen its own `k` or query anything the graph did not declare" against what a `knowledge:` block
can actually restrict.

### Findings

| ID | severity | location | description | recommendation |
|----|----------|----------|-------------|-----------------|
| K1 | must-fix | `support_core/knowledge/sources.py:54-55` (`MarkdownDirSource.resolve`), `support_core/knowledge/sources.py:202-217` (`path_findings`, the only place the boundary is enforced), `support_core/knowledge/ingest.py:216-238` (`read_markdown_dir`), `support_core/cli/main.py:60-118` (`knowledge_sync`) | `support pack knowledge sync` - the command README.md's own four-line demo startup runs, and the command DESIGN.md 9.2 says is the whole of "fixing knowledge" ("edit the source, run `support pack knowledge sync`") - never calls `path_findings`, the function that refuses a `markdown_dir` source whose `path` escapes the pack directory. That check exists only in `support_core/graph/validator.py:374-376`, reached by `support pack validate`, a **separate command the sync workflow does not run**. Reproduced: built a pack whose `knowledge/sources.yaml` declared `path: "../docs"`; `load_sources()` (what `knowledge sync` actually calls) accepted it with no error, while `path_findings()` on the same input correctly reports `"path '../docs' escapes the pack directory"`. Calling `read_markdown_dir` - the function `Ingestor.sync_source` calls on every sync - then read and would index this repository's own internal `docs/authoring-a-pack.md` (31,823 chars) and `docs/pack-schema-reference.md` (24,278 chars) as the pack's corpus: content nobody meant to be customer-facing, retrievable and citable to a customer with a fabricated-looking `locator`. No test anywhere (`test_knowledge_ingest.py`, `test_knowledge_cli.py`) exercises a `sync` of an escaping or absolute path; the only test of the boundary (`test_knowledge_sources.py:139`) is against `path_findings` in isolation. The same root cause - a check written into the validator and never wired into the code that actually opens a socket or a file - very likely also means `html_crawl`'s `path_findings` scheme check (`url` must be `http`/`https`) is bypassed by `sync` too, since `crawl()` never calls it either; not independently reproduced here for lack of time, but worth the same fix. | Move the containment check (or call `path_findings`) into `MarkdownDirSource.resolve()` or `read_markdown_dir()` itself, so a sync refuses the escape rather than only warning about it in a command an operator may never run before syncing. Do the equivalent for `html_crawl`'s scheme (and consider blocking loopback/link-local targets while at it, since `path_findings` currently only rejects a non-`http(s)` scheme and would pass `http://169.254.169.254/...`). |
| K2 | must-fix | `support_core/guardrails/outbound.py:134-148` (`_ACTION`), `:227` (`find_claims`'s `_ACTION.match` short-circuit), endorsed by `tests/test_citation_guardrail.py:114-120` | `_ACTION` matches a whole sentence and removes it from claim detection on the theory that "the check it needs is DESIGN.md 14's forbidden-promise check... phase 7's" (the module's own docstring, line 147). That check does not exist anywhere in this codebase yet (`grep -rn forbidden support_core` outside comments returns nothing); the sentence therefore passes through **no guardrail at all**, not a weaker one. Because the unit of detection is the whole sentence, any pricing or timing fact that rides in the *same* sentence as the action verb is exempted along with it. Reproduced directly against `check_citations`: `"I have issued a refund of $500 to your card ending 4242, which will arrive in 3 to 5 business days."` returns `ok=True, claims=()` - an uncited dollar amount and an uncited delivery window, in one sentence a model is very likely to actually write, pass with nothing checking them. Splitting the same content into two sentences ("I have refunded you. It will arrive in 5 to 7 business days.") is correctly caught as a timing claim - so the gap is specifically about phrasing, and an LLM has no reason to prefer the phrasing that gets checked. The self-critique's "What the citation check misses" section lists five gaps in the detector's own vocabulary and does not mention this one, which is a different shape of gap: not a pattern the detector fails to recognise, but a claim category the detector was deliberately told to skip on the assumption that a different, unbuilt guardrail covers it. | Either scope the `_ACTION` exemption to the clause asserting the action rather than the whole sentence (don't skip a sentence that also matches a pricing/timing pattern outside the action's own object), or require a citation for the pricing/timing part regardless of `_ACTION` co-occurrence and leave the action assertion itself to the future forbidden-promise check. At minimum, name this gap explicitly in the self-critique so phase 7's forbidden-promise work is scoped with it in mind rather than rediscovering it. |
| K3 | should-fix | `support_core/graph/nodes.py:95-104` (`KnowledgeQuery`: only `query` and `k`), `support_core/knowledge/wiring.py:84-101` (`build_retriever`: `source_ids = sources.source_ids()`, unconditionally every document source the pack declares), `support_core/engine/executor.py:1347-1368` (`_retrieval`: hands every node the one pack-wide composite, narrowed only by the live-lookup gateway) | DESIGN.md 9.1 states "Packs choose which backends a given `llm` node may use via the node's `knowledge:` block", and this phase's own plan says the per-node retrieval closure is built "in the same shape as `tool_gateway` (the node cannot widen its own `k` or query anything the graph did not declare)" (line 115-116). Neither is true of the shipped schema: `KnowledgeQuery` has no field to name a subset of sources, and `build_retriever` composes *every* `markdown_dir`/`html_crawl` source the pack has ever declared into the one `CompositeRetriever` every node shares - a node's `knowledge:` block can narrow `k` and the query text and nothing else. `DocumentRetriever` and `ColbertRetriever` both already accept a `source_ids` sequence at construction and `tests/test_knowledge_retrieval.py:109` (`test_a_retriever_scoped_to_a_source_cannot_see_another`) proves the underlying mechanism works when a retriever is built with a narrower set directly - but nothing in the executor or the wiring ever builds one narrower than "the whole pack" for a specific node. Not observable as a live defect in `packs/acme_billing` today, because both its sources are meant to be customer-facing, but it is a real containment gap for the first pack that mixes an internal-only source with a customer-facing one, and it is a plain divergence from both the design's sentence and the plan's own claim, recorded nowhere in the "Where the code diverges from DESIGN.md" section. | Add a `sources: list[str] \| None` field to `KnowledgeQuery`, thread it through `RetrievalRequest`, and filter the per-node retrieval to that subset (the retrievers already support it; only the wiring needs to change) - or, if the scope was deliberately deferred, say so in reviews/phase-5.md next to the other recorded deviations rather than leaving the plan's contradicting claim as the only record. |
| K4 | nit | `support_core/knowledge/ingest.py:493-495` (`_prune`), `support_core/knowledge/qdrant.py:255-273` (`QdrantStore.prune`) | `QdrantStore(url, keep=0)` is accepted at construction with no validation, and `prune(source_id, keep=0)` computes `sorted(versions, reverse=True)[0:]` - every collection, including the one the sync just built and just flipped the alias onto, in the same sync call. A deployment reasoning "I don't need history, keep the footprint small" and setting `keep=0` would delete the collection an in-flight query is about to be pointed at, in the same operation that created it. | Reject `keep < 1` at `QdrantStore.__init__`, or floor `prune`'s effective limit at 1. |

### Reproduction notes

**K1**, exact commands:

```
python -c "
from pathlib import Path
from support_core.knowledge.sources import load_sources, path_findings
p = Path('tmp_traversal_pack')            # sources.yaml: documents: [{id: escaped, type: markdown_dir, path: '../docs'}]
s = load_sources(p)                        # what `knowledge sync` calls: succeeds, no error
print(list(path_findings(p, s)))           # what `pack validate` calls: [('escaped', \"path '../docs' escapes the pack directory\")]
"
```

then, calling `read_markdown_dir` (what `Ingestor.sync_source` calls) against the same pack directly
returned two documents named `authoring-a-pack.md` and `pack-schema-reference.md` with the full text
of this repository's `docs/` directory - outside the scratch pack entirely.

**K2**:

```
python -c "
from support_core.guardrails.outbound import check_citations
v = check_citations('I have issued a refund of \$500 to your card ending 4242, which will arrive in 3 to 5 business days.', citations=[], offered=['c1'])
print(v.ok, v.claims, v.problems)   # True () ()  -- nothing detected, nothing required
"
```

**K3**: read `support_core/graph/nodes.py` (`KnowledgeQuery` has exactly `query: str` and `k: int`,
`extra="forbid"` so no undeclared field could sneak past validation), `support_core/knowledge/
wiring.py`'s `build_retriever` (`source_ids = sources.source_ids()` is the pack's full list, passed
once to both `DocumentRetriever` and `ColbertRetriever` at startup), and `support_core/engine/
executor.py`'s `_retrieval` (composes `self.retriever` - the one built at startup - with only the
per-node live-lookup gateway; no source filter). Cross-checked against `tests/test_knowledge_
retrieval.py:109`, which proves the retriever-level mechanism exists and is simply never reached
from a node.

### Missed by the self-critique

The self-critique is thorough about concurrency, crash windows, retrieval-quality honesty and the
citation detector's *vocabulary* gaps. What it does not name: that the sync path and the validator
enforce two different sets of rules over the same input and only one of them is on the path an
operator actually runs (K1); that the `_ACTION` exemption it defends on the record (`tests/
test_citation_guardrail.py:114-120`, and the self-critique's own "policy claims" discussion) hands
a free pass to whatever pricing or timing fact happens to share a sentence with it, not only to the
action assertion itself (K2); and that "the node cannot widen its own `k` or query anything the
graph did not declare" is stated as an achieved property in the plan (line 115-116) without a test
or a sentence anywhere checking that a node cannot see a source its own `knowledge:` block never
named (K3) - the design's own sentence about per-node backend choice is quietly not implemented.

### Verification

Run from a clean tree on 2026-09-16, `.venv/Scripts/python.exe`, against the docker-compose Postgres
and Qdrant (`docker compose ps` confirmed both `healthy` before starting).

| Command | Result |
|---------|--------|
| `python -m ruff check .` | `All checks passed!` (exit 0) |
| `python -m ruff format --check .` | `196 files already formatted` (exit 0) |
| `python -m mypy support_core` | `Success: no issues found in 105 source files` |
| `python -m pytest -q` | `1600 passed, 2 deselected in 921.50s (0:15:21)` - the 2 deselected are `-m live`, which needs an API key this environment does not have |
| `support pack validate packs/acme_billing` | `acme-billing: well-formed (18 warning(s))`, exit 0 - the same 18 (16 `graph.assignment_optional`/`graph.state_type_unresolved` from phase 4's deferred R8, 2 `graph.confirm_exempt`) phase 6's review recorded; none are phase 5's |
| `alembic downgrade base` | all ten revisions down cleanly |
| `alembic upgrade head` | all ten revisions up cleanly |
| `alembic check` | `No new upgrade operations detected.` |

No test was run twice against the shared database while another process might have been using it
(phase-0 finding N9's collision mode, which phase 6's review hit); this review ran alone.

**Verdict: 2 must-fix, 1 should-fix, 1 nit.** The exit criterion holds, and it holds under the same
kind of adversarial pressure phase 6's review applied to the interrupt stack: the version trace is
exact in both directions, the old collection survives untouched, and a single long-lived executor
picks up a re-sync with no restart. The injection matrix's 0-forged-lines result also holds against
a corpus ingested and retrieved by the real pipeline rather than a hand-built `Passage`. What does
not hold is the boundary around what gets *into* that corpus in the first place (K1) and the
completeness of what stops an *ungrounded* claim from leaving it (K2) - both are the same class of
gap the phase's own guiding principles (5, 7) exist to close, found next to a phase that otherwise
does exactly what it says it does.

