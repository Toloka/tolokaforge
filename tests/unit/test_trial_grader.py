"""Unit tests for :class:`RunnerRPCTrialGrader` — the three grading branches.

Uses a stub :class:`RuntimeBackend` that captures ``grade_trial`` calls so
each branch's runner interaction is asserted directly. No gRPC involved.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory, make_trial_spec
from tests.unit.test_failure_attribution import outcome_cells
from tolokaforge.core.failure_attribution import TrialOutcomeClass
from tolokaforge.core.models import Grade, JudgeStatus, TerminationReason, TrialStatus
from tolokaforge.core.trial_grader import GradingFailedError, RunnerRPCTrialGrader

pytestmark = pytest.mark.unit


class _StubBackend:
    """Records ``grade_trial`` calls; returns a canned result."""

    def __init__(self, grade_result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._grade_result = grade_result or {
            "success": True,
            "grade": {
                "binary_pass": True,
                "score": 1.0,
                "components": {
                    "state_checks": 1.0,
                    "transcript_rules": -1.0,
                    "llm_judge": -1.0,
                    "custom_checks": -1.0,
                },
                "reasons": "ok",
            },
        }

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "trial_id": trial_id,
                "llm_messages_json": llm_messages_json,
                "grading_components": grading_components,
                "termination_reason": termination_reason,
            }
        )
        return self._grade_result


def _make_grader(backend: _StubBackend | None = None) -> tuple[RunnerRPCTrialGrader, MagicMock]:
    logger = MagicMock()
    grader = RunnerRPCTrialGrader(
        runner_address="stub:0",
        logger=logger,
        runner_client=backend or _StubBackend(),
    )
    return grader, logger


class TestAutoFailBranches:
    """Trajectories that never reach the runner produce a synthesised
    fail-`Grade` without calling ``grade_trial`` — and log the auto-fail.
    """

    def test_error_status_auto_fails_and_logs(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.ERROR)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert grade.score == 0.0
        assert "Trial failed with status: error" in grade.reasons
        assert backend.calls == []
        logger.info.assert_called_once()
        call_args = logger.info.call_args
        assert call_args.args[0] == "Trial did not complete successfully - automatic fail"
        assert call_args.kwargs["status"] == "error"

    def test_timeout_status_auto_fails_and_logs(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.TIMEOUT)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert "Trial failed with status: timeout" in grade.reasons
        assert backend.calls == []
        assert logger.info.call_args.kwargs["status"] == "timeout"

    def test_stuck_detected_auto_fails_and_logs(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)
        traj = make_trajectory(
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.STUCK_DETECTED,
        )

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert "stuck" in grade.reasons.lower()
        assert backend.calls == []
        assert logger.info.call_args.args[0] == "Trial stuck - automatic fail"
        assert logger.info.call_args.kwargs["termination_reason"] == "stuck_detected"


class TestInfrastructureAbortProducesNoGrade:
    """A trial the infrastructure killed is not graded at all.

    ``None`` rather than ``Grade(score=0.0)``: ``Grade.score`` is a required
    ``[0, 1]`` float, so any grade for a trial that never ran has to carry a
    number that describes work nobody did. Absence cannot be misread as zero,
    and a consumer that forgets to branch fails loudly instead of quietly
    reporting a model failure.
    """

    @pytest.mark.parametrize("cell", outcome_cells())
    def test_none_exactly_for_the_abort_cells(self, cell) -> None:
        status, reason, outcome_class, _ = cell
        backend = _StubBackend()
        grader, _ = _make_grader(backend)

        grade = grader.grade(
            make_trial_spec(),
            make_trajectory(status=status, termination_reason=reason),
            "sysprompt",
        )

        if outcome_class is TrialOutcomeClass.INFRASTRUCTURE_ABORT:
            assert grade is None
            assert backend.calls == [], "an ungraded trial must not reach the runner"
        else:
            assert isinstance(grade, Grade)

    def test_the_abort_is_logged_with_its_reason(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)

        grader.grade(
            make_trial_spec(),
            make_trajectory(
                status=TrialStatus.ERROR, termination_reason=TerminationReason.RATE_LIMIT
            ),
            "sysprompt",
        )

        assert logger.info.call_args.args[0] == "Trial aborted by infrastructure - not graded"
        assert logger.info.call_args.kwargs["termination_reason"] == "rate_limit"


class TestRunnerRPCBranch:
    """Completed trajectories dispatch to ``grade_trial`` and materialise
    the returned dict into a :class:`Grade`.
    """

    def test_success_path_produces_grade_and_logs(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.binary_pass is True
        assert grade.score == 1.0
        assert grade.components.state_checks == 1.0
        assert len(backend.calls) == 1
        assert backend.calls[0]["trial_id"] == "task-1:0"
        logger.info.assert_called_once()
        assert logger.info.call_args.args[0] == "Grading via Runner RPC"

    def test_grpc_failure_raises_instead_of_scoring_the_trial_zero(self) -> None:
        """A failed grading run publishes no verdict at all.

        A normally-terminated trial classifies ``MEASURED``, so a host-side
        ``score=0.0`` would enter ``success_rate`` / ``avg_score`` / ``pass@k``
        as an agent failure that grading never established.
        """
        backend = _StubBackend(
            grade_result={"success": False, "grade": None, "error": "runner exploded"}
        )
        grader, logger = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        with pytest.raises(GradingFailedError) as excinfo:
            grader.grade(make_trial_spec(), traj, "sysprompt")

        assert "runner exploded" in str(excinfo.value)
        assert "task-1:0" in str(excinfo.value)
        logger.error.assert_called_once()
        assert logger.error.call_args.args[0] == "Grading RPC failed"
        assert logger.error.call_args.kwargs["error"] == "runner exploded"

    def test_a_successful_rpc_carrying_no_grade_also_raises(self) -> None:
        backend = _StubBackend(grade_result={"success": True, "grade": None})
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        with pytest.raises(GradingFailedError):
            grader.grade(make_trial_spec(), traj, "sysprompt")

    def test_judge_report_populates_judge_usage(self) -> None:
        backend = _StubBackend(
            grade_result={
                "success": True,
                "grade": {
                    "binary_pass": True,
                    "score": 0.75,
                    "components": {"state_checks": 1.0, "llm_judge": 0.5},
                    "reasons": "partial",
                    "judge_report": {
                        "calls": 2,
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "reasoning_tokens": 10,
                        "cost_usd": 0.001,
                        "tool_calls": 1,
                        "consistency_rejections": 1,
                    },
                    "judge_status": 1,
                },
            }
        )
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.judge_usage is not None
        assert grade.judge_usage.calls == 2
        assert grade.judge_usage.prompt_tokens == 100
        assert grade.judge_usage.cost_usd == 0.001
        assert grade.judge_usage.consistency_rejections == 1
        assert grade.judge_status == JudgeStatus.from_proto(1)

    def test_state_diff_json_parses(self) -> None:
        backend = _StubBackend(
            grade_result={
                "success": True,
                "grade": {
                    "binary_pass": False,
                    "score": 0.0,
                    "components": {"state_checks": 0.0},
                    "reasons": "diff",
                    "state_diff_json": '{"missing_rows": ["a", "b"]}',
                },
            }
        )
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.state_diff == {"missing_rows": ["a", "b"]}

    def test_state_diff_json_malformed_is_ignored(self) -> None:
        backend = _StubBackend(
            grade_result={
                "success": True,
                "grade": {
                    "binary_pass": False,
                    "score": 0.0,
                    "components": {"state_checks": 0.0},
                    "reasons": "diff",
                    "state_diff_json": "not-json",
                },
            }
        )
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.state_diff is None


class TestTerminationReasonForwarding:
    """The grader hands the runner the trial's own termination reason, as its
    wire value, so grading can tell a deliberate finish from a spent budget."""

    def test_reason_crosses_as_its_wire_value(self) -> None:
        backend = _StubBackend()
        grader, _ = _make_grader(backend)
        traj = make_trajectory(
            status=TrialStatus.COMPLETED, termination_reason=TerminationReason.AGENT_DONE
        )

        grader.grade(make_trial_spec(), traj, "sysprompt")

        assert backend.calls[0]["termination_reason"] == "agent_done"

    def test_trajectory_without_a_reason_forwards_none(self) -> None:
        backend = _StubBackend()
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED, termination_reason=None)

        grader.grade(make_trial_spec(), traj, "sysprompt")

        assert backend.calls[0]["termination_reason"] is None


class TestJudgeMessagesJson:
    """The transcript sent to the runner encodes the agent's policy as
    a leading ``system`` message. Empty trajectory + empty prompt yields
    ``None`` (nothing to grade).
    """

    def test_empty_trajectory_and_prompt_sends_none(self) -> None:
        backend = _StubBackend()
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grader.grade(make_trial_spec(), traj, "")

        assert backend.calls[0]["llm_messages_json"] is None

    def test_prompt_alone_still_sends_messages(self) -> None:
        import json

        backend = _StubBackend()
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grader.grade(make_trial_spec(), traj, "you are a helper")

        assert backend.calls[0]["llm_messages_json"] is not None
        parsed = json.loads(backend.calls[0]["llm_messages_json"])
        assert parsed == [{"role": "system", "content": "you are a helper"}]
