"""
Shared helpers for custom checks — two distinct audiences.

Author-facing utilities (``check_dict_params``, ``last_tool_name``,
``text_contains_any``, …) are imported by a task's ``checks.py`` to compare
dicts, inspect tool-call sequences, and pattern-match text. Domain-specific
helpers should be defined at the project level (e.g. ``tasks/airline/
check_helpers.py``).

Framework-internal utilities (``custom_checks_enabled``, ``build_check_context``,
``custom_checks_reason``) are called by BOTH grading paths —
:class:`~tolokaforge.core.grading.combine.GradingEngine` on the host and the
runner-side :class:`~tolokaforge.runner.service.RunnerServiceImpl` — so the two
cannot diverge on when custom checks run, on the ``final_env_state`` shape
transform they depend on, or on what the grade says about the suite's verdict.

Usage in ``checks.py``:
    from tolokaforge.core.grading.checks_helpers import (
        check_dict_params, last_tool_name, text_contains_any
    )
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeGuard

from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckResultSet,
    CheckStatus,
    CustomChecksConfig,
    EnvironmentState,
    TaskContext,
    Transcript,
)

#: Names the ``custom_checks`` component in ``Grade.reasons``, the way ``Transcript:``
#: and ``Trace checks:`` name theirs. Both substrates emit segments under it.
CUSTOM_CHECKS_REASON_PREFIX = "Custom checks:"

# =============================================================================
# Framework-internal: shared gate + CheckContext builder (both grading paths)
# =============================================================================


def custom_checks_enabled(
    custom_checks: Mapping[str, Any] | None,
) -> TypeGuard[dict[str, Any]]:
    """Whether a pack's raw ``grading.custom_checks`` block opts into execution.

    The one gate both substrates, the adapter's ``checks.py`` delivery decision
    and the runner's accounted-keys ledger ask. A delivery gate that disagreed
    with an execution gate is precisely the shape that produces a pack claiming
    custom checks whose ``checks.py`` never arrives.

    Validates before reading, so a mistyped key raises here rather than reading
    ``enabled`` as its default and disabling a scored component silently. The
    trial's registration is the loud end of that: ``RegisterTrial`` turns the
    ``ValidationError`` into a refusal naming the offending config.

    Raises:
        ValidationError: the block is non-empty and is not a valid
            :class:`~tolokaforge.core.grading.checks_interface.CustomChecksConfig`.
    """
    if not custom_checks:
        return False
    return CustomChecksConfig(**custom_checks).enabled


def build_check_context(
    *,
    initial_state_json_db: dict[str, Any] | None,
    final_env_state: dict[str, Any],
    transcript: Transcript,
    task: TaskContext,
) -> CheckContext:
    """Build a :class:`CheckContext` with the canonical state-shape transform.

    Encodes the single rule both grading paths must apply so that a check
    written against one path reads identical evidence from the other:

    * ``initial_state.data`` = ``initial_state_json_db`` when it is a dict, else
      an empty dict. Author-declared initial DB rows only; other init sources
      (filesystem, mock_web) are not surfaced here.
    * ``final_state.data`` is picked from ``final_env_state`` by precedence —
      the ``"agent"`` dict wins over the ``"db"`` dict, and the flat
      ``final_env_state`` is used if neither key holds a dict. This mirrors the
      adapter convention that ``final_env_state["agent"]`` carries the mutated
      state after tool calls.
    * When ``final_env_state`` supplies a ``"filesystem"`` key and the chosen
      level lacks one, ``filesystem`` is merged into ``final_state.data`` so a
      check can read agent-produced files alongside the DB rows it just
      inspected.

    ``transcript`` and ``task`` are passed through — each grading path owns
    its own transcript/task construction (rich :class:`Trajectory` on the host,
    wire message dicts + :class:`~tolokaforge.runner.models.TaskDescription`
    on the runner).
    """
    initial_data = initial_state_json_db if isinstance(initial_state_json_db, dict) else {}

    if isinstance(final_env_state.get("agent"), dict):
        final_state_data = final_env_state["agent"]
    elif isinstance(final_env_state.get("db"), dict):
        final_state_data = final_env_state["db"]
    else:
        final_state_data = final_env_state

    if "filesystem" in final_env_state and "filesystem" not in final_state_data:
        final_state_data = {**final_state_data, "filesystem": final_env_state["filesystem"]}

    return CheckContext(
        initial_state=EnvironmentState(data=initial_data),
        final_state=EnvironmentState(data=final_state_data),
        transcript=transcript,
        task=task,
    )


def custom_checks_reason(result: CheckResultSet) -> str:
    """The sentence ``Grade.reasons`` carries for the ``custom_checks`` component.

    Never empty: a caller with no suite to describe — a pack declaring no
    ``custom_checks`` block, or one that disabled it — does not call this.

    Four shapes, and which one a set falls into is the set's own answer rather than
    the caller's. A suite carrying ``error`` could not run, and the error is the only
    thing that says why. A suite that reached no verdict says so instead of reporting
    an aggregate over nothing, which is ``0.0`` and indistinguishable from having
    failed. A suite that reached verdicts reports its score, how many checks reached
    one, and every check that reached one and lost — by name and message, the way
    ``Transcript:`` and ``Trace check <id>:`` name theirs. A skipped check reached no
    verdict, so it is counted and not named.

    Only the checks that did not pass are named, which is what keeps a downstream
    reader honest: :func:`~tolokaforge.core.failure_attribution.attribute_failure`
    keeps the segments of ``reasons`` matching ``"FAIL"`` case-insensitively, so a
    sentence describing a suite with a losing check contains ``fail`` and one
    describing a suite without carries no check name that could supply it.
    """
    if result.error:
        return f"{CUSTOM_CHECKS_REASON_PREFIX} the suite could not run — {result.error}"

    decided = [r for r in result.results if r.status != CheckStatus.SKIPPED]
    skipped = len(result.results) - len(decided)
    if not decided:
        nothing = "the file declared no check" if not result.results else f"all {skipped} skipped"
        return f"{CUSTOM_CHECKS_REASON_PREFIX} no check reached a verdict — {nothing}"

    head = f"{CUSTOM_CHECKS_REASON_PREFIX} score={result.aggregate_score:.2f}"
    tail = f", {skipped} skipped" if skipped else ""
    lost = [r for r in decided if r.status != CheckStatus.PASSED]
    if not lost:
        return f"{head}, all {len(decided)} checks passed{tail}"
    named = "; ".join(f"{r.check_name}: {r.message}" for r in lost)
    return f"{head}, {len(lost)} of {len(decided)} checks failed{tail} — {named}"


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
