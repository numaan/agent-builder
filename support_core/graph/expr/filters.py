"""The fixed filter set. Implements DESIGN.md section 6.4 ("a fixed set of filters").

BACKLOG.md phase 1 names them exactly: ``money``, ``lower``, ``len``, ``default``. The same four
are the only filters the Jinja templates in :mod:`support_core.graph.templates` may use, so a
pack author sees one vocabulary in predicates and in prose.

Each filter declares a runtime implementation *and* a static signature, expressed as coarse
value categories so that :mod:`support_core.graph.expr.typecheck` can check a filter chain
without importing the type checker back into this module.
"""

from collections.abc import Callable, Sized
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

Category = str
"""One of ``number``, ``str``, ``sized``, ``any``: the shape a filter accepts."""

Returns = str
"""``str``, ``int``, ``same`` (the input type) or ``unwrap_optional`` (the input minus None)."""


class FilterError(ValueError):
    """A filter was applied to a value it cannot handle at run time."""


def _money(value: Any) -> str:
    """Format a number as an amount with two decimal places and thousands separators.

    No currency symbol and no locale: DESIGN.md fixes neither, and section 21 leaves
    multi-language formatting open. Phase 1 renders ``1234.5`` as ``1,234.50``; whichever phase
    introduces per-pack currency configuration should extend this filter rather than let packs
    hand-roll formatting in templates.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise FilterError(f"money expects a number, got {type(value).__name__}")
    return f"{Decimal(str(value)):,.2f}"


def _lower(value: Any) -> str:
    if not isinstance(value, str):
        raise FilterError(f"lower expects a string, got {type(value).__name__}")
    return value.lower()


def _len(value: Any) -> int:
    if isinstance(value, Sized):
        return len(value)
    raise FilterError(f"len expects a string or a collection, got {type(value).__name__}")


def _default(value: Any, fallback: Any) -> Any:
    """Return ``fallback`` when ``value`` is ``None``.

    Unlike Jinja's ``default`` this does not have a "treat falsy as missing" mode: an empty
    string and a zero amount are real values a support workflow must be able to distinguish.
    """
    return fallback if value is None else value


@dataclass(frozen=True, slots=True)
class FilterSpec:
    name: str
    call: Callable[..., Any]
    min_args: int
    max_args: int
    accepts: tuple[Category, ...]
    returns: Returns
    accepts_none: bool
    """Whether the filter is allowed to receive an optional (possibly ``None``) value."""


FILTERS: dict[str, FilterSpec] = {
    "money": FilterSpec(
        name="money",
        call=_money,
        min_args=0,
        max_args=0,
        accepts=("number",),
        returns="str",
        accepts_none=False,
    ),
    "lower": FilterSpec(
        name="lower",
        call=_lower,
        min_args=0,
        max_args=0,
        accepts=("str",),
        returns="str",
        accepts_none=False,
    ),
    "len": FilterSpec(
        name="len",
        call=_len,
        min_args=0,
        max_args=0,
        accepts=("sized",),
        returns="int",
        accepts_none=False,
    ),
    "default": FilterSpec(
        name="default",
        call=_default,
        min_args=1,
        max_args=1,
        accepts=("any",),
        returns="unwrap_optional",
        accepts_none=True,
    ),
}
