"""Conversation summaries and the customer memory tool. Implements DESIGN.md section 10.

Phase 3 delivers the "Conversation summary" row: a rolling summary rewritten every K turns and
stored in ``conversation.summary`` (:mod:`support_core.memory.summary`). Customer memory - the
durable per-customer notes, written through a WRITE-tier internal tool - is phase 9.

The rule the whole layer is built around: memory is prompt context, never a source of authority.
Nothing a turn depends on is read from it, and nothing in it can authorise an action.
"""

from support_core.memory.summary import LlmSummarizer

__all__ = ["LlmSummarizer"]
