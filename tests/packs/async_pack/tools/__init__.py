"""The async pack's tools come from ``tests.tool_support``, not from here.

The file exists because DESIGN.md section 5 requires it; ``unvalidated_pack`` supplies the
registry directly.
"""

from typing import Any

TOOLS: list[Any] = []
