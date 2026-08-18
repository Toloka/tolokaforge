"""An attempt whose grading refuses is counted, bundled, and not retried.

``RunnerRPCTrialGrader.grade`` raises :class:`GradingFailedError` when
``GradeTrial`` returns no verdict. The conductor's grading phase records the
reason on ``Trajectory.grading_error`` and lets the trial through its normal
path, so the attempt reaches ``total_trials``, keeps the ``termination_reason``
it really ended with, and leaves the cause recoverable from its bundle.

Every refusal here is produced, not typed: the in-process runner servicer
records two tool calls under one ``call_id``, which needs no live provider and
which the trial's event timeline cannot join.

``InProcessConductor.run()`` is not drivable without a real environment — it
dies inside ``EnvironmentState.hydrate()`` — so the composition tests reach the
production grading and artifact-write phases through the declared
:class:`~tolokaforge.core.conductor.Conductor` seam. What ``run()`` itself
sequences, ``_grade`` then ``_write_artifacts``, belongs to the integration
lane and is not claimed here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

pytestmark = pytest.mark.unit

from tests.canonical._factories import make_task_config, make_trial_spec
from tests.utils.conductor_phases import (
    make_conductor,
    make_run_config,
    make_setup,
    runner_stub,
)
from tests.utils.runner_requests import simple_task_description as make_task_description_dict
from tests.utils.servicer_runtime import (
    DUPLICATE_CALL_ID,
    ServicerBackend,
    collided_trajectory,
    produce_grading_refusal,
    register_collided_trial,
)
from tolokaforge.core.conductor import ConductorContext, InProcessConductor
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Metrics,
    TaskConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.trial import TrialResult, TrialSpec
from tolokaforge.core.trial_grader import GradingFailedError, RunnerRPCTrialGrader
from tolokaforge.runner.models import RunnerGradingConfig, TaskDescription

_TASK_IDS = count()


# ---------------------------------------------------------------------------
# Doubles and builders
# ---------------------------------------------------------------------------


class _RecordingEvents:
    """Records every display emission as ``(kind, kwargs)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, kind: str) -> Any:
        def record(**kwargs: Any) -> None:
            self.calls.append((kind, kwargs))

        return record

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


class _RefuseOneTrialGrader:
    """Grades every trial but ``refusing_index``, which gets *refusal*.

    *refusal* is the message a real ``GradeTrial`` refusal carried, captured
    once from the servicer — the string is produced; which trial it lands on is
    the scenario.
    """

    def __init__(self, refusal: str, *, refusing_index: int) -> None:
        self._refusal = refusal
        self._refusing_index = refusing_index

    def grade(
        self, spec: TrialSpec, trajectory: Trajectory, agent_system_prompt: str
    ) -> Grade | None:
        if trajectory.trial_index == self._refusing_index:
            raise GradingFailedError(self._refusal)
        return Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(state_checks=1.0),
            reasons="state checks passed",
        )


def _make_task_description(task_id: str) -> TaskDescription:
    return TaskDescription(
        task_id=task_id,
        name=task_id,
        category="test",
        description="d",
        adapter_type="native",
        system_prompt="sys",
        grading=RunnerGradingConfig(llm_judge=None),
    )


# ---------------------------------------------------------------------------
# The real refusal
# ---------------------------------------------------------------------------


@pytest.fixture
def collided_task_id() -> str:
    """A task id no earlier test in this module registered.

    The in-process DB service keeps every registered trial for the life of the
    process, so a shared id makes the second registration fail.
    """
    return f"ungradeable_{next(_TASK_IDS)}"


@pytest.fixture
def refusing_grader(runner_service, mock_grpc_context, collided_task_id: str):
    """A real :class:`RunnerRPCTrialGrader` over a servicer that will refuse."""
    register_collided_trial(
        runner_service,
        mock_grpc_context,
        make_task_description_dict(),
        trial_id=f"{collided_task_id}:0",
    )
    return RunnerRPCTrialGrader(
        runtime_backend=ServicerBackend(runner_service, mock_grpc_context),
        logger=StructuredLogger("test-ungradeable-grader"),
    )


@pytest.fixture
def real_refusal(runner_service, mock_grpc_context) -> str:
    """The message a real ``GradeTrial`` refusal carries, produced not typed."""
    return produce_grading_refusal(runner_service, mock_grpc_context)


class TestGradingRefusalIsRecordedNotRaised:
    def test_the_phase_returns_with_the_cause_and_no_verdict(
        self, refusing_grader, collided_task_id: str, tmp_path: Path
    ) -> None:
        conductor = make_conductor(make_run_config(tmp_path / "results"), tmp_path, refusing_grader)
        conductor.events = events = _RecordingEvents()
        trajectory = collided_trajectory(task_id=collided_task_id)

        conductor._grade(
            make_trial_spec(trial_id=f"{collided_task_id}:0", task_id=collided_task_id),
            make_task_config(collided_task_id),
            make_setup(tmp_path, collided_task_id, 0),
            trajectory,
            runner_stub(),
            "You are a test assistant.",
        )

        assert trajectory.grade is None
        assert trajectory.grading_error is not None
        assert DUPLICATE_CALL_ID in trajectory.grading_error
        # The trial really did end this way; grading's failure does not rewrite it.
        assert trajectory.termination_reason is TerminationReason.AGENT_DONE
        assert trajectory.status is TrialStatus.COMPLETED
        assert "judgment_scored" not in events.kinds()

    def test_a_verdict_still_reaches_the_same_recorder(self, tmp_path: Path) -> None:
        """The emptiness assertion above is not vacuous — this wiring does emit."""
        conductor = make_conductor(
            make_run_config(tmp_path / "results"),
            tmp_path,
            _RefuseOneTrialGrader("unused", refusing_index=99),
        )
        conductor.events = events = _RecordingEvents()

        conductor._grade(
            make_trial_spec(trial_id="graded:0", task_id="graded"),
            make_task_config("graded"),
            make_setup(tmp_path, "graded", 0),
            collided_trajectory(task_id="graded"),
            runner_stub(),
            "sys",
        )

        assert "judgment_scored" in events.kinds()

    def test_the_bundle_carries_the_cause_and_no_grade_file(
        self, refusing_grader, collided_task_id: str, tmp_path: Path
    ) -> None:
        conductor = make_conductor(make_run_config(tmp_path / "results"), tmp_path, refusing_grader)
        setup = make_setup(tmp_path, collided_task_id, 0)
        task_config = make_task_config(collided_task_id)
        trajectory = collided_trajectory(task_id=collided_task_id)
        spec = make_trial_spec(trial_id=f"{collided_task_id}:0", task_id=collided_task_id)
        runner = runner_stub()

        conductor._grade(spec, task_config, setup, trajectory, runner, "sys")
        conductor._write_artifacts(spec, task_config, setup, trajectory, runner)

        written = yaml.safe_load((setup.trial_dir / "trajectory.yaml").read_text())
        assert DUPLICATE_CALL_ID in written["grading_error"]
        assert not (setup.trial_dir / "grade.yaml").exists()


# ---------------------------------------------------------------------------
# Composition — a real Orchestrator.run() over the production phases
# ---------------------------------------------------------------------------


class _GradingPhaseConductor:
    """A :class:`Conductor` that runs the production grading and artifact-write
    phases against a trajectory it builds itself.

    Everything the behaviour under test lives in stays production code: the
    :class:`InProcessConductor` instance the orchestrator's own
    :class:`ConductorContext` builds, its ``_grade`` and ``_write_artifacts``,
    and the orchestrator's real
    :class:`~tolokaforge.core.output.artifacts.FileArtifactWriter`. Only the
    phases that need a real environment — setup, agent loop, final-state
    capture — are skipped.
    """

    def __init__(self, ctx: ConductorContext, grader: Any) -> None:
        self._conductor = InProcessConductor(**{**vars(ctx), "trial_grader": grader})
        self._output_dir = ctx.output_dir

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        trial_idx = int(spec.trial_id.rsplit(":", 1)[1])
        now = datetime.now(UTC)
        trajectory = Trajectory(
            task_id=task_config.task_id,
            trial_index=trial_idx,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.AGENT_DONE,
            messages=[],
            metrics=Metrics(),
        )
        setup = make_setup(self._output_dir, task_config.task_id, trial_idx)
        runner = runner_stub()
        self._conductor._grade(spec, task_config, setup, trajectory, runner, "sys")
        self._conductor._write_artifacts(spec, task_config, setup, trajectory, runner)
        return TrialResult.from_trajectory(
            trial_id=spec.trial_id, trajectory=trajectory, worker_id=spec.worker_id
        )


def _run_with_one_refusal(
    tmp_path: Path,
    refusal: str,
    *,
    repeats: int,
    events: _RecordingEvents | None = None,
) -> Path:
    """Drive a real ``Orchestrator.run()`` in which trial 1's grading refuses."""
    run_root = tmp_path / "results" / "run_base"
    run_root.parent.mkdir(parents=True, exist_ok=True)

    grader = _RefuseOneTrialGrader(refusal, refusing_index=1)
    orch = Orchestrator(
        make_run_config(run_root, repeats=repeats),
        deps=OrchestratorDeps(
            events=events or _RecordingEvents(),
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda ctx: _GradingPhaseConductor(ctx, grader),
        ),
    )
    orch.tasks = [make_task_config("TASK-A")]

    adapter = MagicMock()
    adapter.to_task_description.side_effect = _make_task_description
    adapter.docker_stack_requirements.return_value = None
    adapter.trial_grader_name = "runner_rpc"
    adapter.get_grading_config.return_value = None
    adapter.fingerprint.return_value = None
    orch.adapter = adapter

    return orch.run()


class TestTheRunCountsTheUngradeableAttempt:
    """The issue's acceptance oracle: one task, two trials, one refusal."""

    def test_both_trials_reach_the_metrics_and_the_bundle_lands(
        self, real_refusal: str, tmp_path: Path
    ) -> None:
        run_dir = _run_with_one_refusal(tmp_path, real_refusal, repeats=2)

        per_task = json.loads((run_dir / "per_task_metrics.json").read_text())
        assert len(per_task) == 1
        metrics = per_task[0]
        assert metrics["total_trials"] == 2
        assert metrics["measured_trials"] == 2
        assert metrics["scored_trials"] == 1
        assert metrics["avg_score"] == 1.0
        assert metrics["success_rate"] == 0.5
        assert metrics["pass@1"] == 0.5

        # Counted as ours, and its own row: the graded trial terminated
        # ``agent_done`` too, so a reason-keyed row would have swallowed it.
        assert metrics["ungradeable"] == 1
        assert metrics["outcomes_by_reason"] == {
            "agent_done": {"class": "measured", "count": 1},
            "ungradeable_agent_done": {"class": "ungradeable", "count": 1},
        }

        bundle = run_dir / "trials" / "TASK-A" / "1" / "trajectory.yaml"
        assert DUPLICATE_CALL_ID in yaml.safe_load(bundle.read_text())["grading_error"]

    def test_the_attempt_is_not_retried_and_counts_once(
        self, real_refusal: str, tmp_path: Path
    ) -> None:
        run_dir = _run_with_one_refusal(tmp_path, real_refusal, repeats=2)

        per_task = json.loads((run_dir / "per_task_metrics.json").read_text())
        assert per_task[0]["total_trials"] == 2

        run_state = json.loads((run_dir / "run_state.json").read_text())
        assert run_state["trials"]["TASK-A:1"]["status"] == "completed"
        assert run_state["failed_trials"] == 0

    def test_the_completion_event_carries_no_verdict(
        self, real_refusal: str, tmp_path: Path
    ) -> None:
        """The live display is told the trial was not graded, not that it failed."""
        events = _RecordingEvents()
        _run_with_one_refusal(tmp_path, real_refusal, repeats=2, events=events)

        completions = {
            kwargs["trial_id"]: kwargs for kind, kwargs in events.calls if kind == "trial_completed"
        }
        assert completions.keys() == {"TASK-A:0", "TASK-A:1"}
        # The graded trial's verdict still arrives, so the None below is the
        # ungradeable trial's and not a recorder that drops every kwarg.
        assert completions["TASK-A:0"]["binary_pass"] is True
        assert completions["TASK-A:0"]["score"] == 1.0
        assert completions["TASK-A:1"]["binary_pass"] is None
        assert completions["TASK-A:1"]["score"] is None


# ---------------------------------------------------------------------------
# The deserialisation boundary
# ---------------------------------------------------------------------------


def _trajectory_kwargs() -> dict[str, Any]:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    return {
        "task_id": "ungradeable-validator",
        "trial_index": 0,
        "start_ts": now,
        "end_ts": now,
        "status": TrialStatus.COMPLETED,
        "messages": [],
    }


def _a_grade() -> Grade:
    return Grade(
        binary_pass=True,
        score=1.0,
        components=GradeComponents(state_checks=1.0),
        reasons="state checks passed",
    )


class TestATrajectoryIsGradedOrUngradeableNotBoth:
    def test_construction_rejects_both(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            Trajectory(**_trajectory_kwargs(), grade=_a_grade(), grading_error="refused")

        message = str(excinfo.value)
        assert "grading_error" in message
        assert "grade" in message

    def test_model_validate_rejects_a_dict_carrying_both(self) -> None:
        payload = Trajectory(**_trajectory_kwargs(), grade=_a_grade()).model_dump(mode="json")
        payload["grading_error"] = "refused"

        with pytest.raises(ValueError, match="grading_error"):
            Trajectory.model_validate(payload)

    @pytest.mark.parametrize(
        ("extra", "expected_error", "expected_grade"),
        [
            ({"grade": _a_grade()}, None, True),
            ({"grading_error": "refused"}, "refused", False),
            ({}, None, False),
        ],
        ids=["graded", "ungradeable", "neither"],
    )
    def test_each_field_alone_validates(
        self, extra: dict[str, Any], expected_error: str | None, expected_grade: bool
    ) -> None:
        trajectory = Trajectory(**_trajectory_kwargs(), **extra)

        assert trajectory.grading_error == expected_error
        assert (trajectory.grade is not None) is expected_grade
        assert Trajectory.model_validate(trajectory.model_dump(mode="json")) == trajectory
