"""Unit tests for deterministic failure attribution."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.utils.recorded_calls import recorded_call
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
    ToolExecutionStatus,
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


def test_provision_error_classification():
    """A trajectory carrying ``PROVISION_ERROR`` classifies as ``provision_failure`` —
    substrate came up wrong before the trial body ran, so the failure is
    infra-side, not model-side."""
    traj = _base_trajectory()
    traj.status = TrialStatus.ERROR
    traj.termination_reason = TerminationReason.PROVISION_ERROR

    assert is_failed_trajectory(traj) is True
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "provision_failure"
    assert attribution["deterministic"] is True
    assert any(e["kind"] == "termination_reason" for e in attribution["evidence"])

    # A provision failure (which a reset-recipe failure surfaces as) counts
    # toward the run's failed attempts in the summary rollup.
    summary = summarize_failure_attributions([attribution])
    assert summary["total_failed_attempts"] == 1
    assert summary["by_failure_class"]["provision_failure"] == 1


def test_tool_argument_classification():
    traj = _base_trajectory()
    traj.tool_log = [
        recorded_call(
            "db_query",
            status=ToolExecutionStatus.INVALID_ARGUMENTS,
            output="Invalid arguments: 'id' is required",
        )
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


def test_summary_coverage_is_none_when_no_failures():
    """0/0 deterministic attribution coverage is meaningless — return None.

    Previously returned 0.0, which the analyze_run.py reporter then
    formatted as "0.000" alongside successful runs and confused readers.
    """
    summary = summarize_failure_attributions([])
    assert summary["total_failed_attempts"] == 0
    assert summary["deterministic_attribution_coverage"] is None


def test_infrastructure_class_for_connection_errors():
    """ERR_CONNECTION_REFUSED in messages → infrastructure failure class."""
    traj = _base_trajectory()
    traj.messages = [
        Message(role=MessageRole.USER, content="please check the portal"),
        Message(
            role=MessageRole.ASSISTANT,
            content="The browser returned net::ERR_CONNECTION_REFUSED at http://mock-web:8080",
        ),
    ]
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "infrastructure"
    assert attribution["deterministic"] is True
    kinds = {ev["kind"] for ev in attribution["evidence"]}
    assert "connection_errors" in kinds


def test_grade_fail_patterns_extracted_from_reasons_string():
    traj = _base_trajectory()
    assert traj.grade is not None
    traj.grade.reasons = (
        "Files: PASS: order ref; "
        "FAIL: must mention compliance escalation; "
        "FAIL: must mention 7 business days"
    )
    attribution = attribute_failure(traj)
    fail_evidence = [ev for ev in attribution["evidence"] if ev["kind"] == "grade_fail_patterns"]
    assert len(fail_evidence) == 1
    assert any("compliance escalation" in p for p in fail_evidence[0]["patterns"])


def test_missing_write_file_tool_when_grading_expected_files():
    traj = _base_trajectory()
    assert traj.grade is not None
    traj.grade.reasons = "Files: FAIL: No files match /env/fs/agent-visible/submissions/*"
    traj.tool_log = [recorded_call("browser")]
    attribution = attribute_failure(traj)
    missing_tool_evidence = [ev for ev in attribution["evidence"] if ev["kind"] == "missing_tool"]
    assert len(missing_tool_evidence) == 1
    assert missing_tool_evidence[0]["tool"] == "write_file"
