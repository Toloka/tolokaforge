"""Unit tests for :class:`RunnerRPCTrialGrader` — the three grading branches.

Uses a stub :class:`RuntimeBackend` that captures ``grade_trial`` calls so
each branch's runner interaction is asserted directly. No gRPC involved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tests.canonical._factories import make_env_endpoints, make_task_description
from tolokaforge.core.models import (
    JudgeStatus,
    Metrics,
    ModelConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.trial import TrialSpec
from tolokaforge.core.trial_grader import RunnerRPCTrialGrader

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
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "trial_id": trial_id,
                "llm_messages_json": llm_messages_json,
                "grading_components": grading_components,
            }
        )
        return self._grade_result


def _make_spec() -> TrialSpec:
    return TrialSpec(
        trial_id="task-1:0",
        run_id="run-1",
        task=make_task_description(task_id="task-1"),
        agent_model_config=ModelConfig(provider="openai", name="gpt-4"),
        env_endpoints=make_env_endpoints(),
    )


def _make_task_config():
    from tolokaforge.core.models import (
        InitialStateConfig,
        TaskConfig,
        ToolsConfig,
        UserSimulatorConfig,
    )

    return TaskConfig(
        task_id="task-1",
        name="task-1",
        category="test",
        description="grader unit-test task",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        user_simulator=UserSimulatorConfig(mode="scripted"),
        grading="grading.yaml",
    )


def _make_trajectory(
    status: TrialStatus = TrialStatus.COMPLETED,
    termination_reason: TerminationReason | None = None,
) -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id="task-1",
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=status,
        termination_reason=termination_reason,
        messages=[],
        metrics=Metrics(),
    )


class TestAutoFailBranches:
    """Trajectories that never reach the runner produce a synthesised
    fail-`Grade` without calling ``grade_trial``.
    """

    def test_error_status_auto_fails(self) -> None:
        backend = _StubBackend()
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.ERROR)

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert grade.score == 0.0
        assert "Trial failed with status: error" in grade.reasons
        assert backend.calls == []

    def test_timeout_status_auto_fails(self) -> None:
        backend = _StubBackend()
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.TIMEOUT)

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert grade.score == 0.0
        assert "Trial failed with status: timeout" in grade.reasons
        assert backend.calls == []

    def test_stuck_detected_auto_fails(self) -> None:
        backend = _StubBackend()
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.STUCK_DETECTED,
        )

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert grade.score == 0.0
        assert "stuck" in grade.reasons.lower()
        assert backend.calls == []


class TestRunnerRPCBranch:
    """Completed trajectories dispatch to ``grade_trial`` and materialise
    the returned dict into a :class:`Grade`.
    """

    def test_success_path_produces_grade(self) -> None:
        backend = _StubBackend()
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

        assert grade.binary_pass is True
        assert grade.score == 1.0
        assert grade.components.state_checks == 1.0
        assert len(backend.calls) == 1
        assert backend.calls[0]["trial_id"] == "task-1:0"

    def test_grpc_failure_falls_through_to_fail_grade(self) -> None:
        backend = _StubBackend(
            grade_result={"success": False, "grade": None, "error": "runner exploded"}
        )
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert grade.score == 0.0
        assert "Grading RPC failed" in grade.reasons
        assert "runner exploded" in grade.reasons

    def test_missing_grade_dict_falls_through_to_fail_grade(self) -> None:
        backend = _StubBackend(grade_result={"success": True, "grade": None})
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert grade.score == 0.0

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
                    },
                    "judge_status": 1,
                },
            }
        )
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

        assert grade.judge_usage is not None
        assert grade.judge_usage.calls == 2
        assert grade.judge_usage.prompt_tokens == 100
        assert grade.judge_usage.cost_usd == 0.001
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
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

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
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(_make_spec(), _make_task_config(), traj, "sysprompt")

        assert grade.state_diff is None


class TestJudgeMessagesJson:
    """The transcript sent to the runner encodes the agent's policy as
    a leading ``system`` message. Empty trajectory + empty prompt yields
    ``None`` (nothing to grade).
    """

    def test_empty_trajectory_and_prompt_sends_none(self) -> None:
        backend = _StubBackend()
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.COMPLETED)

        grader.grade(_make_spec(), _make_task_config(), traj, "")

        assert backend.calls[0]["llm_messages_json"] is None

    def test_prompt_alone_still_sends_messages(self) -> None:
        backend = _StubBackend()
        grader = RunnerRPCTrialGrader(runtime_backend=backend)
        traj = _make_trajectory(status=TrialStatus.COMPLETED)

        grader.grade(_make_spec(), _make_task_config(), traj, "you are a helper")

        assert backend.calls[0]["llm_messages_json"] is not None
        import json

        parsed = json.loads(backend.calls[0]["llm_messages_json"])
        assert parsed == [{"role": "system", "content": "you are a helper"}]
