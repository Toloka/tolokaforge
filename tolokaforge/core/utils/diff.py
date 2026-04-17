"""State diff calculation utilities for grading

This module provides utilities for calculating diffs between expected and actual states,
with proper JSON serialization (sorted keys) for consistent comparison.
"""

import difflib
import json
from typing import Any


def serialize_state(state: dict[str, Any], sort_keys: bool = True) -> str:
    """Serialize state to formatted JSON string with sorted keys

    Args:
        state: State dictionary to serialize
        sort_keys: If True, sort keys for consistent comparison (default: True)

    Returns:
        Formatted JSON string
    """
    return json.dumps(state, indent=2, sort_keys=sort_keys, default=str)


def calculate_state_diff(
    expected_state: dict[str, Any], actual_state: dict[str, Any]
) -> dict[str, Any]:
    """Calculate diff between expected and actual states

    This function serializes both states with sorted keys, then uses difflib
    to generate a unified diff that clearly shows what changed.

    Args:
        expected_state: Expected state (typically from golden actions)
        actual_state: Actual final state from trial

    Returns:
        Dictionary containing:
            - golden_state: Expected state
            - actual_state: Actual state
            - diff: Unified diff string
            - diff_lines: Number of diff lines
            - has_diff: True if states differ
    """
    # Serialize both states with sorted keys for consistent comparison
    expected_str = serialize_state(expected_state, sort_keys=True)
    actual_str = serialize_state(actual_state, sort_keys=True)

    # Calculate unified diff
    expected_lines = expected_str.splitlines(keepends=True)
    actual_lines = actual_str.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            expected_lines,
            actual_lines,
            fromfile="expected_state",
            tofile="actual_state",
            lineterm="",
        )
    )

    # Join diff lines into string
    diff_str = "".join(diff_lines)

    return {
        "diff": diff_str,
        "diff_lines": len(diff_lines),
        "has_diff": len(diff_lines) > 0,
    }


def format_diff_summary(diff_result: dict[str, Any], max_lines: int = 50) -> str:
    """Format diff result as human-readable summary with truncation

    Args:
        diff_result: Result from calculate_state_diff
        max_lines: Maximum number of diff lines to include (default: 50)

    Returns:
        Formatted string suitable for error messages
    """
    if not diff_result["has_diff"]:
        return "States are identical"

    diff_text = diff_result["diff"]
    diff_lines = diff_text.split("\n")

    if len(diff_lines) <= max_lines:
        return diff_text
    else:
        # Truncate with summary
        truncated = "\n".join(diff_lines[:max_lines])
        remaining = len(diff_lines) - max_lines
        return f"{truncated}\n... ({remaining} more lines omitted)"


def get_diff_stats(diff_result: dict[str, Any]) -> dict[str, int]:
    """Get statistics about the diff

    Args:
        diff_result: Result from calculate_state_diff

    Returns:
        Dictionary with diff statistics:
            - total_lines: Total diff lines
            - additions: Lines added (starting with +)
            - deletions: Lines removed (starting with -)
            - context: Context lines (no prefix or starting with space)
    """
    if not diff_result["has_diff"]:
        return {"total_lines": 0, "additions": 0, "deletions": 0, "context": 0}

    diff_lines = diff_result["diff"].split("\n")

    additions = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    context = len(diff_lines) - additions - deletions

    return {
        "total_lines": len(diff_lines),
        "additions": additions,
        "deletions": deletions,
        "context": context,
    }
