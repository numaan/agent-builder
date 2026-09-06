"""Acme Billing's identity tools: the one-time passcode of DESIGN.md sections 19 and 8.2.

``send_otp`` is the design's own example of a ``confirm_exempt`` WRITE tool:

    A pack may mark a WRITE tool ``confirm_exempt: true`` for side effects the customer cannot
    reasonably be asked about, such as sending a one-time passcode. - DESIGN.md section 8.2

``verify_otp`` is the other half, and it is the tool that makes DESIGN.md section 19 step 9 work:
"``verify_otp`` (``tool``) sets ``ctx.customer.identity_verified = true``". It does that through
:meth:`~support_core.tools.base.ToolContext.patch_customer`, so the *node* still never writes
``ctx`` (section 6.1 keeps it read-only to nodes) and the engine commits the change in the same
transaction as the step. It is WRITE rather than READ because changing whether the system
believes who it is talking to is a side effect, and a large one.

Both are exempt from confirmation for the same reason and both say so, because a
``confirm_exempt`` flag without a written reason is not the deliberate review DESIGN.md 8.2 asks
for. Neither is exempt from *anything else*: they are recorded as ``tool_call`` rows, keyed by
the step id, and refused from a model loop like every other WRITE tool.

The codes are held in memory and derived from the conversation id, so a demo and a test see the
same code without a mail server in between. A real pack sends mail and stores nothing.
"""

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from support_core.tools import FunctionTool, Risk, Tool, ToolContext, ToolFailed

OTP_EXEMPT_REASON = (
    "sending and checking a one-time passcode is the identity check itself; asking the customer "
    "to confirm that we may check their identity would be a confirmation they cannot "
    "meaningfully refuse and would train them to say yes (DESIGN.md section 8.2)"
)


@dataclass(slots=True)
class OtpStore:
    """One-time passcodes, in memory, one per conversation."""

    sent: dict[uuid.UUID, str] = field(default_factory=dict)
    deliveries: list[tuple[uuid.UUID, str, str]] = field(default_factory=list)
    """``(conversation, address, code)`` for every send, so a test can watch the side effect."""

    def code_for(self, conversation_id: uuid.UUID) -> str:
        """The code this conversation's passcode will be. Deterministic on purpose."""
        digest = hashlib.sha256(str(conversation_id).encode("utf-8")).hexdigest()
        return f"{int(digest[:8], 16) % 1_000_000:06d}"

    def send(self, conversation_id: uuid.UUID, address: str) -> str:
        code = self.code_for(conversation_id)
        self.sent[conversation_id] = code
        self.deliveries.append((conversation_id, address, code))
        return code

    def check(self, conversation_id: uuid.UUID, code: str | None) -> bool:
        expected = self.sent.get(conversation_id)
        if expected is None or not code:
            return False
        return code.strip().replace(" ", "") == expected

    def reset(self) -> None:
        self.sent.clear()
        self.deliveries.clear()


OTP = OtpStore()


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SendOtpInput(_Model):
    email: str | None = None


class SendOtpOutput(_Model):
    sent: bool
    address: str


class VerifyOtpInput(_Model):
    code: str | None = None


class VerifyOtpOutput(_Model):
    verified: bool


async def _send_otp(payload: Any, ctx: ToolContext) -> SendOtpOutput:
    address = (payload.email or ctx.customer.email or "").strip()
    if "@" not in address:
        msg = "no email address to send a passcode to"
        raise ToolFailed(msg)
    OTP.send(ctx.conversation_id, address)
    return SendOtpOutput(sent=True, address=address)


async def _verify_otp(payload: Any, ctx: ToolContext) -> VerifyOtpOutput:
    verified = OTP.check(ctx.conversation_id, payload.code)
    if verified:
        # The node does not write ``ctx``; this asks the engine to, and the engine commits it
        # with the checkpoint (DESIGN.md sections 6.1, 19 step 9).
        ctx.patch_customer(identity_verified=True)
    return VerifyOtpOutput(verified=verified)


SEND_OTP: Tool = FunctionTool(
    name="send_otp",
    description="Send a one-time passcode to the email address on the account.",
    input_model=SendOtpInput,
    output_model=SendOtpOutput,
    risk=Risk.WRITE,
    confirm_exempt=True,
    confirm_exempt_reason=OTP_EXEMPT_REASON,
    handler=_send_otp,
)

VERIFY_OTP: Tool = FunctionTool(
    name="verify_otp",
    description="Check a one-time passcode the customer typed, and mark the identity verified.",
    input_model=VerifyOtpInput,
    output_model=VerifyOtpOutput,
    risk=Risk.WRITE,
    confirm_exempt=True,
    confirm_exempt_reason=OTP_EXEMPT_REASON,
    handler=_verify_otp,
)
