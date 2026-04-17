"""
Custom checks for the calc_custom_checks test task.

This file demonstrates how to use the custom checks system.
"""

from tolokaforge.core.grading.checks_helpers import (
    count_tool_calls,
)
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckFailed,
    CheckPassed,
    CheckSkipped,
    check,
    init,
)

# Global state populated by init
counter_value = 0
initial_counter = 0
operations = []
tool_calls = []


@init(interface_version="1.0")
def setup(ctx: CheckContext):
    """Initialize state for all checks."""
    global counter_value, initial_counter, operations, tool_calls

    counter_value = ctx.effects.get("counter", 0)
    initial_counter = ctx.initial_state.get("counter", 0)
    operations = ctx.effects.get("operations", [])
    tool_calls = ctx.tool_calls


@check
def counter_was_incremented():
    """Verify the counter was incremented from initial value."""
    if counter_value > initial_counter:
        return CheckPassed(
            f"Counter increased from {initial_counter} to {counter_value}",
            details={"initial": initial_counter, "final": counter_value},
        )
    return CheckFailed(
        f"Counter was not incremented (still {counter_value})",
        details={"initial": initial_counter, "final": counter_value},
    )


@check
def counter_reached_target():
    """Verify the counter reached at least 5."""
    if counter_value >= 5:
        return CheckPassed(f"Counter is {counter_value} (>= 5)")
    return CheckFailed(
        f"Counter is only {counter_value}, should be >= 5",
        score=counter_value / 5,  # Partial credit
    )


@check
def update_counter_tool_was_called():
    """Verify the update_counter tool was called at least once."""
    call_count = count_tool_calls(tool_calls, "update_counter")

    if call_count == 0:
        return CheckSkipped("No tool calls to check - test data may not include transcript")

    if call_count > 0:
        return CheckPassed(f"update_counter was called {call_count} time(s)")
    return CheckFailed("update_counter was never called")


@check
def operations_were_logged():
    """Check if operations were logged (optional check with partial credit)."""
    if operations:
        return CheckPassed(
            f"Found {len(operations)} logged operations",
            details={"operations": operations[:5]},  # First 5 only
        )
    # Not a failure, just means logging wasn't implemented
    return CheckSkipped("No operations logged (logging not required)")
