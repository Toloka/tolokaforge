"""Custom check for the substrate-parity all-keys fixture.

Present so ``custom_checks.file`` names a real module — the parity suite only
translates this pack's grading config and never executes the check.
"""

from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckFailed,
    CheckPassed,
    check,
    init,
)

widgets: list[dict] = []


@init(interface_version="1.0")
def setup(ctx: CheckContext):
    global widgets
    widgets = ctx.final_state.data.get("widgets", [])


@check
def widget_was_closed():
    """Widget W1 reached the closed state."""
    closed = any(w.get("widget_id") == "W1" and w.get("status") == "closed" for w in widgets)
    if closed:
        return CheckPassed("widget W1 is closed")
    return CheckFailed("widget W1 is not closed")
