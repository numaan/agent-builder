"""Risk tiers and the policy that follows from them. Implements DESIGN.md section 8.2.

Phase 1 needs the tiers (and only the tiers) so the validator can decide which ``tool`` nodes
must be covered by a ``confirm`` node. The runtime that *enforces* the policy - approval hashes,
idempotency keys, the model-facing tool loop - is phase 4. The table below is the design's,
transcribed once so the validator and the future runtime cannot disagree about it:

============  ======================  ================  ===============  ============
Risk          From ``llm`` node loop  From ``tool`` node  Needs confirm   Needs human
============  ======================  ================  ===============  ============
``read``      allowed if listed       allowed            no               no
``write``     never                   allowed            yes              no
``high``      never                   allowed            yes              if declared
============  ======================  ================  ===============  ============
"""

from enum import StrEnum


class Risk(StrEnum):
    """DESIGN.md section 8.1."""

    READ = "read"
    """No side effects."""

    WRITE = "write"
    """Reversible or low-impact side effect."""

    HIGH = "high"
    """Money, access, irreversible."""


REQUIRES_CONFIRM: frozenset[Risk] = frozenset({Risk.WRITE, Risk.HIGH})
"""Tiers that need a ``confirm`` node on every path from the last customer input (5.2, 8.2)."""

MODEL_CALLABLE: frozenset[Risk] = frozenset({Risk.READ})
"""Tiers an ``llm`` node's bounded tool loop may call at all (DESIGN.md sections 8.2, 8.4)."""

SIDE_EFFECTING: frozenset[Risk] = frozenset({Risk.WRITE, Risk.HIGH})
"""Tiers that may change anything outside the conversation - including ``ctx`` (section 19)."""


def needs_confirm(risk: Risk, *, confirm_exempt: bool = False) -> bool:
    """Whether a ``confirm`` node must authorise a call at this tier (DESIGN.md section 8.2).

    One function, called by the validator's :class:`~support_core.graph.tools_manifest.ToolSpec`
    and by the runtime's :class:`~support_core.tools.base.Tool`, so a tool cannot be exempt to
    one of them and not to the other.

    ``confirm_exempt`` is honoured for WRITE only. Section 8.2 offers it for "side effects the
    customer cannot reasonably be asked about, such as sending a one-time passcode"; HIGH is
    "money, access, irreversible", and no phrasing of the flag makes that askable-about.
    """
    if risk is Risk.HIGH:
        return True
    return risk is Risk.WRITE and not confirm_exempt
