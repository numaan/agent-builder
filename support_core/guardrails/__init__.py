"""Inbound, outbound and structural guardrails. Implements DESIGN.md section 14.

Phase 5 delivers the first item of section 14's "Outbound" list, the citation check of section
9.2: :mod:`support_core.guardrails.outbound`. Everything else in section 14 belongs to other
phases and is deliberately not stubbed here, so that a reader can tell what exists from what is
planned - inbound PII tagging, the prompt-injection flag and language routing, the forbidden
promise check and the leakage scan are phase 7's; the structural half (risk tiers, approval
hashes, gates, per-turn limits) is not in this package at all, because it is enforced by the tool
runtime and the engine rather than by a text check, which is the point section 14 makes about it.
"""

from support_core.guardrails.outbound import (
    CitationPolicy,
    CitationVerdict,
    Claim,
    check_citations,
    find_claims,
)

__all__ = [
    "CitationPolicy",
    "CitationVerdict",
    "Claim",
    "check_citations",
    "find_claims",
]
