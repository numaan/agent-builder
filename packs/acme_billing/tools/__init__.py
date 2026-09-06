"""Tools exported by the Acme Billing pack (DESIGN.md sections 5 and 8.3).

``TOOLS`` is the whole contract between a pack and the tool runtime: the registry is built from
this list, and from phase 4 it is the *only* source of a risk tier at run time. A tier written
anywhere else - ``tools/tools.yaml``, a comment, a prompt - governs nothing (phase-1 deferred
finding I); the validator compares the two and refuses a pack whose declaration disagrees with
what it exports.
"""

from support_core.tools import Tool

from .address import ADDRESSES as ADDRESSES
from .address import GET_ADDRESS, SET_ADDRESS
from .billing import (
    BILLING,
    CHECK_REFUND_ELIGIBILITY,
    GET_CHARGE,
    ISSUE_REFUND,
    LIST_RECENT_CHARGES,
)
from .identity import OTP, SEND_OTP, VERIFY_OTP

TOOLS: list[Tool] = [
    LIST_RECENT_CHARGES,
    GET_ADDRESS,
    SET_ADDRESS,
    GET_CHARGE,
    CHECK_REFUND_ELIGIBILITY,
    ISSUE_REFUND,
    SEND_OTP,
    VERIFY_OTP,
]


def reset_backend() -> None:
    """Put the in-memory fakes back to their seeded state.

    Only a fake pack has one of these: a real pack's backend is a service, and "reset the
    billing system" is not an operation. It exists so a demo or a test can start from the same
    account every time.

    **Call it through the module the tool registry imported**, not through ``packs.acme_billing``:
    :func:`support_core.tools.loading.import_pack_tools` loads a pack under a name derived from
    its path, so importing the same file both ways gives two module objects with two independent
    ``BILLING`` singletons, and resetting one leaves the other holding yesterday's refunds.
    """
    BILLING.reset()
    OTP.reset()
