"""The knowledge layer's vocabulary. Implements DESIGN.md section 9.1's ``Passage`` and
``Retriever``.

Section 9.1 gives both verbatim, and this module is them with two changes, both stated here
rather than buried:

* **``Passage`` gains an ``id``.** Section 9.2 requires that "every passage handed to the model
  carries an inline id" and that the model's ``citations`` name those ids; the design's own
  class has five fields and none of them can be that id, because ``source_id`` is shared by
  every chunk of a document and ``locator`` is not something a model should have to retype. The
  id is assigned by :class:`~support_core.knowledge.composite.CompositeRetriever` at the point
  the passages become a *set* - ``k1``, ``k2``, ... - because that is the only place that knows
  what else the model is being shown.

* **``Passage`` gains a ``backend``.** Which retriever produced a passage is a fact a trace
  wants (section 15 lists "passage versions" among the span attributes) and a fact the merge
  needs, and reconstructing it afterwards from the locator is guesswork.

There are two passage types in this repository and there should not be a third.
:class:`Passage` is this one, and :mod:`support_core.handoff.packet` re-exports it rather than
declaring its own. The other is :class:`support_core.llm.prompt.Passage`, which is the *render*
shape: :mod:`support_core.llm.prompt` imports nothing from the retrieval side on purpose,
because the prompt boundary is the one surface in the system that must not move when something
else changes.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from support_core.graph.context import ConversationContext
from support_core.llm.prompt import Passage as PromptPassage


class Passage(BaseModel):
    """One retrieved piece of text, and everything needed to trace it (DESIGN.md section 9.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    """The inline id the model cites (section 9.2). Empty until the composite assigns it."""

    text: str
    source_id: str
    source_version: str
    """The version of the source this text was indexed from.

    This is the field the phase's exit criterion is about: it is written into the trace step at
    the moment the passage is used, so an answer given last week names the revision that was
    live last week even after the document has been edited and re-synced twice."""

    locator: str
    """``url#anchor``, ``document path + heading``, or a knowledge-graph path (section 9.1)."""

    score: float = 0.0
    backend: str = ""
    """Which retriever produced it: ``lexical``, ``dense``, ``colbert``, ``live``."""

    def cite(self) -> str:
        """How this passage is named in a handoff packet or a log line."""
        return f"{self.id} ({self.source_id}@{self.source_version}, {self.locator})"

    def for_prompt(self) -> PromptPassage:
        """The render shape layer 7 takes (:class:`support_core.llm.prompt.Passage`).

        The conversion goes this way and never the other, which is the layering the module
        docstring describes: retrieval knows what a prompt needs, and the prompt boundary knows
        nothing about retrieval. Every caller that puts a passage in front of a model goes
        through here, so there is exactly one place that decides what a model is told about a
        passage - its id, its text, its source and its version, and not its score or which
        backend found it, because neither is evidence and both would be noise the model might
        reason about.
        """
        return PromptPassage(
            id=self.id,
            text=self.text,
            source=self.source_id,
            version=self.source_version,
        )


class RetrievalRequest(BaseModel):
    """What a retriever is asked for.

    Section 9.1's protocol is ``retrieve(query, ctx, k)``. It is bundled into one object for the
    same reason phase 3 bundled :class:`~support_core.engine.hooks.SlotRequest`: the knowledge
    layer already needs a fifth thing (which node asked, for the trace) and a positional
    signature cannot grow without every implementation changing at once.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    ctx: ConversationContext
    k: int = Field(default=3, ge=1, le=50)
    node_id: str = ""


@runtime_checkable
class Retriever(Protocol):
    """DESIGN.md section 9.1's one protocol, which every backend implements."""

    name: str

    async def retrieve(self, request: RetrievalRequest) -> Sequence[Passage]:
        """Return at most ``request.k`` passages, best first.

        A backend that cannot answer raises :class:`RetrieverUnavailable`. It must not return an
        empty list to mean "I am broken": the composite drops an unavailable backend and keeps
        the rest, and "nothing matched" and "I am down" have to be distinguishable for that to
        be a degradation rather than a silent hole.
        """
        ...


class RetrieverUnavailable(RuntimeError):
    """A retrieval backend could not be reached.

    Caught by :class:`~support_core.knowledge.composite.CompositeRetriever`, which drops the
    backend for that call and answers from the others. This is the whole of the "retrieval
    degrades rather than dies" claim, so it is a named type and not a bare ``Exception``: a
    backend that raises something else is a bug, and the composite lets it through.
    """
