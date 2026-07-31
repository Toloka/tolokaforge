"""Unit tests for deterministic failure attribution."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.failure_attribution import (
    EXCLUDED_TYPED_REASONS,
    TrialOutcomeClass,
    attribute_failure,
    classify_trial_outcome,
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
from tolokaforge.core.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

_MEASURED = TrialOutcomeClass.MEASURED
_HARNESS = TrialOutcomeClass.HARNESS_ERROR
_ABORT = TrialOutcomeClass.INFRASTRUCTURE_ABORT

# Every ``TrialStatus`` x (``TerminationReason`` | None) cell, with both answers
# written out: what the trial is counted as, and whether it is retried. The
# cross-product is the point — over the handful of pairs a reader expects, any
# default looks like any other, so a test restricted to them proves the table's
# scope and never its truth.
#
# The two columns disagree in three reachable cells — ``(error, api_error)``,
# ``(error, error)`` and ``(timeout, timeout)`` are retried *and* counted — and
# that is the design, not a defect: whether an attempt is worth repeating and
# whether it measured the agent are different questions. Deriving either column
# from the other is what this table exists to prevent.
_OUTCOME_CELLS: tuple[
    tuple[TrialStatus, TerminationReason | None, TrialOutcomeClass, bool], ...
] = (
    (TrialStatus.COMPLETED, TerminationReason.AGENT_DONE, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.USER_STOP, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.STUCK_DETECTED, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.MAX_TURNS, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.TIMEOUT, _MEASURED, True),
    (TrialStatus.COMPLETED, TerminationReason.ERROR, _HARNESS, True),
    (TrialStatus.COMPLETED, TerminationReason.RATE_LIMIT, _ABORT, True),
    (TrialStatus.COMPLETED, TerminationReason.API_TIMEOUT, _ABORT, False),
    (TrialStatus.COMPLETED, TerminationReason.API_ERROR, _MEASURED, True),
    (TrialStatus.COMPLETED, TerminationReason.PROVISION_ERROR, _ABORT, False),
    (TrialStatus.COMPLETED, None, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.AGENT_DONE, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.USER_STOP, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.STUCK_DETECTED, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.MAX_TURNS, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.TIMEOUT, _MEASURED, True),
    (TrialStatus.FAILED, TerminationReason.ERROR, _HARNESS, True),
    (TrialStatus.FAILED, TerminationReason.RATE_LIMIT, _ABORT, True),
    (TrialStatus.FAILED, TerminationReason.API_TIMEOUT, _ABORT, False),
    (TrialStatus.FAILED, TerminationReason.API_ERROR, _MEASURED, True),
    (TrialStatus.FAILED, TerminationReason.PROVISION_ERROR, _ABORT, False),
    (TrialStatus.FAILED, None, _HARNESS, False),
    (TrialStatus.TIMEOUT, TerminationReason.AGENT_DONE, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.USER_STOP, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.STUCK_DETECTED, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.MAX_TURNS, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.TIMEOUT, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.ERROR, _HARNESS, True),
    (TrialStatus.TIMEOUT, TerminationReason.RATE_LIMIT, _ABORT, True),
    (TrialStatus.TIMEOUT, TerminationReason.API_TIMEOUT, _ABORT, True),
    (TrialStatus.TIMEOUT, TerminationReason.API_ERROR, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.PROVISION_ERROR, _ABORT, False),
    (TrialStatus.TIMEOUT, None, _HARNESS, True),
    (TrialStatus.ERROR, TerminationReason.AGENT_DONE, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.USER_STOP, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.STUCK_DETECTED, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.MAX_TURNS, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.TIMEOUT, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.ERROR, _HARNESS, True),
    (TrialStatus.ERROR, TerminationReason.RATE_LIMIT, _ABORT, True),
    (TrialStatus.ERROR, TerminationReason.API_TIMEOUT, _ABORT, True),
    (TrialStatus.ERROR, TerminationReason.API_ERROR, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.PROVISION_ERROR, _ABORT, False),
    (TrialStatus.ERROR, None, _HARNESS, True),
)


def _cell_id(cell: tuple[TrialStatus, TerminationReason | None, TrialOutcomeClass, bool]) -> str:
    status, reason, _, _ = cell
    return f"{status.value}-{reason.value if reason else 'unset'}"


def outcome_cells() -> tuple:
    """The cross-product cells, as pytest params keyed by ``status-reason``."""
    return tuple(pytest.param(cell, id=_cell_id(cell)) for cell in _OUTCOME_CELLS)


def _cell_trajectory(status: TrialStatus, reason: TerminationReason | None) -> Trajectory:
    traj = _base_trajectory()
    traj.status = status
    traj.termination_reason = reason
    return traj


class TestOutcomeClassificationCrossProduct:
    """``classify_trial_outcome`` is total, and the exclusion default is an
    allowlist: a reason it has never heard of is counted, not dropped."""

    def test_the_table_covers_every_cell_exactly_once(self) -> None:
        cells = {(status, reason) for status, reason, _, _ in _OUTCOME_CELLS}
        expected = {
            (status, reason) for status in TrialStatus for reason in (*TerminationReason, None)
        }
        assert cells == expected
        assert len(_OUTCOME_CELLS) == len(expected) == 44

    @pytest.mark.parametrize("cell", outcome_cells())
    def test_every_cell_classifies_as_tabled(
        self, cell: tuple[TrialStatus, TerminationReason | None, TrialOutcomeClass, bool]
    ) -> None:
        status, reason, expected_class, _ = cell
        assert classify_trial_outcome(_cell_trajectory(status, reason)) is expected_class

    def test_an_unrecognised_reason_would_be_counted(self) -> None:
        """The allowlist default, asserted where it matters: exclusion is
        earned by membership, so a reason nobody classified stays in the
        denominator and stays visible."""
        counted = {
            reason
            for reason in TerminationReason
            if classify_trial_outcome(_cell_trajectory(TrialStatus.ERROR, reason)) is not _ABORT
        }
        assert counted == set(TerminationReason) - EXCLUDED_TYPED_REASONS


class TestRetryabilityIsIndependentOfCountability:
    """``Orchestrator._is_retryable_trajectory`` is pinned bit-identical over
    the same cells. It is deliberately NOT derived from the classification —
    the cells where the two answers differ are the design."""

    @pytest.mark.parametrize("cell", outcome_cells())
    def test_every_cell_retries_as_tabled(
        self, cell: tuple[TrialStatus, TerminationReason | None, TrialOutcomeClass, bool]
    ) -> None:
        status, reason, _, expected_retryable = cell
        traj = _cell_trajectory(status, reason)
        assert Orchestrator._is_retryable_trajectory(traj) is expected_retryable

    def test_an_auth_shaped_api_error_is_not_retried(self) -> None:
        """The one cell whose answer depends on the message rather than the
        pair: a bad key fails the same way on every attempt."""
        traj = _cell_trajectory(TrialStatus.ERROR, TerminationReason.API_ERROR)
        traj.messages = [
            Message(
                role=MessageRole.SYSTEM,
                content="API error: LLM API call failed: AuthenticationError - invalid key",
            )
        ]
        assert Orchestrator._is_retryable_trajectory(traj) is False
        # Countability does not consult the message, so it is unmoved.
        assert classify_trial_outcome(traj) is _MEASURED

    def test_the_two_answers_disagree_where_the_table_says_they_do(self) -> None:
        """A guard on the table itself: if these cells ever agree, one column
        was derived from the other and the regression lock stopped biting."""
        disagreements = {
            (status, reason)
            for status, reason, outcome_class, retryable in _OUTCOME_CELLS
            if retryable is not (outcome_class is _ABORT)
        }
        assert (TrialStatus.ERROR, TerminationReason.API_ERROR) in disagreements
        assert (TrialStatus.ERROR, TerminationReason.ERROR) in disagreements
        assert (TrialStatus.TIMEOUT, TerminationReason.TIMEOUT) in disagreements


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
