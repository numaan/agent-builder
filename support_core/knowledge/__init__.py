"""The knowledge layer. Implements DESIGN.md section 9.

Where to start reading, in the order the data moves:

* :mod:`~support_core.knowledge.sources` - ``knowledge/sources.yaml``, typed. What a pack
  declares, checked at load rather than at three in the morning.
* :mod:`~support_core.knowledge.ingest` - section 9.3's pipeline: fetch, normalise, chunk, embed,
  upsert, mark stale. Behind ``support pack knowledge sync``.
* :mod:`~support_core.knowledge.chunking` - "chunk by headings (target 300 to 500 tokens)", and
  the locator that makes a citation checkable.
* :mod:`~support_core.knowledge.embedding` - the encoder seam, and the local deterministic
  stand-in this deployment actually runs.
* :mod:`~support_core.knowledge.qdrant` - the vector store: one collection per source revision,
  an alias flipped per sync, MaxSim over ColBERT multivectors.
* :mod:`~support_core.knowledge.document`, :mod:`~support_core.knowledge.live` - the backends.
* :mod:`~support_core.knowledge.composite` - the merge, and the graceful degradation the second
  store was accepted on.
* :mod:`~support_core.knowledge.wiring` - how a pack becomes a retriever.

Two things this package does **not** own, and should not be made to. The citation guardrail is
:mod:`support_core.guardrails.outbound`, because it is a guardrail and section 14 is where a
reader will look for it. And a passage's route into a prompt is
:mod:`support_core.llm.prompt`'s layer 7 and nothing else: this package converts a
:class:`~support_core.knowledge.types.Passage` into that shape and knows nothing more about
prompts, which is what keeps the trust boundary from moving when retrieval changes.

The knowledge graph (``kg_entity``, ``kg_relation``) is phase 9. Its section of
``sources.yaml`` is parsed here and loaded nowhere.
"""

from support_core.knowledge.composite import CompositeRetriever, Retrieval
from support_core.knowledge.types import (
    Passage,
    RetrievalRequest,
    Retriever,
    RetrieverUnavailable,
)

__all__ = [
    "CompositeRetriever",
    "Passage",
    "Retrieval",
    "RetrievalRequest",
    "Retriever",
    "RetrieverUnavailable",
]
