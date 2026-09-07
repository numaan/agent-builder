# Acme refund policy

This is the policy the billing agent answers refund questions from. It is a knowledge source,
not a prompt: it is chunked, indexed and retrieved, and a change to it takes effect on the next
`support pack knowledge sync` without a deploy (DESIGN.md sections 9.2, 9.3 and principle 6).

## What can be refunded

A charge can be refunded when all of the following are true.

- It was made in the last 60 days. Charges older than 60 days are outside the refund window and
  are not eligible.
- It has not already been refunded. A charge is refunded once; a second request for the same
  charge is declined as already refunded.
- It is not a charge for a plan that was used in the period it paid for. A subscription month
  that the account used is not refundable, because the service was delivered.

An account in good standing does not need a reason for a refund inside the 60-day window. Acme
does not ask what the money is for.

## Duplicate charges

A duplicate charge is always refundable, whatever the age of the charge and whether or not the
plan was used. A duplicate is two charges for the same plan, for the same amount, in the same
billing period. Acme refunds the later of the two.

## What cannot be refunded

- One-off setup fees, once the setup work has been carried out.
- Charges disputed with the customer's bank. While a chargeback is open the money is with the
  bank, and Acme cannot refund it a second time; the customer's bank decides that case.
- Charges on an account that has been closed for more than 90 days, because the payment method
  on file has been deleted.

## Disagreeing with a decision

A customer who disagrees with a refund decision can ask for it to be looked at by a person. The
decision is reviewed by a billing specialist, who can override the policy. The agent never
promises the outcome of that review; it passes the conversation on.

## Partial refunds

A partial refund is possible where only part of a charge is disputed - one seat on a multi-seat
invoice, for example. A partial refund is worked out by a specialist rather than by the agent,
because it needs the invoice line items.
