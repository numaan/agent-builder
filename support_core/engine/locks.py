"""The per-conversation single-writer lock. Implements DESIGN.md section 17 "Concurrency".

"``pg_advisory_xact_lock(hash(conversation_id))`` around each turn. Inbound messages that
arrive while locked are stored in ``message`` with ``status = pending`` and processed in order
when the lock frees."

Two details the design leaves open, decided here:

* **Which connection holds it.** A turn is deliberately many transactions - one per node, so
  that a crash loses at most the node in flight (section 7.1) - so the lock cannot live on the
  connection doing the work. It lives on a connection of its own whose transaction is opened
  when the turn starts and rolled back when it ends. Nothing else runs on that connection.
  Using the *transaction* form rather than the session form matters: a process that dies drops
  the connection, Postgres aborts the transaction, and the lock is released without anyone
  running an unlock. There is no leaked-lock path.
* **What a caller that cannot get the lock does.** It waits, bounded by ``lock_timeout``,
  because its message is already stored as ``pending``: waiting makes it the next drainer and
  closes the window in which the current holder finishes draining just after the newcomer's row
  became visible. ``wait_seconds=0`` gives the non-blocking form for callers that must not
  block, at the cost of leaving the message for the next arrival to drain.
"""

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

LOCK_NOT_AVAILABLE = "55P03"
"""Postgres SQLSTATE ``lock_not_available``, which is what ``lock_timeout`` raises. Every other
SQLSTATE from the lock statement is a real failure and is re-raised (review finding R5)."""

_PERSON = b"supportconv"
"""Namespace for the lock key, so a conversation id cannot collide with some other advisory
lock taken on the same database by another component."""


def lock_key(conversation_id: uuid.UUID) -> int:
    """The 64-bit advisory lock key for a conversation.

    Hashed in Python rather than with Postgres ``hashtext``, whose value is not part of any
    documented contract and has changed between major versions.
    """
    digest = hashlib.blake2b(conversation_id.bytes, digest_size=8, person=_PERSON).digest()
    return int.from_bytes(digest, "big", signed=True)


@asynccontextmanager
async def conversation_lock(
    engine: AsyncEngine, conversation_id: uuid.UUID, *, wait_seconds: float = 30.0
) -> AsyncIterator[bool]:
    """Hold the conversation's advisory lock for the body of the ``with`` block.

    Yields whether the lock was acquired. ``wait_seconds=0`` tries once and does not wait.
    """
    key = lock_key(conversation_id)
    connection = await engine.connect()
    try:
        transaction = await connection.begin()
        try:
            if wait_seconds <= 0:
                result = await connection.execute(
                    text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
                )
                acquired = bool(result.scalar_one())
            else:
                milliseconds = max(1, int(wait_seconds * 1000))
                await connection.execute(text(f"SET LOCAL lock_timeout = '{milliseconds}ms'"))
                try:
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
                    )
                    acquired = True
                except DBAPIError as exc:
                    if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
                        # A dropped connection, a cancelled statement or a permissions error is
                        # not "somebody else is holding the lock". Reporting it as a busy lock
                        # would leave the message pending with the failure recorded nowhere
                        # (review finding R5).
                        raise
                    # lock_timeout fired. The message is already stored as pending, so the
                    # work is not lost; the next caller to get the lock drains it in order.
                    acquired = False
            yield acquired
        finally:
            await transaction.rollback()
    finally:
        await connection.close()
