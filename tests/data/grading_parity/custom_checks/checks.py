"""Custom check for the ``custom_checks`` substrate-parity differential.

Reads only ``ctx.final_state``, which ``build_check_context`` derives identically
on both substrates, so a score difference between them is a real divergence in
the grading path rather than a fixture asymmetry.
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
def setup(ctx: CheckContext):
    global orders
    orders = ctx.final_state.data.get("orders", [])


@check
def order_was_shipped():
    """Order O1 reached the shipped state."""
    if any(o.get("id") == "O1" and o.get("status") == "shipped" for o in orders):
        return CheckPassed("order O1 is shipped")
    return CheckFailed("order O1 is not shipped")
