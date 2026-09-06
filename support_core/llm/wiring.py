"""Building the LLM layer for a pack, and the ``ask`` node's real slot extractor.

DESIGN.md section 11.1 puts model choice on the pack ("per pack with per-node override") and
section 5.1's manifest is where a pack says so; this module is the one place that reads those
settings, so nothing else has to know that ``confidence_threshold`` lives on ``llm:`` and
``window_messages`` on ``memory:``.

:class:`StructuredSlotExtractor` is what reviews/phase-2.md asked phase 3 to deliver: the
``ask`` node is unchanged and its ``extract_slots`` *hook* is replaced, so there are not two ways
to fill a slot. :class:`StructuredConfirmClassifier` is phase 4's twin of it, for the one
question in the system whose wrong answer moves money.
"""

from typing import Any

from support_core.engine.hooks import ConfirmDecision, ConfirmRequest, SlotRequest
from support_core.llm.prompt import PromptBudget, TranscriptMessage
from support_core.llm.provider import LLMProvider
from support_core.llm.service import ConfirmationRequest as ServiceConfirmationRequest
from support_core.llm.service import LlmService, ModelChoice, Sleeper
from support_core.llm.service import SlotRequest as ServiceSlotRequest


def service_for_pack(
    pack: Any, provider: LLMProvider, *, sleep: Sleeper | None = None
) -> LlmService:
    """An :class:`~support_core.llm.service.LlmService` configured from ``pack.yaml``.

    ``pack`` is typed loosely to keep :mod:`support_core.llm` free of a dependency on the graph
    package's :class:`~support_core.graph.pack.Pack`; it needs ``manifest``, ``persona`` and
    ``policies``.
    """
    manifest = pack.manifest
    return LlmService(
        provider,
        persona=pack.persona,
        policies=pack.policies,
        models=ModelChoice(
            default=manifest.llm.default_model, escalation=manifest.llm.escalation_model
        ),
        budget=PromptBudget(**manifest.llm.prompt_budget),
        confidence_threshold=manifest.llm.confidence_threshold,
        max_tool_iterations=manifest.llm.max_tool_iterations,
        retries=manifest.llm.retries,
        max_tokens=manifest.llm.max_output_tokens,
        sleep=sleep,
    )


class StructuredConfirmClassifier:
    """DESIGN.md section 6.2: a ``confirm`` node "requires an explicit yes".

    Fills the ``confirm_decision`` hook. Deliberately narrow: it asks the model how the reply
    *reads* and returns that, and it is the ``confirm`` node - not this, and not the model -
    that decides an approval follows. Two things it does not do:

    * it does not fall back to the keyword matcher when the model fails. A confirmation the
      system had to guess at is not a confirmation; the failure propagates, the node errors and
      a human picks the conversation up.
    * it does not lower the bar for "yes" when it is confident. ``unclear`` is returned as
      ``unclear``, and the node asks again.
    """

    def __init__(self, service: LlmService, *, threshold: float = 0.0) -> None:
        self.service = service
        self.threshold = threshold or service.confidence_threshold

    async def __call__(self, request: ConfirmRequest) -> ConfirmDecision:
        reading = await self.service.read_confirmation(
            ServiceConfirmationRequest(
                node_id=request.node_id,
                prompt=request.prompt,
                reply=request.reply,
                tool=request.tool,
                args=dict(request.args),
                summary=request.ctx.summary,
                window=[
                    TranscriptMessage(author=author, text=text) for author, text in request.window
                ],
            )
        )
        answer = reading.answer
        if answer == "yes" and reading.confidence < self.threshold:
            # A "yes" the model is not sure it read correctly is exactly the case where asking
            # again is cheap and being wrong is not (DESIGN.md section 11.3's threshold, applied
            # where it matters most).
            answer = "unclear"
        return ConfirmDecision(answer=answer, confidence=reading.confidence)


class StructuredSlotExtractor:
    """DESIGN.md section 6.2: "On resume, extracts slots via structured output."

    Fills the ``extract_slots`` hook. Two things it deliberately does not do:

    * it does not write a slot the model left unset, so an unanswered question stays unanswered
      and the ``ask`` node's own graph decides what that means, and
    * it does not fall back to the phase-2 default (whole reply into the first slot) when the
      model fails. A guessed slot value is a value the rest of the workflow will treat as the
      customer's own words; the failure is raised, and the ``ask`` node routes it as a node
      error, which hands off.
    """

    def __init__(self, service: LlmService) -> None:
        self.service = service

    async def __call__(self, request: SlotRequest) -> dict[str, Any]:
        extraction = await self.service.extract_slots(
            ServiceSlotRequest(
                node_id=request.node_id,
                slots=list(request.slots),
                state_model=request.state_model,
                prompt=request.prompt,
                reply=request.reply,
                state=dict(request.state),
                summary=request.ctx.summary,
                window=[
                    TranscriptMessage(author=author, text=text) for author, text in request.window
                ],
            )
        )
        slots = extraction.slots
        values = (
            slots.model_dump(mode="json", exclude_unset=True)
            if hasattr(slots, "model_dump")
            else dict(slots)
        )
        return {
            name: value
            for name, value in values.items()
            if name in request.slots and name not in extraction.unfilled and value is not None
        }
