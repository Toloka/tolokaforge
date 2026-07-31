"""A tau-style tool the runner replays as a golden action.

Shipped into the runner container as a ``TaskDescription.tool_artifacts`` entry by
``tests/integration/test_docker_grading_hash_composition.py``, which is the only
consumer. Reconstructed as ``InvocationStyle.TAU_SYNC``, so its mutation reaches
the trial's own db-service namespace through the runner's trial-scoped proxy.
"""

from __future__ import annotations

from typing import Any


class SetOrderStatus:
    """Set one order's ``status`` in a tau-style state dict."""

    @staticmethod
    def invoke(data: dict[str, Any], order_id: str, status: str) -> str:
        rows = data["orders"]
        if not any(row["id"] == order_id for row in rows):
            raise ValueError(f"no order {order_id!r} among {[row['id'] for row in rows]}")
        # A new list of new records rather than an in-place edit: the runner's tau
        # wrapper detects mutations by diffing the state dict it read against the
        # one the tool leaves behind, and the two share their nested objects.
        data["orders"] = [
            {**row, "status": status} if row["id"] == order_id else row for row in rows
        ]
        return f"order {order_id} status set to {status}"
