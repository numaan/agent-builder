"""The approval hash. Implements DESIGN.md section 8.2's binding, once, for both ends of it.

    Approval binding: ``confirm`` computes ``sha256(tool_name + canonical_json(args))`` and
    stores an ``ActionApproval``. The tool node presents the same hash. Mismatch means refuse
    and route to ``on_error``. This closes the gap where a model confirms one amount and then
    calls with another.

Two decisions the design leaves open, both recorded in reviews/phase-4.md:

* **Which arguments.** The values are coerced through the tool's own ``input_model`` before they
  are hashed. ``29`` and ``29.0`` for a ``float`` input then hash alike - they are the same call
  - while any difference the model does not erase is still a mismatch. It also means the hash
  covers exactly what the tool will receive, because ``model_validate`` drops anything the input
  model does not declare, so an argument the tool never sees cannot change the hash either.
* **What canonical means.** ``json.dumps`` with sorted keys, no whitespace, ASCII escapes and
  ``allow_nan=False``. NaN and infinity are refused rather than hashed: they are not JSON, and
  two calls that differ only in which NaN they carry must not be told apart by a hash that
  cannot represent them.

The *evaluation* of the argument expressions happens before this module sees them, in
:func:`~support_core.engine.runners.value_of`, which goes through
:func:`~support_core.graph.schema.parse_value` - the same literal-versus-expression decision the
validator canonicalises with (phase-1 review finding F7, run-time half). A ``confirm`` node and
the ``tool`` node it authorises therefore classify ``{amount: 100}`` and ``{amount: "100"}`` the
same way the static check did.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from support_core.tools.base import Tool, ToolRefused


def canonical_args(tool: Tool, args: Mapping[str, Any]) -> dict[str, Any]:
    """The argument values as the tool will receive them, in JSON form.

    Raises :class:`~support_core.tools.base.ToolRefused` if they do not fit the input model,
    which is the right answer for both callers: a ``confirm`` node cannot propose an action it
    could not perform, and a ``tool`` node cannot perform one it cannot describe.
    """
    model = build_input(tool, args)
    dumped = model.model_dump(mode="json")
    return dict(dumped)


def build_input(tool: Tool, args: Mapping[str, Any]) -> BaseModel:
    try:
        return tool.input_model.model_validate(dict(args))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        msg = f"arguments do not fit the input of tool {tool.name!r}: {problems}"
        raise ToolRefused(msg) from exc


def canonical_json(args: Mapping[str, Any]) -> str:
    """A stable text for an argument mapping. Key order and whitespace never change it."""
    try:
        return json.dumps(
            dict(args), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except ValueError as exc:  # NaN, Infinity, or a value json cannot represent
        msg = f"arguments cannot be canonicalised for hashing: {exc}"
        raise ToolRefused(msg) from exc


def approval_hash(tool_name: str, args: Mapping[str, Any]) -> str:
    """``sha256(tool_name + canonical_json(args))`` (DESIGN.md section 8.2), exactly."""
    payload = tool_name + canonical_json(args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_for(tool: Tool, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The hash and the canonical arguments it was taken over, for one call."""
    canonical = canonical_args(tool, args)
    return approval_hash(tool.name, canonical), canonical
