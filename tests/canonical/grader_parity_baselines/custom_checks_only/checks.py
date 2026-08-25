"""Custom check for the ``custom_checks_only`` isolation pack.

Reads ``ctx.final_state.data`` — both legs derive this from the same
:class:`GradingSubstrate` final-state read, so a scoring divergence between
them names the ``custom_check_executors`` seam alone.
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
