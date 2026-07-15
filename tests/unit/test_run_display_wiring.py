"""Wiring tests for :class:`RunDisplayEvents` propagation.

Locks that the orchestrator, conductor, and runner all fire the seven
Protocol events from the natural sites the plan documents:

- ``Orchestrator.run()`` — ``run_started`` before the pool, ``trial_started``
  in ``submit_one``, ``trial_completed`` / ``trial_failed`` in the wait
  loop, ``run_finished`` before ``return output_dir.resolve()``.
- ``InProcessConductor._grade`` — ``judgment_scored`` right after
  ``trajectory.grade`` is populated.
- ``_AgentMetricsSink.record_generation`` — ``trial_progress`` after
  internal metrics accumulation.

The tests use a small ``_RecordingEvents`` double that appends each call
to a shared list — no assertions on emission order across layers, only
that every emission the plan mandates fires with the documented kwargs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import Metrics
from tolokaforge.core.run_display_events import RunDisplayEvents

pytestmark = pytest.mark.unit


class _RecordingEvents:
    """Test double capturing every :class:`RunDisplayEvents` invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_started(self, **kwargs: Any) -> None:
        self.calls.append(("run_started", kwargs))

    def trial_started(self, **kwargs: Any) -> None:
        self.calls.append(("trial_started", kwargs))

    def trial_progress(self, **kwargs: Any) -> None:
        self.calls.append(("trial_progress", kwargs))

    def trial_completed(self, **kwargs: Any) -> None:
        self.calls.append(("trial_completed", kwargs))

    def trial_failed(self, **kwargs: Any) -> None:
        self.calls.append(("trial_failed", kwargs))

    def judgment_scored(self, **kwargs: Any) -> None:
        self.calls.append(("judgment_scored", kwargs))

    def run_finished(self, **kwargs: Any) -> None:
        self.calls.append(("run_finished", kwargs))

    def phase_changed(self, **kwargs: Any) -> None:
        self.calls.append(("phase_changed", kwargs))

    def trial_provisioned(self, **kwargs: Any) -> None:
        self.calls.append(("trial_provisioned", kwargs))

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_for(self, kind: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == kind]


def test_recording_events_satisfies_protocol() -> None:
    assert isinstance(_RecordingEvents(), RunDisplayEvents)


# ---------------------------------------------------------------------------
# _AgentMetricsSink — fires `trial_progress` from `record_generation`
# ---------------------------------------------------------------------------


def _make_generation_result(
    *, prompt: int = 1000, completion: int = 200, cost: float | None = 0.005
) -> GenerationResult:
    return GenerationResult(
        text="response",
        usage=Usage(prompt_tokens=prompt, completion_tokens=completion),
        cost_usd=cost,
    )


def test_agent_metrics_sink_fires_trial_progress_with_deltas() -> None:
    from tolokaforge.core.runner import _AgentMetricsSink

    events = _RecordingEvents()
    sink = _AgentMetricsSink(Metrics(), events=events, trial_id="taskA:0")

    sink.record_generation(_make_generation_result(prompt=1000, completion=200, cost=0.005))

    assert events.kinds() == ["trial_progress"]
    (kwargs,) = events.kwargs_for("trial_progress")
    assert kwargs == {
        "trial_id": "taskA:0",
        "prompt_tokens_delta": 1000,
        "completion_tokens_delta": 200,
        "cost_delta_usd": 0.005,
    }


def test_agent_metrics_sink_treats_none_cost_as_zero_delta() -> None:
    from tolokaforge.core.runner import _AgentMetricsSink

    events = _RecordingEvents()
    sink = _AgentMetricsSink(Metrics(), events=events, trial_id="taskA:0")

    sink.record_generation(_make_generation_result(cost=None))

    (kwargs,) = events.kwargs_for("trial_progress")
    assert kwargs["cost_delta_usd"] == 0.0


def test_agent_metrics_sink_accumulates_metrics_before_emitting() -> None:
    """Metrics accumulate first; emission is defensive against a raise in the
    display path (though Protocol contract already forbids implementations
    from raising)."""
    from tolokaforge.core.runner import _AgentMetricsSink

    events = _RecordingEvents()
    metrics = Metrics()
    sink = _AgentMetricsSink(metrics, events=events, trial_id="taskA:0")

    sink.record_generation(_make_generation_result(prompt=500, completion=100, cost=0.002))
    sink.record_generation(_make_generation_result(prompt=300, completion=50, cost=0.001))

    assert metrics.api_calls == 2
    assert metrics.usage.prompt_tokens == 800
    assert metrics.usage.completion_tokens == 150
    assert metrics.cost_usd == pytest.approx(0.003)
    assert events.kinds() == ["trial_progress", "trial_progress"]


def test_agent_metrics_sink_default_events_is_null_and_never_raises() -> None:
    """Existing callers that omit ``events`` keep working — the default sink
    silently accepts every call."""
    from tolokaforge.core.runner import _AgentMetricsSink

    sink = _AgentMetricsSink(Metrics())
    sink.record_generation(_make_generation_result())


# ---------------------------------------------------------------------------
# ConductorContext / InProcessConductor threading
# ---------------------------------------------------------------------------


def test_conductor_context_carries_events_field() -> None:
    from tolokaforge.core.conductor import ConductorContext

    field_names = {f.name for f in ConductorContext.__dataclass_fields__.values()}
    assert "events" in field_names


def test_orchestrator_deps_defaults_to_null_events() -> None:
    from tolokaforge.core.orchestrator import OrchestratorDeps
    from tolokaforge.core.run_display_events import _NullRunDisplayEvents

    deps = OrchestratorDeps()
    assert isinstance(deps.events, _NullRunDisplayEvents)


def test_orchestrator_deps_stores_injected_events() -> None:
    from tolokaforge.core.orchestrator import OrchestratorDeps

    events = _RecordingEvents()
    deps = OrchestratorDeps(events=events)
    assert deps.events is events


def test_build_conductor_threads_events_into_context(tmp_path: Path) -> None:
    """The orchestrator's ``events`` field is what ends up on the
    ``ConductorContext`` the factory receives."""
    from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
    from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps

    events = _RecordingEvents()
    captured: dict[str, ConductorContext] = {}

    def factory(ctx: ConductorContext) -> InMemoryConductor:
        captured["ctx"] = ctx
        return InMemoryConductor()

    config = _make_minimal_run_config()
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(events=events, conductor_factory=factory),
    )
    orch.adapter = MagicMock()

    orch._build_conductor(
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        output_dir=tmp_path,
        request_limiter=None,
    )

    assert captured["ctx"].events is events


def test_in_process_conductor_grade_fires_judgment_scored(tmp_path: Path) -> None:
    """``_grade`` fires ``judgment_scored`` right after the grader populates
    ``trajectory.grade``. Locked directly against the phase method with
    every collaborator mocked out — no docker, no LLM."""
    from datetime import UTC, datetime

    from tolokaforge.core.conductor import InProcessConductor, _TrialSetup
    from tolokaforge.core.models import (
        Grade,
        GradeComponents,
        Message,
        Metrics,
        Trajectory,
        TrialStatus,
    )
    from tolokaforge.core.trial import EnvEndpoints, TrialSpec
    from tolokaforge.runner.models import TaskDescription

    events = _RecordingEvents()

    grader = MagicMock()
    grader.grade.return_value = Grade(
        binary_pass=True,
        score=0.75,
        components=GradeComponents(),
        reasons="test",
    )

    conductor = InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=_make_minimal_run_config(),
        logger=MagicMock(),
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        trial_grader=grader,
        output_dir=tmp_path,
        events=events,
    )

    trial_setup = _TrialSetup(
        trial_id="taskA:0",
        trial_idx=0,
        task_dir=tmp_path,
        trial_dir=tmp_path,
        env_state=MagicMock(),
        adapter_env=MagicMock(),
        tool_schemas=[],
        tool_executor=MagicMock(),
    )
    trajectory = Trajectory(
        task_id="taskA",
        trial_index=0,
        start_ts=datetime.now(UTC),
        end_ts=datetime.now(UTC),
        status=TrialStatus.COMPLETED,
        messages=[Message.__class__ for _ in ()],  # type: ignore[list-item]
        metrics=Metrics(),
    )
    trajectory.messages = []
    runner_stub = MagicMock()
    runner_stub.effective_system_prompt = "sys"
    spec = TrialSpec(
        trial_id="taskA:0",
        run_id="run-1",
        attempt_id=0,
        task=TaskDescription(
            task_id="taskA",
            name="taskA",
            category="test",
            description="d",
            adapter_type="native",
            system_prompt="sys",
        ),
        agent_model_config=config_agent_model(),
        env_endpoints=EnvEndpoints(db_url=None, rag_url=None, runner_url="http://x"),
    )
    task_config = MagicMock(task_id="taskA")

    conductor._grade(spec, task_config, trial_setup, trajectory, runner_stub, "sys")

    assert events.kinds() == ["judgment_scored"]
    (kwargs,) = events.kwargs_for("judgment_scored")
    assert kwargs == {"trial_id": "taskA:0", "score": 0.75, "binary_pass": True}


# ---------------------------------------------------------------------------
# Orchestrator.run() — full end-to-end emission
# ---------------------------------------------------------------------------


def config_agent_model():
    from tolokaforge.core.models import ModelConfig

    return ModelConfig(provider="openai", name="gpt-4")


def _make_minimal_run_config(**overrides: Any):
    from tolokaforge.core.models import (
        EvaluationConfig,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
    )

    defaults: dict[str, Any] = {
        "models": {"agent": ModelConfig(provider="openai", name="gpt-4")},
        "orchestrator": OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
        ),
        "evaluation": EvaluationConfig(output_dir="results/test_run"),
    }
    defaults.update(overrides)
    return RunConfig(**defaults)


def _make_task_config(task_id: str):
    from tolokaforge.core.models import (
        InitialStateConfig,
        TaskConfig,
        ToolsConfig,
        UserSimulatorConfig,
    )

    return TaskConfig(
        task_id=task_id,
        name=f"Test Task {task_id}",
        category="tool_use",
        description="A test task",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        user_simulator=UserSimulatorConfig(mode="scripted"),
        grading="grading.yaml",
    )


def _task_description(task_id: str):
    from tolokaforge.runner.models import GradingConfig, TaskDescription

    return TaskDescription(
        task_id=task_id,
        name=task_id,
        category="test",
        description="d",
        adapter_type="native",
        system_prompt="sys",
        grading=GradingConfig(),
    )


def _orchestrator_for_run(
    events: _RecordingEvents,
    task_ids: list[str],
    *,
    conductor_factory: Callable[[Any], Any] | None = None,
    tmp_path: Path,
):
    """Build an Orchestrator wired for a full ``run()`` end-to-end.

    Uses :class:`InMemoryRuntimeBackend` (no docker), :class:`InMemoryConductor`
    (returns synthetic-success trajectory by default), and a mocked adapter
    whose task descriptions carry no ``llm_judge`` so the judge gate is a
    no-op.
    """
    from tolokaforge.core.conductor import InMemoryConductor
    from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
    from tolokaforge.core.runtime import InMemoryRuntimeBackend

    config = _make_minimal_run_config(
        evaluation={"output_dir": str(tmp_path / "results" / "run")},
    )
    factory = conductor_factory or (lambda _ctx: InMemoryConductor())
    runtime = InMemoryRuntimeBackend()

    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            events=events,
            runtime_backend=runtime,
            conductor_factory=factory,
        ),
    )
    orch.tasks = [_make_task_config(tid) for tid in task_ids]
    adapter = MagicMock()
    adapter.to_task_description.side_effect = lambda tid: _task_description(tid)
    adapter.docker_stack_requirements.return_value = MagicMock(needs_rag_service=False)
    orch.adapter = adapter
    return orch, runtime


def test_orchestrator_run_emits_run_started_and_run_finished(tmp_path: Path) -> None:
    events = _RecordingEvents()
    orch, _runtime = _orchestrator_for_run(events, ["taskA"], tmp_path=tmp_path)

    output_dir = orch.run()

    # phase_changed events legitimately fire before run_started (Docker
    # startup visibility). Ordering assertions are on lifecycle events only.
    lifecycle = [k for k in events.kinds() if k != "phase_changed"]
    assert lifecycle[0] == "run_started"
    assert lifecycle[-1] == "run_finished"

    (start_kwargs,) = events.kwargs_for("run_started")
    assert start_kwargs == {"total_trials": 1, "initial_completed": 0}

    (finish_kwargs,) = events.kwargs_for("run_finished")
    assert finish_kwargs == {"output_dir": output_dir}


def test_orchestrator_run_emits_trial_started_and_trial_completed_per_trial(
    tmp_path: Path,
) -> None:
    events = _RecordingEvents()
    orch, _runtime = _orchestrator_for_run(events, ["taskA", "taskB", "taskC"], tmp_path=tmp_path)

    orch.run()

    started_ids = {kw["trial_id"] for kw in events.kwargs_for("trial_started")}
    completed_ids = {kw["trial_id"] for kw in events.kwargs_for("trial_completed")}
    assert started_ids == {"taskA:0", "taskB:0", "taskC:0"}
    assert completed_ids == {"taskA:0", "taskB:0", "taskC:0"}

    for kw in events.kwargs_for("trial_completed"):
        # Default success trajectory carries ``binary_pass=True`` and ``score=1.0``.
        assert kw["binary_pass"] is True
        assert kw["score"] == 1.0


def test_orchestrator_run_fires_trial_failed_when_retries_exhausted(tmp_path: Path) -> None:
    """A trajectory classified retryable that exhausts retries fires
    ``trial_failed(retryable=True, ...)`` — the retry-exhausted branch."""
    from datetime import UTC, datetime

    from tolokaforge.core.conductor import InMemoryConductor
    from tolokaforge.core.models import (
        Metrics,
        TerminationReason,
        Trajectory,
        TrialStatus,
    )

    def error_trajectory(task_id: str, trial_idx: int) -> Trajectory:
        now = datetime.now(UTC)
        return Trajectory(
            task_id=task_id,
            trial_index=trial_idx,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.ERROR,
            termination_reason=TerminationReason.API_ERROR,
            messages=[],
            metrics=Metrics(),
        )

    events = _RecordingEvents()
    orch, _runtime = _orchestrator_for_run(
        events,
        ["taskA"],
        conductor_factory=lambda _ctx: InMemoryConductor(trajectory_factory=error_trajectory),
        tmp_path=tmp_path,
    )

    orch.run()

    failed_kwargs = events.kwargs_for("trial_failed")
    assert len(failed_kwargs) == 1
    assert failed_kwargs[0]["trial_id"] == "taskA:0"
    assert failed_kwargs[0]["retryable"] is True
    assert "API_ERROR" in failed_kwargs[0]["error"] or "api_error" in failed_kwargs[0]["error"]


def test_orchestrator_run_fires_trial_failed_on_hard_exception(tmp_path: Path) -> None:
    """Hard exception in the conductor with retries exhausted fires
    ``trial_failed`` carrying the exception message."""
    from tolokaforge.core.conductor import InMemoryConductor

    def boom_factory(task_id: str, trial_idx: int):
        raise RuntimeError("boom")

    events = _RecordingEvents()
    orch, _runtime = _orchestrator_for_run(
        events,
        ["taskA"],
        conductor_factory=lambda _ctx: InMemoryConductor(trajectory_factory=boom_factory),
        tmp_path=tmp_path,
    )

    orch.run()

    failed_kwargs = events.kwargs_for("trial_failed")
    assert len(failed_kwargs) == 1
    assert failed_kwargs[0]["trial_id"] == "taskA:0"
    assert "boom" in failed_kwargs[0]["error"]


def test_orchestrator_run_events_are_ordered_run_started_first_run_finished_last(
    tmp_path: Path,
) -> None:
    events = _RecordingEvents()
    orch, _runtime = _orchestrator_for_run(events, ["taskA", "taskB"], tmp_path=tmp_path)

    orch.run()

    # phase_changed events legitimately fire before run_started (Docker
    # startup visibility). Ordering assertions are on lifecycle events only.
    lifecycle = [k for k in events.kinds() if k != "phase_changed"]
    assert lifecycle[0] == "run_started"
    assert lifecycle[-1] == "run_finished"
    # trial_started + trial_completed for each trial land between the two.
    assert lifecycle.count("trial_started") == 2
    assert lifecycle.count("trial_completed") == 2
