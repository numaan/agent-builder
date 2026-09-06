"""Acme Billing's charge and refund tools, over an in-memory fake billing system.

The fake is the point of it being a *sample* pack: there is no billing provider to call, so the
tools call something that behaves like one - it has charges, it refunds them, it refuses to
refund the same charge twice, and it remembers what it was asked to do so a test can ask whether
the money moved. Swapping :data:`BILLING` for an HTTP client is the only change a real pack
would make; the risk tiers, the approval binding and the idempotency key are not the pack's
business and do not appear here.

Risk tiers (DESIGN.md section 8.1), and why each one:

* ``list_recent_charges``, ``get_charge``, ``check_refund_eligibility`` - READ. No side effect,
  so an ``llm`` node's bounded loop may call them (section 8.4) and no confirmation is needed.
* ``issue_refund`` - HIGH and **not idempotent**. Section 8.1 defaults ``idempotent`` to true
  and the design's example is silent; at-most-once is the honest setting for money. A crash
  between the call and its recorded outcome then reaches a human instead of a retry, which is
  the trade this pack chooses deliberately: one refund that needs a person to confirm it landed
  beats two refunds that nobody asked for.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from support_core.tools import FunctionTool, Risk, Tool, ToolContext, ToolFailed

REFUND_WINDOW_DAYS = 60
"""Acme's refund policy horizon. Stated in policies.md too, because the model explains it."""


@dataclass(slots=True)
class Charge:
    """One charge on a customer's account."""

    id: str
    customer_ref: str
    amount: float
    currency: str
    description: str
    charged_on: date
    refunded_by: str | None = None


@dataclass(slots=True)
class Refund:
    id: str
    charge_id: str
    amount: float
    status: str = "pending"


@dataclass(slots=True)
class FakeBilling:
    """An in-memory stand-in for Acme's billing system.

    ``executed`` is the honest record of what actually happened, in order, including a refund
    the system itself rejected. Idempotency tests read it: "how many times did money move" is
    not answerable from the tool's return value, because a replayed result looks the same as a
    fresh one.
    """

    charges: dict[str, Charge] = field(default_factory=dict)
    refunds: dict[str, Refund] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)
    today: date = date(2026, 9, 6)

    def reset(self, charges: list[Charge] | None = None) -> None:
        wanted = charges if charges is not None else seed()
        self.charges = {charge.id: charge for charge in wanted}
        self.refunds = {}
        self.executed = []

    def for_customer(self, customer_ref: str | None) -> list[Charge]:
        return sorted(
            (c for c in self.charges.values() if customer_ref and c.customer_ref == customer_ref),
            key=lambda c: (c.charged_on, c.id),
            reverse=True,
        )

    def eligibility(self, charge: Charge) -> tuple[bool, str | None]:
        if charge.refunded_by is not None:
            return False, "that charge has already been refunded"
        age = (self.today - charge.charged_on).days
        if age > REFUND_WINDOW_DAYS:
            return False, (
                f"it was charged {age} days ago, outside the {REFUND_WINDOW_DAYS}-day refund window"
            )
        return True, None

    def refund(self, charge_id: str, amount: float) -> Refund:
        self.executed.append(f"issue_refund:{charge_id}:{amount}")
        charge = self.charges.get(charge_id)
        if charge is None:
            msg = f"no charge {charge_id!r}"
            raise ToolFailed(msg)
        if charge.refunded_by is not None:
            msg = f"charge {charge_id!r} was already refunded by {charge.refunded_by}"
            raise ToolFailed(msg)
        if abs(amount - charge.amount) > 0.005:
            msg = f"partial refunds are not supported: {amount} against a charge of {charge.amount}"
            raise ToolFailed(msg)
        refund = Refund(
            id=f"re_{hashlib.sha256(charge_id.encode()).hexdigest()[:10]}",
            charge_id=charge_id,
            amount=amount,
            status="pending",
        )
        charge.refunded_by = refund.id
        self.refunds[refund.id] = refund
        return refund


def seed() -> list[Charge]:
    """The account the sample conversations happen on: a duplicated Pro Plan charge."""
    return [
        Charge("ch_1001", "cus_acme_1", 29.00, "USD", "Pro Plan - September", date(2026, 9, 1)),
        Charge("ch_1002", "cus_acme_1", 29.00, "USD", "Pro Plan - September", date(2026, 9, 3)),
        Charge("ch_0900", "cus_acme_1", 12.50, "USD", "Extra seats - June", date(2026, 6, 2)),
    ]


BILLING = FakeBilling()
BILLING.reset()


# -- input and output models -------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListChargesInput(_Model):
    limit: int | None = Field(default=5, ge=1, le=50)


class ChargeView(_Model):
    id: str
    amount: float
    currency: str
    description: str
    charged_on: str
    refunded: bool = False


class ListChargesOutput(_Model):
    charges: list[ChargeView] = Field(default_factory=list)


class ChargeIdInput(_Model):
    charge_id: str


class EligibilityOutput(_Model):
    eligible: bool
    reason: str | None = None


class RefundInput(_Model):
    charge_id: str
    amount: float


class RefundOutput(_Model):
    refund_id: str
    status: str


def _view(charge: Charge) -> ChargeView:
    return ChargeView(
        id=charge.id,
        amount=charge.amount,
        currency=charge.currency,
        description=charge.description,
        charged_on=charge.charged_on.isoformat(),
        refunded=charge.refunded_by is not None,
    )


# -- the tools ---------------------------------------------------------------------------


async def _list_recent_charges(payload: Any, ctx: ToolContext) -> ListChargesOutput:
    charges = BILLING.for_customer(ctx.customer.ref)[: payload.limit or 5]
    return ListChargesOutput(charges=[_view(charge) for charge in charges])


async def _get_charge(payload: Any, ctx: ToolContext) -> ChargeView:
    charge = BILLING.charges.get(payload.charge_id)
    if charge is None or charge.customer_ref != ctx.customer.ref:
        # Not "no such charge": a charge belonging to somebody else must be indistinguishable
        # from one that does not exist, or the tool is an account enumeration oracle.
        msg = f"no charge {payload.charge_id!r} on this account"
        raise ToolFailed(msg)
    return _view(charge)


async def _check_refund_eligibility(payload: Any, ctx: ToolContext) -> EligibilityOutput:
    charge = BILLING.charges.get(payload.charge_id)
    if charge is None or charge.customer_ref != ctx.customer.ref:
        return EligibilityOutput(eligible=False, reason="that charge is not on this account")
    eligible, reason = BILLING.eligibility(charge)
    return EligibilityOutput(eligible=eligible, reason=reason)


async def _issue_refund(payload: Any, ctx: ToolContext) -> RefundOutput:
    if not ctx.customer.identity_verified:
        # The graph's gate is the first line and this is the second. A tool that moves money on
        # an unverified identity is a tool that trusts the graph never to change.
        msg = "refusing to refund on an unverified identity"
        raise ToolFailed(msg)
    refund = BILLING.refund(payload.charge_id, payload.amount)
    return RefundOutput(refund_id=refund.id, status=refund.status)


LIST_RECENT_CHARGES: Tool = FunctionTool(
    name="list_recent_charges",
    description="List the customer's recent charges, most recent first.",
    input_model=ListChargesInput,
    output_model=ListChargesOutput,
    risk=Risk.READ,
    handler=_list_recent_charges,
)

GET_CHARGE: Tool = FunctionTool(
    name="get_charge",
    description="Fetch one charge on the customer's account by id.",
    input_model=ChargeIdInput,
    output_model=ChargeView,
    risk=Risk.READ,
    handler=_get_charge,
)

CHECK_REFUND_ELIGIBILITY: Tool = FunctionTool(
    name="check_refund_eligibility",
    description="Decide whether a charge may be refunded, and say why not if it may not.",
    input_model=ChargeIdInput,
    output_model=EligibilityOutput,
    risk=Risk.READ,
    handler=_check_refund_eligibility,
)

ISSUE_REFUND: Tool = FunctionTool(
    name="issue_refund",
    description="Refund a charge in full to the original payment method.",
    input_model=RefundInput,
    output_model=RefundOutput,
    risk=Risk.HIGH,
    idempotent=False,
    handler=_issue_refund,
)
