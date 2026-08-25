"""Custom check for the ``hash_and_all_four`` composite pack.

Reads ``ctx.final_state.data`` — the runner leg's :class:`GradingSubstrate`
final-state read; the grader leg refuses this pack up front on the
``hash_enabled`` branch and never reaches ``custom_check_executors``.
"""

from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckFailed,
    CheckPassed,
    check,
    init,
)

orders: list[dict] = []


@init(interface_version="1.0")
def setup(ctx: CheckContext) -> None:
    global orders
    orders = ctx.final_state.data.get("orders", [])


@check
def order_was_shipped():
    """Order O1 reached the shipped state."""
    if any(o.get("id") == "O1" and o.get("status") == "shipped" for o in orders):
        return CheckPassed("order O1 is shipped")
    return CheckFailed("order O1 is not shipped")
