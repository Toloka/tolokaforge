"""Unit tests for state diff utilities"""

import json

import pytest

from tolokaforge.core.utils.diff import (
    calculate_state_diff,
    format_diff_summary,
    get_diff_stats,
    serialize_state,
)

pytestmark = pytest.mark.unit


def test_serialize_state_basic():
    """Test basic state serialization"""
    state = {"b": 2, "a": 1, "c": 3}

    result = serialize_state(state)

    # Keys should be sorted
    assert '"a":' in result
    assert result.index('"a":') < result.index('"b":')
    assert result.index('"b":') < result.index('"c":')


def test_serialize_state_nested():
    """Test serialization of nested structures"""
    state = {"users": {"user_2": {"name": "Bob"}, "user_1": {"name": "Alice"}}, "orders": []}

    result = serialize_state(state)

    # Should be valid JSON
    parsed = json.loads(result)
    assert parsed == state

    # Keys should be sorted at all levels
    assert '"orders":' in result
    assert result.index('"orders":') < result.index('"users":')


def test_calculate_state_diff_identical():
    """Test diff of identical states"""
    state1 = {"a": 1, "b": 2}
    state2 = {"a": 1, "b": 2}

    result = calculate_state_diff(state1, state2)

    assert result["has_diff"] is False
    assert result["diff_lines"] == 0
    assert result["diff"] == ""


def test_calculate_state_diff_different():
    """Test diff of different states"""
    expected = {"a": 1, "b": 2, "c": 3}
    actual = {"a": 1, "b": 999, "d": 4}

    result = calculate_state_diff(expected, actual)

    assert result["has_diff"] is True
    assert result["diff_lines"] > 0

    diff = result["diff"]
    assert "---" in diff
    assert "+++" in diff
    assert "-" in diff and "2" in diff
    assert "+" in diff and "999" in diff
    assert "-" in diff and "c" in diff
    assert "+" in diff and "d" in diff


def test_format_diff_summary_no_diff():
    """Test formatting when there's no diff"""
    diff_result = {"has_diff": False, "diff": "", "diff_lines": 0}

    summary = format_diff_summary(diff_result)
    assert summary == "States are identical"


def test_format_diff_summary_large_diff():
    """Test formatting large diff (over max_lines)"""
    diff_lines = ["line " + str(i) for i in range(100)]
    diff_text = "\n".join(diff_lines)

    diff_result = {"has_diff": True, "diff": diff_text, "diff_lines": 100}

    summary = format_diff_summary(diff_result, max_lines=50)

    assert "line 0" in summary
    assert "line 49" in summary
    assert "line 50" not in summary
    assert "(50 more lines omitted)" in summary


def test_get_diff_stats_with_changes():
    """Test stats for diff with changes"""
    diff_text = "\n".join(
        [
            "--- expected_state",
            "+++ actual_state",
            "@@ -1,5 +1,5 @@",
            " {",
            '-  "a": 1',
            '-  "b": 2',
            '+  "a": 2',
            '+  "c": 3',
            " }",
        ]
    )

    diff_result = {"has_diff": True, "diff": diff_text, "diff_lines": 9}

    stats = get_diff_stats(diff_result)

    assert stats["total_lines"] == 9
    assert stats["additions"] == 2
    assert stats["deletions"] == 2
    assert stats["context"] == 5


def test_end_to_end_diff_workflow():
    """Test complete workflow from states to formatted summary"""
    expected = {
        "users": {"user_1": {"name": "Alice", "email": "alice@example.com"}},
        "orders": [{"id": "order_1", "total": 100}],
    }

    actual = {
        "users": {"user_1": {"name": "Alice", "email": "alice@newdomain.com"}},
        "orders": [{"id": "order_1", "total": 100}, {"id": "order_2", "total": 200}],
    }

    diff_result = calculate_state_diff(expected, actual)

    assert diff_result["has_diff"] is True

    summary = format_diff_summary(diff_result, max_lines=100)
    assert "example.com" in summary
    assert "newdomain.com" in summary
    assert "order_2" in summary

    stats = get_diff_stats(diff_result)
    assert stats["additions"] > 0
    assert stats["deletions"] > 0
