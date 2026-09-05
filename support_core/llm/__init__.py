"""Provider protocol, AnthropicProvider, prompt assembly, structured output schemas. Implements
DESIGN.md section 11.

The three pieces DESIGN.md section 11 names, in the order they matter:

* :mod:`support_core.llm.prompt` - the nine layers of section 11.2 in a fixed order that a pack
  can fill but cannot reorder, remove or escape, with untrusted content fenced into data blocks
  (principle 7) and per-layer token budgets (section 10).
* :mod:`support_core.llm.schemas` - section 11.3's ``LlmNodeOutput``, narrowed per node so
  ``decision`` is a ``Literal`` over exactly the edges the graph declares (principle 2).
* :mod:`support_core.llm.provider` and :mod:`support_core.llm.anthropic_provider` - section
  11.1's protocol and its first implementation, with the recorded-response fakes in
  :mod:`support_core.llm.fake` for tests.

:class:`~support_core.llm.service.LlmService` is what the engine holds; nothing in the engine
imports a provider directly.
"""

from support_core.llm.prompt import (
    CORE_SYSTEM_PROMPT,
    Decision,
    Layer,
    Passage,
    PromptBudget,
    PromptInputs,
    PromptTooLargeError,
    ToolResult,
    TranscriptMessage,
    assemble,
    data_block,
    neutralise,
)
from support_core.llm.provider import LLMProvider, StructuredByCompletion
from support_core.llm.schemas import (
    ConversationSummary,
    LlmNodeOutput,
    SlotExtraction,
    build_node_output_model,
    build_slot_model,
    json_schema_for,
)
from support_core.llm.service import (
    LlmService,
    ModelChoice,
    NodeDecision,
    NodeRequest,
    SlotRequest,
    SummaryRequest,
)
from support_core.llm.tool_loop import (
    ModelToolRunner,
    ModelToolSpec,
    ReadOnlyToolGateway,
    ToolOutcome,
    ToolsUnavailableError,
    UnavailableToolRunner,
)
from support_core.llm.types import (
    CompletionRequest,
    CompletionResponse,
    LLMError,
    LLMUnavailableError,
    StructuredOutputError,
    Usage,
)

__all__ = [
    "CORE_SYSTEM_PROMPT",
    "CompletionRequest",
    "CompletionResponse",
    "ConversationSummary",
    "Decision",
    "LLMError",
    "LLMProvider",
    "LLMUnavailableError",
    "Layer",
    "LlmNodeOutput",
    "LlmService",
    "ModelChoice",
    "ModelToolRunner",
    "ModelToolSpec",
    "NodeDecision",
    "NodeRequest",
    "Passage",
    "PromptBudget",
    "PromptInputs",
    "PromptTooLargeError",
    "ReadOnlyToolGateway",
    "SlotExtraction",
    "SlotRequest",
    "StructuredByCompletion",
    "StructuredOutputError",
    "SummaryRequest",
    "ToolOutcome",
    "ToolResult",
    "ToolsUnavailableError",
    "TranscriptMessage",
    "UnavailableToolRunner",
    "Usage",
    "assemble",
    "build_node_output_model",
    "build_slot_model",
    "data_block",
    "json_schema_for",
    "neutralise",
]
