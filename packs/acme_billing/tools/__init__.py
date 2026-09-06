"""Tools exported by the Acme Billing pack (DESIGN.md sections 5 and 8.3).

``TOOLS`` is the whole contract between a pack and the tool runtime: the registry is built from
this list, and from phase 4 it is the *only* source of a risk tier at run time. A tier written
anywhere else - ``tools/tools.yaml``, a comment, a prompt - governs nothing (phase-1 deferred
finding I); the validator compares the two and refuses a pack whose declaration disagrees with
what it exports.
"""

from support_core.tools import Tool

from .billing import (
    CHECK_REFUND_ELIGIBILITY,
    GET_CHARGE,
    ISSUE_REFUND,
    LIST_RECENT_CHARGES,
)
from .identity import SEND_OTP, VERIFY_OTP

TOOLS: list[Tool] = [
    LIST_RECENT_CHARGES,
    GET_CHARGE,
    CHECK_REFUND_ELIGIBILITY,
    ISSUE_REFUND,
    SEND_OTP,
    VERIFY_OTP,
]
