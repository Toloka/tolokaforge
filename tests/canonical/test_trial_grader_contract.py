"""Pin the ``TrialGrader`` Protocol contract.

Every concrete grader must satisfy the Protocol via ``isinstance`` (not
just structural type-hint compatibility) and produce a :class:`Grade`
with the same shape the conductor's grading phase used to produce
directly. This file is the load-bearing contract when future
implementations land (Judge-lift per GH #131, remote grader for the
multi-container future).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.canonical._factories import make_env_endpoints, make_task_description
from tolokaforge.core.models import Grade, Metrics, ModelConfig, Trajectory, TrialStatus
from tolokaforge.core.trial import TrialSpec
from tolokaforge.core.trial_grader import RunnerRPCTrialGrader, TrialGrader

pytestmark = pytest.mark.canonical


class _StubRuntimeBackendForGrading:
    """Minimal runtime-backend stand-in that satisfies the ``grade_trial``
    surface the grader actually calls."""

    def grade_trial(self, trial_id: str, **_kwargs: object) -> dict[str, object]:
        return {
            "success": True,
            "grade": {
                "binary_pass": True,
                "score": 1.0,
                "components": {"state_checks": 1.0},
                "reasons": "stub",
            },
        }


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; every implementation
    satisfies it structurally.
    """

    def test_runner_rpc_trial_grader_passes_isinstance(self) -> None:
        grader = RunnerRPCTrialGrader(runtime_backend=_StubRuntimeBackendForGrading())
        assert isinstance(grader, TrialGrader)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAGrader:
            pass

        assert not isinstance(_NotAGrader(), TrialGrader)

    def test_object_with_matching_shape_passes_isinstance(self) -> None:
        class _DuckGrader:
            def grade(
                self,
                spec: object,
                task_config: object,
                trajectory: object,
                agent_system_prompt: str,
            ) -> Grade:  # pragma: no cover — never called
                return Grade(binary_pass=True, score=1.0)

        assert isinstance(_DuckGrader(), TrialGrader)


class TestGradeShapeParity:
    """The Grade returned by the grader matches the shape the conductor
    used to produce inline. A regression here would silently break
    downstream consumers.
    """

    def _make_task_config(self):
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
            description="grader contract test",
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(),
            user_simulator=UserSimulatorConfig(mode="scripted"),
            grading="grading.yaml",
        )

    def _make_spec(self) -> TrialSpec:
        return TrialSpec(
            trial_id="task-1:0",
            run_id="run-1",
            task=make_task_description(task_id="task-1"),
            agent_model_config=ModelConfig(provider="openai", name="gpt-4"),
            env_endpoints=make_env_endpoints(),
        )

    def _make_trajectory(self) -> Trajectory:
        now = datetime.now(UTC)
        return Trajectory(
            task_id="task-1",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.COMPLETED,
            messages=[],
            metrics=Metrics(),
        )

    def test_success_grade_has_required_fields(self) -> None:
        grader = RunnerRPCTrialGrader(runtime_backend=_StubRuntimeBackendForGrading())
        grade = grader.grade(
            self._make_spec(), self._make_task_config(), self._make_trajectory(), "sys"
        )

        assert isinstance(grade, Grade)
        assert grade.binary_pass is True
        assert grade.score == 1.0
        assert grade.components is not None
        assert grade.components.state_checks == 1.0
