"""The provider abstraction. Implements DESIGN.md section 11.1 verbatim.

    class LLMProvider(Protocol):
        async def complete(self, req: CompletionRequest) -> CompletionResponse: ...
        async def structured(
            self, req: CompletionRequest, schema: type[BaseModel]
        ) -> BaseModel: ...

Two implementations ship in phase 3: :class:`~support_core.llm.anthropic_provider.AnthropicProvider`
and the recorded-response fakes in :mod:`support_core.llm.fake`. The interface is deliberately
this small - section 11.1: "The interface is small enough that another provider is a day of
work."

``structured`` returns the model *instance*, so the schema is enforced on the way back whatever
the provider did on the way out. A provider that ignores the schema, or one whose SDK stops
honouring it, fails here rather than downstream.
"""

from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from support_core.llm.schemas import json_schema_for
from support_core.llm.types import (
    CompletionRequest,
    CompletionResponse,
    StructuredOutputError,
    StructuredSpec,
)

T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    """DESIGN.md section 11.1."""

    @property
    def name(self) -> str:
        """Short identifier for traces and error messages (``anthropic``, ``fake``)."""
        ...

    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...

    async def structured(self, req: CompletionRequest, schema: type[T]) -> T: ...


class StructuredByCompletion:
    """``structured`` implemented once, over ``complete``, for every provider that ships here.

    Two reasons it is a mixin rather than four implementations. The validation of the answer
    against the schema must happen for *every* provider, including the fakes, or a recorded
    fixture could contain something the real contract would reject. And it leaves ``complete``
    as the single point every call passes through, which is what lets
    :class:`~support_core.llm.recording.RecordingProvider` wrap any provider by wrapping one
    method.
    """

    async def complete(  # pragma: no cover - overridden by every concrete provider
        self, req: CompletionRequest
    ) -> CompletionResponse:
        raise NotImplementedError

    async def structured(self, req: CompletionRequest, schema: type[T]) -> T:
        request = req
        if request.structured is None:
            request = req.model_copy(
                update={
                    "structured": StructuredSpec(
                        name="respond",
                        description=schema.__doc__ or "Answer in this exact shape.",
                        json_schema=json_schema_for(schema),
                    )
                }
            )
        response = await self.complete(request)
        if response.structured is None:
            msg = (
                f"{request.purpose}: the model answered without the structured payload it was "
                f"asked for (stop_reason={response.stop_reason!r})"
            )
            raise StructuredOutputError(msg)
        try:
            return schema.model_validate(response.structured)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors()
            )
            msg = f"{request.purpose}: the model's answer does not fit the schema: {problems}"
            raise StructuredOutputError(msg) from exc
