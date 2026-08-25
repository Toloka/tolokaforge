"""Deterministic custom operator for the composite-dispatch canonical test.

``is_positive_number`` — returns True iff value is a numeric > 0. Not present
in the shipped operator set. Proves the seam's registry-lookup path resolves
custom names alongside the 17 defaults.

The file name avoids the ``test_`` prefix so pyproject's ``python_files``
collection does not pick this module up as a test — it is a fixture the
canonical dispatch test wires up through a monkeypatched entry point.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["is_positive_number"]


def is_positive_number(value: Any, expected: Any, bindings: Mapping[str, Any]) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value > 0
