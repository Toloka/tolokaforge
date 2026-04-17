"""
Generic helper functions for custom checks.

These are domain-agnostic utilities that can be used by any task's checks.py.
Domain-specific helpers should be defined at the project level (e.g.,
tasks/airline/check_helpers.py).

Usage in checks.py:
    from tolokaforge.core.grading.checks_helpers import (
        check_dict_params, last_tool_name, text_contains_any
    )
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

# =============================================================================
# Dictionary / Data Comparison Helpers
# =============================================================================


def check_dict_params(
    data: dict[str, Any],
    expected: dict[str, Any],
    prefix: str = "",
) -> list[str]:
    """
    Check that dictionary values match expected values.

    Returns a list of mismatch descriptions. Empty list means all match.

    Args:
        data: The dictionary to check
        expected: Dictionary of key->expected_value pairs
        prefix: Optional prefix for error messages (e.g., "reservation")

    Returns:
        List of mismatch messages, empty if all match

    Example:
        errors = check_dict_params(reservation, {
            "status": "cancelled",
            "cabin": "economy"
        }, prefix="reservation")
        if errors:
            return CheckFailed("; ".join(errors))
    """
    mismatches = []
    for key, expected_val in expected.items():
        actual_val = data.get(key)
        if actual_val != expected_val:
            path = f"{prefix}.{key}" if prefix else key
            mismatches.append(f"{path}: expected {expected_val!r}, got {actual_val!r}")
    return mismatches


def dict_diff(
    dict1: dict[str, Any],
    dict2: dict[str, Any],
    include_keys: list[str] | None = None,
    exclude_keys: list[str] | None = None,
) -> dict[str, tuple]:
    """
    Find differences between two dictionaries.

    Args:
        dict1: First dictionary (e.g., initial state)
        dict2: Second dictionary (e.g., final state)
        include_keys: If provided, only check these keys
        exclude_keys: Keys to skip (e.g., timestamps)

    Returns:
        Dict mapping key -> (value_in_dict1, value_in_dict2) for differing keys

    Example:
        changes = dict_diff(initial_user, final_user, exclude_keys=["updated_at"])
        if "email" in changes:
            old_email, new_email = changes["email"]
    """
    exclude_set = set(exclude_keys or [])
    all_keys = set(dict1.keys()) | set(dict2.keys())

    if include_keys is not None:
        all_keys = all_keys & set(include_keys)

    all_keys = all_keys - exclude_set

    diffs = {}
    for key in all_keys:
        val1 = dict1.get(key)
        val2 = dict2.get(key)
        if val1 != val2:
            diffs[key] = (val1, val2)

    return diffs


def get_nested(
    data: dict[str, Any] | list,
    path: str,
    default: Any = None,
    separator: str = ".",
) -> Any:
    """
    Get value from nested dict/list by dot-notation path.

    Args:
        data: Dictionary or list to search
        path: Dot-separated path (e.g., "users.0.name" or "orders.order_123.status")
        default: Value to return if path not found
        separator: Path separator (default ".")

    Returns:
        Value at path or default

    Example:
        status = get_nested(state, "orders.ABC123.status", default="unknown")
    """
    parts = path.split(separator)
    current = data

    for part in parts:
        if current is None:
            return default

        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)):
            try:
                idx = int(part)
                current = current[idx] if 0 <= idx < len(current) else None
            except (ValueError, IndexError):
                return default
        else:
            return default

    return current if current is not None else default


# =============================================================================
# Tool Call Helpers
# =============================================================================


def last_tool_name(tool_calls: Sequence[Any]) -> str | None:
    """
    Get the name of the last tool called.

    Args:
        tool_calls: List of tool calls (ToolCall objects or dicts)

    Returns:
        Name of last tool or None if no calls

    Example:
        if last_tool_name(ctx.tool_calls) != "submit_order":
            return CheckFailed("Expected submit_order to be called last")
    """
    if not tool_calls:
        return None

    last = tool_calls[-1]
    if hasattr(last, "name"):
        return last.name
    if isinstance(last, dict):
        return last.get("name")
    return None


def first_tool_name(tool_calls: Sequence[Any]) -> str | None:
    """
    Get the name of the first tool called.

    Args:
        tool_calls: List of tool calls (ToolCall objects or dicts)

    Returns:
        Name of first tool or None if no calls
    """
    if not tool_calls:
        return None

    first = tool_calls[0]
    if hasattr(first, "name"):
        return first.name
    if isinstance(first, dict):
        return first.get("name")
    return None


def count_tool_calls(
    tool_calls: Sequence[Any],
    tool_name: str | None = None,
) -> int:
    """
    Count tool calls, optionally filtering by name.

    Args:
        tool_calls: List of tool calls
        tool_name: If provided, only count calls to this tool

    Returns:
        Number of matching calls

    Example:
        cancel_count = count_tool_calls(ctx.tool_calls, "cancel_order")
    """
    if tool_name is None:
        return len(tool_calls)

    count = 0
    for tc in tool_calls:
        name = tc.name if hasattr(tc, "name") else tc.get("name")
        if name == tool_name:
            count += 1
    return count


def find_tool_calls(
    tool_calls: Sequence[Any],
    tool_name: str,
) -> list[Any]:
    """
    Find all calls to a specific tool.

    Args:
        tool_calls: List of tool calls
        tool_name: Tool name to filter by

    Returns:
        List of matching tool calls

    Example:
        updates = find_tool_calls(ctx.tool_calls, "update_user")
        for update in updates:
            # check arguments...
    """
    result = []
    for tc in tool_calls:
        name = tc.name if hasattr(tc, "name") else tc.get("name")
        if name == tool_name:
            result.append(tc)
    return result


def tool_was_called(
    tool_calls: Sequence[Any],
    tool_name: str,
) -> bool:
    """
    Check if a specific tool was called at least once.

    Args:
        tool_calls: List of tool calls
        tool_name: Tool name to check

    Returns:
        True if tool was called
    """
    return count_tool_calls(tool_calls, tool_name) > 0


def get_tool_argument(
    tool_call: Any,
    arg_name: str,
    default: Any = None,
) -> Any:
    """
    Get an argument from a tool call.

    Args:
        tool_call: Tool call object or dict
        arg_name: Name of the argument
        default: Default if argument not found

    Returns:
        Argument value or default
    """
    if hasattr(tool_call, "arguments"):
        args = tool_call.arguments
    elif isinstance(tool_call, dict):
        args = tool_call.get("arguments", {})
    else:
        return default

    if isinstance(args, dict):
        return args.get(arg_name, default)
    return default


# =============================================================================
# Text / String Helpers
# =============================================================================


def text_contains_any(
    text: str,
    phrases: list[str],
    case_sensitive: bool = False,
) -> bool:
    """
    Check if text contains any of the given phrases.

    Args:
        text: Text to search in
        phrases: List of phrases to look for
        case_sensitive: Whether matching is case-sensitive

    Returns:
        True if any phrase found

    Example:
        if text_contains_any(response, ["error", "failed", "unable"]):
            return CheckFailed("Response contains error message")
    """
    if not case_sensitive:
        text = text.lower()
        phrases = [p.lower() for p in phrases]
    return any(phrase in text for phrase in phrases)


def text_contains_all(
    text: str,
    phrases: list[str],
    case_sensitive: bool = False,
) -> bool:
    """
    Check if text contains all of the given phrases.

    Args:
        text: Text to search in
        phrases: List of phrases to look for
        case_sensitive: Whether matching is case-sensitive

    Returns:
        True if all phrases found
    """
    if not case_sensitive:
        text = text.lower()
        phrases = [p.lower() for p in phrases]
    return all(phrase in text for phrase in phrases)


def text_matches_pattern(
    text: str,
    pattern: str,
    flags: int = re.IGNORECASE,
) -> bool:
    """
    Check if text matches a regex pattern.

    Args:
        text: Text to match
        pattern: Regex pattern
        flags: Regex flags (default: case-insensitive)

    Returns:
        True if pattern matches anywhere in text
    """
    return bool(re.search(pattern, text, flags))


def normalize_whitespace(text: str) -> str:
    """
    Normalize whitespace in text (collapse multiple spaces, trim).

    Useful for comparing text that may have formatting differences.
    """
    return " ".join(text.split())


# =============================================================================
# List / Collection Helpers
# =============================================================================


def find_by_key(
    items: list[dict[str, Any]],
    key: str,
    value: Any,
) -> dict[str, Any] | None:
    """
    Find first item in list where item[key] == value.

    Args:
        items: List of dictionaries
        key: Key to match
        value: Value to match

    Returns:
        First matching item or None

    Example:
        user = find_by_key(users, "user_id", "user_123")
    """
    for item in items:
        if isinstance(item, dict) and item.get(key) == value:
            return item
    return None


def filter_by_key(
    items: list[dict[str, Any]],
    key: str,
    value: Any,
) -> list[dict[str, Any]]:
    """
    Filter items where item[key] == value.

    Args:
        items: List of dictionaries
        key: Key to match
        value: Value to match

    Returns:
        List of matching items

    Example:
        cancelled = filter_by_key(orders, "status", "cancelled")
    """
    return [item for item in items if isinstance(item, dict) and item.get(key) == value]


def count_by_key(
    items: list[dict[str, Any]],
    key: str,
    value: Any,
) -> int:
    """
    Count items where item[key] == value.

    Args:
        items: List of dictionaries
        key: Key to match
        value: Value to match

    Returns:
        Count of matching items
    """
    return len(filter_by_key(items, key, value))


# =============================================================================
# Public API exports
# =============================================================================

__all__ = [
    # Dictionary helpers
    "check_dict_params",
    "dict_diff",
    "get_nested",
    # Tool call helpers
    "last_tool_name",
    "first_tool_name",
    "count_tool_calls",
    "find_tool_calls",
    "tool_was_called",
    "get_tool_argument",
    # Text helpers
    "text_contains_any",
    "text_contains_all",
    "text_matches_pattern",
    "normalize_whitespace",
    # Collection helpers
    "find_by_key",
    "filter_by_key",
    "count_by_key",
]
