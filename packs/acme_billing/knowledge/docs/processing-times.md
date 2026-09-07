# How long an Acme refund takes

The timing the billing agent quotes comes from here and from nowhere else. A node that tells a
customer when their money arrives cites a passage of this document, and the outbound citation
guardrail (DESIGN.md sections 9.2 and 14) refuses the sentence if it does not.

## Refunds to a card

A refund to the original payment card takes five to seven business days to appear on the
customer's statement. Acme releases the money on the day the refund is approved; the rest of that
time is the card network and the customer's own bank, and Acme cannot make it faster.

Weekends and public holidays do not count as business days, so a refund approved on a Friday
usually lands in the middle of the following week.

## Refunds to a bank account

A refund to a bank account by direct transfer takes three to five business days. It is used when
the original card has expired or been cancelled, and it needs the account details confirmed by a
person first.

## Refunds to account credit

Account credit is applied immediately. It is available on the next invoice and it cannot be paid
out to a card afterwards, so a customer who wants the money back rather than credit should say so
before the refund is issued.

## When nothing has arrived

If a card refund has not appeared after seven business days, the next step is for a billing
specialist to trace it with the payment provider. The agent does not re-issue the refund, because
issuing a second refund for one charge pays the customer twice.
