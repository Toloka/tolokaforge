"""A tau-style tool the runner replays as a golden action.

Shipped into the runner container as a ``TaskDescription.tool_artifacts`` entry by
``tests/integration/test_docker_grading_hash_composition.py``, which is the only
consumer. Reconstructed as ``InvocationStyle.TAU_SYNC``, so its mutation reaches
the trial's own db-service namespace through the runner's trial-scoped proxy.

``amount`` is written as the string the caller passed, never coerced to a number:
what the folding cells compare is two representations of one amount, and a tool
that parsed them would erase the difference before the hash ever saw it.
"""

from __future__ import annotations

from typing import Any


class SetOrderAmount:
    """Set one order's ``amount`` in a tau-style state dict."""

    @staticmethod
    def invoke(data: dict[str, Any], order_id: str, amount: str) -> str:
        rows = data["orders"]
        if not any(row["id"] == order_id for row in rows):
            raise ValueError(f"no order {order_id!r} among {[row['id'] for row in rows]}")
        # A new list of new records rather than an in-place edit: the runner's tau
        # wrapper detects mutations by diffing the state dict it read against the
        # one the tool leaves behind, and the two share their nested objects.
        data["orders"] = [
            {**row, "amount": amount} if row["id"] == order_id else row for row in rows
        ]
        return f"order {order_id} amount set to {amount}"
