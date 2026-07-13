"""Light unit tests for ``automation.reprobe`` pure helpers.

``k_group`` (junit probe name -> precise ``-k`` group) and the failure selectors
``failed_probes`` / ``failed_wire_tasks`` are deterministic; the pytest/tolokaforge
shell-outs (``run_capability_flat`` / ``run_wire_task`` / ``run``) are not unit-tested.
"""

from __future__ import annotations

import automation.reprobe as reprobe


def test_k_group_splits_param_into_and_group():
    # `func[param]` -> `(func and param)` (no brackets, which pytest -k rejects)
    assert reprobe.k_group("test_dict_map_tool_call[nested_in_object]") == (
        "(test_dict_map_tool_call and nested_in_object)"
    )


def test_k_group_without_param_is_bare_group():
    assert reprobe.k_group("test_basic_completion") == "(test_basic_completion)"


def test_failed_probes_selects_not_fully_passing():
    section = {
        "per_probe": [
            {"probe": "test_a[x]", "passed": 15, "runs": 15},  # fully passed -> excluded
            {"probe": "test_b[y]", "passed": 9, "runs": 10},  # partial -> included
            {"probe": "test_c[z]", "passed": 0, "runs": 10},  # all failed -> included
        ]
    }
    assert reprobe.failed_probes(section) == ["test_b[y]", "test_c[z]"]


def test_failed_probes_tolerates_none_section():
    assert reprobe.failed_probes(None) == []
    assert reprobe.failed_probes({}) == []


def test_failed_wire_tasks_sorted_from_by_task():
    findings = {"wire": {"tool_arg_rejections": {"by_task": {"task_b": 3, "task_a": 1}}}}
    assert reprobe.failed_wire_tasks(findings) == ["task_a", "task_b"]


def test_failed_wire_tasks_tolerates_missing():
    assert reprobe.failed_wire_tasks({}) == []
    assert reprobe.failed_wire_tasks({"wire": {}}) == []
