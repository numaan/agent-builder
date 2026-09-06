"""Acme Billing's address tools: the second workflow, added without touching the core.

This module exists to make one claim checkable. DESIGN.md section 1 says the core "knows nothing
about any particular business" and that moving to another domain must need no change to it. A new
capability should therefore be a graph file plus tools in a pack, and nothing else. Adding the
address change touched:

* ``graphs/update_address.yaml`` - the workflow, as data;
* this module and one line of ``tools/__init__.py`` - the two tools it calls;
* one edge and one sub-graph node in ``graphs/root.yaml`` - the route to it.

No file under ``support_core/`` changed. That is the whole argument, and it is worth a demo.

``get_address`` is READ. ``set_address`` is WRITE, and deliberately *not* ``confirm_exempt``: the
customer can reasonably be asked "shall I change it to this", and DESIGN.md section 8.2 requires a
confirm on every path to it. The validator enforces that at load time, and the runtime enforces it
again against the approval hash, so the address the customer approved is the address that is
written - a typo corrected between the confirmation and the call is a mismatch and is refused.

The store is in memory, seeded to the same customer the refund flow uses, so the demo starts from
a known address every time.
"""

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from support_core.tools import FunctionTool, Risk, Tool, ToolContext, ToolFailed


class AddressView(BaseModel):
    """One postal address, as the customer would read it back."""

    model_config = ConfigDict(extra="forbid")

    line1: str
    line2: str | None = None
    city: str
    postcode: str
    country: str

    def one_line(self) -> str:
        parts = [self.line1, self.line2, self.city, self.postcode, self.country]
        return ", ".join(part for part in parts if part)


class CustomerRefInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_ref: str = Field(description="The account to read the address of.")


class SetAddressInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_ref: str = Field(description="The account to change.")
    line1: str = Field(description="House name or number and street.")
    line2: str | None = Field(default=None, description="Flat, unit or similar. May be omitted.")
    city: str = Field(description="Town or city.")
    postcode: str = Field(description="Postal or ZIP code.")
    country: str = Field(description="Country name.")


class SetAddressOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: AddressView
    changed: bool


@dataclass
class AddressBook:
    """The pack's fake address store. A real pack calls a customer record service."""

    _by_ref: dict[str, AddressView] = field(default_factory=dict)

    def seed(self) -> None:
        self._by_ref = {
            "cus_acme_1": AddressView(
                line1="14 Otter Lane",
                line2=None,
                city="Bristol",
                postcode="BS1 4TR",
                country="United Kingdom",
            )
        }

    def get(self, ref: str) -> AddressView:
        try:
            return self._by_ref[ref]
        except KeyError:
            msg = f"no address on file for {ref}"
            raise ToolFailed(msg) from None

    def set(self, ref: str, address: AddressView) -> bool:
        before = self._by_ref.get(ref)
        self._by_ref[ref] = address
        return before is None or before.model_dump() != address.model_dump()


ADDRESSES = AddressBook()
ADDRESSES.seed()


async def _get_address(payload: CustomerRefInput, ctx: ToolContext) -> AddressView:
    return ADDRESSES.get(payload.customer_ref)


async def _set_address(payload: SetAddressInput, ctx: ToolContext) -> SetAddressOutput:
    if not ctx.customer.identity_verified:
        # The graph gates this workflow, and this is the second line. A tool that changes where
        # a customer's post goes on an unverified identity is a tool that trusts the graph never
        # to change - and a redirected address is how an account is taken over.
        msg = "refusing to change an address on an unverified identity"
        raise ToolFailed(msg)
    address = AddressView(
        line1=payload.line1,
        line2=payload.line2,
        city=payload.city,
        postcode=payload.postcode,
        country=payload.country,
    )
    changed = ADDRESSES.set(payload.customer_ref, address)
    return SetAddressOutput(address=address, changed=changed)


GET_ADDRESS: Tool = FunctionTool(
    name="get_address",
    description="Read the postal address currently on the customer's account.",
    input_model=CustomerRefInput,
    output_model=AddressView,
    risk=Risk.READ,
    handler=_get_address,
)

SET_ADDRESS: Tool = FunctionTool(
    name="set_address",
    description="Replace the postal address on the customer's account.",
    input_model=SetAddressInput,
    output_model=SetAddressOutput,
    risk=Risk.WRITE,
    handler=_set_address,
)


def reset_addresses() -> Any:
    """Put the fake address book back to its seeded state, for a demo or a test."""
    ADDRESSES.seed()
