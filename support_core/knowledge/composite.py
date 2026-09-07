"""``CompositeRetriever``. Implements DESIGN.md section 9.1's "fans out, merges, and
deduplicates", and the graceful degradation BACKLOG.md's Qdrant decision was justified on.

Three jobs, and the third is the one that is easy to get wrong:

1. **Fan out in parallel.** DESIGN.md section 20 names "parallel retrieval" as one of the two
   levers on the four-second p95, beside prompt caching. Three sequential round trips would spend
   the budget the design allocated to the whole turn.

2. **Merge and deduplicate.** By reciprocal rank fusion, for the reason given on
   :func:`support_core.knowledge.document.fuse`: the three backends' scores are in three
   incomparable units and only their orderings mean anything.

3. **Survive a backend that is down.** A retriever that raises
   :class:`~support_core.knowledge.types.RetrieverUnavailable` is dropped for that call, logged,
   and recorded on the result so the trace can say the answer was given without the vector side.
   With Qdrant stopped the lexical and dense halves still answer, which is the whole of the
   argument for splitting the two stores. With *everything* down the result is empty, and that is
   not papered over: an empty knowledge block plus a factual claim is exactly what the citation
   guardrail refuses, so the conversation reaches a human rather than an ungrounded answer.

The ids are assigned here, last, because this is the only place that knows the whole set the
model will see. ``k1``, ``k2``, ... in final rank order - short, because the model has to repeat
them in ``citations``, and positional, so a passage's id says where it ranked.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from support_core.knowledge.document import fuse
from support_core.knowledge.types import Passage, RetrievalRequest, Retriever, RetrieverUnavailable

logger = logging.getLogger(__name__)

ID_PREFIX = "k"


@dataclass(frozen=True, slots=True)
class Retrieval:
    """What one retrieval produced, and what it could not reach.

    ``degraded`` is not decoration. DESIGN.md section 15 wants the retrieval span to carry what
    happened, and "these three passages are the best available *because Qdrant was down*" is a
    materially different fact from "these three passages are the best there are" when somebody is
    later asked why an answer was wrong.
    """

    passages: tuple[Passage, ...] = ()
    degraded: tuple[str, ...] = ()
    """Backends that were unreachable, by name."""

    errors: tuple[str, ...] = field(default=())

    def as_trace(self) -> list[dict[str, object]]:
        """What goes on the trace step (DESIGN.md sections 9.2, 15: "passage versions")."""
        return [
            {
                "id": passage.id,
                "source_id": passage.source_id,
                "source_version": passage.source_version,
                "locator": passage.locator,
                "score": round(passage.score, 6),
                "backend": passage.backend,
            }
            for passage in self.passages
        ]


class CompositeRetriever:
    """Every backend a pack configured, merged (DESIGN.md section 9.1)."""

    name = "composite"

    def __init__(self, retrievers: Sequence[Retriever]) -> None:
        self.retrievers = tuple(retrievers)

    def plus(self, extra: Sequence[Retriever]) -> "CompositeRetriever":
        """This composite with more backends, as a *new* object.

        New rather than mutated because the per-node backends - a live lookup holding a gateway
        that spends one turn's tool budget - must not outlive the node that built them, and a
        composite the executor holds across turns is exactly what they would outlive if this
        appended in place.
        """
        return CompositeRetriever([*self.retrievers, *extra])

    async def gather(self, request: RetrievalRequest) -> Retrieval:
        """The full result, including what was unreachable. :meth:`retrieve` is the protocol."""
        if not self.retrievers:
            return Retrieval()
        results = await asyncio.gather(
            *(backend.retrieve(request) for backend in self.retrievers),
            return_exceptions=True,
        )
        ranked: list[Sequence[Passage]] = []
        degraded: list[str] = []
        errors: list[str] = []
        for backend, result in zip(self.retrievers, results, strict=True):
            name = getattr(backend, "name", type(backend).__name__)
            if isinstance(result, RetrieverUnavailable):
                # Exactly this type, and nothing wider. A backend that raises something else has
                # a bug rather than an outage, and swallowing it here would turn every future
                # programming error in a retriever into a quietly worse answer.
                logger.warning("retrieval backend %s unavailable: %s", name, result)
                degraded.append(name)
                errors.append(f"{name}: {result}")
                continue
            if isinstance(result, BaseException):
                raise result
            ranked.append(result)
        merged = fuse(ranked)[: request.k]
        for position, passage in enumerate(merged, start=1):
            passage.id = f"{ID_PREFIX}{position}"
        return Retrieval(passages=tuple(merged), degraded=tuple(degraded), errors=tuple(errors))

    async def retrieve(self, request: RetrievalRequest) -> Sequence[Passage]:
        return (await self.gather(request)).passages
