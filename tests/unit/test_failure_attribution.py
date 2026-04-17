"""Unit tests for deterministic failure attribution."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tolokaforge.core.failure_attribution import (
    attribute_failure,
    is_failed_trajectory,
    summarize_failure_attributions,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    MessageRole,
    Metrics,
    TerminationReason,
    Trajectory,
    TrialStatus,
)

pytestmark = pytest.mark.unit


def _base_trajectory() -> Trajectory:
    return Trajectory(
        task_id="task_x",
        trial_index=0,
        start_ts=datetime.now(tz=timezone.utc),
        end_ts=datetime.now(tz=timezone.utc),
        messages=[Message(role=MessageRole.USER, content="hello")],
        metrics=Metrics(),
        grade=Grade(binary_pass=False, score=0.0, components=GradeComponents(), reasons="failed"),
    )


def test_timeout_classification():
    traj = _base_trajectory()
    traj.status = TrialStatus.TIMEOUT
    traj.termination_reason = TerminationReason.TIMEOUT

    assert is_failed_trajectory(traj) is True
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "timeout_or_resource"
    assert attribution["deterministic"] is True


def test_tool_argument_classification():
    traj = _base_trajectory()
    traj.tool_log = [
        {
            "tool": "db_query",
            "success": False,
            "error": "Invalid arguments: 'id' is required",
        }
    ]
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "tool_arguments"
    assert attribution["deterministic"] is True


def test_grader_contract_classification():
    traj = _base_trajectory()
    assert traj.grade is not None
    traj.grade.state_diff = {"orders.status": {"expected": "done", "actual": "pending"}}
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "grader_contract"
    assert attribution["deterministic"] is True


def test_attribution_summary():
    a = {
        "failure_class": "tool_execution",
        "deterministic": True,
        "evidence": [{"tool": "browser"}],
    }
    b = {"failure_class": "model_reasoning", "deterministic": False, "evidence": []}
    summary = summarize_failure_attributions([a, b])
    assert summary["total_failed_attempts"] == 2
    assert summary["by_failure_class"]["tool_execution"] == 1
    assert summary["by_failure_class"]["model_reasoning"] == 1
    assert summary["by_tool"]["browser"] == 1
