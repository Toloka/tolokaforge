"""Light unit tests for ``automation.reprobe`` pure helpers.

``k_group`` (junit probe name -> precise ``-k`` group) and the failure selectors
``failed_probes`` / ``failed_wire_tasks`` are deterministic; the pytest/tolokaforge
shell-outs (``run_capability_flat`` / ``run_wire_task`` / ``run``) are not unit-tested.
"""

from __future__ import annotations

import automation.reprobe as reprobe
import pytest

pytestmark = pytest.mark.unit


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


_BASELINE = {
    "capability": {"per_probe": [{"probe": "test_cap[x]", "passed": 1, "runs": 5}]},
    "variants": {"per_probe": [{"probe": "test_variant_v[y]", "passed": 0, "runs": 5}]},
    "wire": {"tool_arg_rejections": {"by_task": {"task_w": 4}}},
}


def test_select_targets_default_is_all_failed_plus_wire():
    probes, wire = reprobe.select_reprobe_targets(_BASELINE, None, wire_only=False, skip_wire=False)
    assert probes == ["test_cap[x]", "test_variant_v[y]"]
    assert wire == ["task_w"]


def test_select_targets_explicit_targets_and_skip_wire():
    probes, wire = reprobe.select_reprobe_targets(
        _BASELINE, "test_cap[x], test_other[z]", wire_only=False, skip_wire=True
    )
    assert probes == ["test_cap[x]", "test_other[z]"]
    assert wire == []


def test_select_targets_wire_only_selects_no_probes():
    # The final wire-verification pass: the fix loop iterates with --skip-wire, so this is
    # the only place a wire-evidenced fix gets a post-fix measurement.
    probes, wire = reprobe.select_reprobe_targets(_BASELINE, None, wire_only=True, skip_wire=False)
    assert probes == []
    assert wire == ["task_w"]


def test_select_targets_conflicting_flags_raise():
    with pytest.raises(ValueError, match="mutually exclusive"):
        reprobe.select_reprobe_targets(_BASELINE, None, wire_only=True, skip_wire=True)
    with pytest.raises(ValueError, match="pass one or the other"):
        reprobe.select_reprobe_targets(_BASELINE, "test_cap[x]", wire_only=True, skip_wire=False)
