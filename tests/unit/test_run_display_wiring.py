"""Wiring tests for :class:`RunDisplayEvents` propagation.

Locks that every engine surface — per-trial (``_AgentMetricsSink``,
``InProcessConductor._grade``, ``ProvisioningTrialExecutor.execute``)
and run-level (``Orchestrator``) — fires the RunDisplayEvents seam
events it owns:

- ``_AgentMetricsSink.record_generation`` fires ``trial_progress`` after
  internal metrics accumulation.
- ``InProcessConductor._grade`` fires ``judgment_scored`` right after
  ``trajectory.grade`` is populated.
- ``ProvisioningTrialExecutor.execute`` fires ``trial_provisioned``
  after ``runtime_backend.await_ready(handle)`` succeeds.
- ``Orchestrator`` threads the injected sink into the conductor and
  trial-executor seams, fires ``phase_changed`` / ``run_started`` /
  ``trial_started`` / ``trial_completed`` / ``trial_failed`` /
  ``run_finished``, and — critically — populates
  ``_total_index_by_key`` inside ``_build_pending_trials`` so
  ``trial_started`` emissions carry distinct run-wide indices rather
  than a silent ``0`` from the ``.get(..., 0)`` fallback.

The tests use a small ``_RecordingEvents`` double that appends each call
to a shared list so kwargs and call kinds can be asserted directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import (
    make_env_endpoints,
    make_task_config,
    make_trial_spec,
)
from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import Metrics
from tolokaforge.core.output.artifacts import InMemoryArtifactWriter
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

    def llm_call_started(self, **kwargs: Any) -> None:
        self.calls.append(("llm_call_started", kwargs))

    def llm_call_finished(self, **kwargs: Any) -> None:
        self.calls.append(("llm_call_finished", kwargs))

    def llm_retry_scheduled(self, **kwargs: Any) -> None:
        self.calls.append(("llm_retry_scheduled", kwargs))

    def component_registered(self, **kwargs: Any) -> None:
        self.calls.append(("component_registered", kwargs))

    def component_status_changed(self, **kwargs: Any) -> None:
        self.calls.append(("component_status_changed", kwargs))

    def component_log_appended(self, **kwargs: Any) -> None:
        self.calls.append(("component_log_appended", kwargs))

    def component_unregistered(self, **kwargs: Any) -> None:
        self.calls.append(("component_unregistered", kwargs))

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
    """Metrics accumulate first; two record_generation calls yield two
    ``trial_progress`` emissions with the per-call deltas."""
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


def test_conductor_context_events_defaults_to_null_sink() -> None:
    from dataclasses import fields as dataclass_fields

    from tolokaforge.core.conductor import ConductorContext
    from tolokaforge.core.run_display_events import _NullRunDisplayEvents

    events_field = next(f for f in dataclass_fields(ConductorContext) if f.name == "events")
    default_value = events_field.default_factory()
    assert isinstance(default_value, _NullRunDisplayEvents)


def test_in_process_conductor_grade_fires_judgment_scored(tmp_path: Path) -> None:
    """``_grade`` fires ``judgment_scored`` right after the grader populates
    ``trajectory.grade``. Locked directly against the phase method with
    every collaborator mocked out — no docker, no LLM."""
    from datetime import UTC, datetime

    from tolokaforge.core.conductor import InProcessConductor, _TrialSetup
    from tolokaforge.core.models import (
        EvaluationConfig,
        Grade,
        GradeComponents,
        Metrics,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
        Trajectory,
        TrialStatus,
    )

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
        config=RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir=str(tmp_path / "results" / "run")),
        ),
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
        messages=[],
        metrics=Metrics(),
    )
    runner_stub = MagicMock()
    runner_stub.effective_system_prompt = "sys"

    spec = make_trial_spec(trial_id="taskA:0", task_id="taskA")
    task_config = MagicMock(task_id="taskA")

    conductor._grade(spec, task_config, trial_setup, trajectory, runner_stub, "sys")

    assert events.kinds() == ["judgment_scored"]
    (kwargs,) = events.kwargs_for("judgment_scored")
    assert kwargs == {"trial_id": "taskA:0", "score": 0.75, "binary_pass": True}


def test_in_process_conductor_default_events_is_null_sink(tmp_path: Path) -> None:
    """When constructed without an ``events`` kwarg, the conductor holds the
    null sink so callers that skip the wiring keep working."""
    from tolokaforge.core.conductor import InProcessConductor
    from tolokaforge.core.models import (
        EvaluationConfig,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
    )
    from tolokaforge.core.run_display_events import _NullRunDisplayEvents

    conductor = InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir=str(tmp_path / "results" / "run")),
        ),
        logger=MagicMock(),
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        trial_grader=MagicMock(),
        output_dir=tmp_path,
    )
    assert isinstance(conductor.events, _NullRunDisplayEvents)


# ---------------------------------------------------------------------------
# ProvisioningTrialExecutor — fires `trial_provisioned` post-await_ready
# ---------------------------------------------------------------------------


def test_provisioning_trial_executor_fires_trial_provisioned_after_await_ready() -> None:
    """After ``endpoints(handle)`` returns, the executor emits
    ``trial_provisioned`` with the runtime's infrastructure snapshot and
    an endpoints map assembled from the resolved :class:`EnvEndpoints`."""
    from tolokaforge.core.conductor import InMemoryConductor
    from tolokaforge.core.runtime import InMemoryRuntimeBackend
    from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

    events = _RecordingEvents()
    backend = InMemoryRuntimeBackend()
    conductor = InMemoryConductor()
    executor = ProvisioningTrialExecutor(
        runtime_backend=backend,
        conductor=conductor,
        logger=MagicMock(),
        output_dir=Path("/nonexistent-run-dir"),
        artifact_writer=InMemoryArtifactWriter(),
        events=events,
    )

    spec = make_trial_spec(
        trial_id="task-1:0",
        env_endpoints=make_env_endpoints(
            db_url="http://db.local:8000", runner_url="http://runner.local:50051"
        ),
    )
    executor.execute(spec, make_task_config())

    provisioned = events.kwargs_for("trial_provisioned")
    assert len(provisioned) == 1
    kwargs = provisioned[0]
    assert kwargs["trial_id"] == "task-1:0"
    # InMemoryRuntimeBackend synthesises one container row keyed on trial_id.
    assert isinstance(kwargs["containers"], list)
    assert len(kwargs["containers"]) == 1
    # The endpoints map is post-provisioning (from InMemoryRuntimeBackend);
    # runner + db keys always appear because that backend fills both fields.
    endpoints_map = kwargs["endpoints"]
    assert set(endpoints_map.keys()) >= {"runner", "db"}
    for url in endpoints_map.values():
        assert isinstance(url, str) and url


def test_provisioning_trial_executor_default_events_is_silent() -> None:
    """The default null-events sink does not emit and does not raise."""
    from tolokaforge.core.conductor import InMemoryConductor
    from tolokaforge.core.runtime import InMemoryRuntimeBackend
    from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

    backend = InMemoryRuntimeBackend()
    executor = ProvisioningTrialExecutor(
        runtime_backend=backend,
        conductor=InMemoryConductor(),
        logger=MagicMock(),
        output_dir=Path("/nonexistent-run-dir"),
        artifact_writer=InMemoryArtifactWriter(),
    )
    executor.execute(make_trial_spec(), make_task_config())


def test_provisioning_trial_executor_skips_trial_provisioned_on_provision_error() -> None:
    """When provisioning fails before ``endpoints(handle)``, no
    ``trial_provisioned`` fires — the display never sees a half-live trial."""
    from tolokaforge.core.conductor import InMemoryConductor
    from tolokaforge.core.runtime import InMemoryRuntimeBackend
    from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

    events = _RecordingEvents()
    backend = InMemoryRuntimeBackend(await_ready_times_out=True)
    executor = ProvisioningTrialExecutor(
        runtime_backend=backend,
        conductor=InMemoryConductor(),
        logger=MagicMock(),
        output_dir=Path("/nonexistent-run-dir"),
        artifact_writer=InMemoryArtifactWriter(),
        events=events,
    )
    executor.execute(make_trial_spec(), make_task_config())
    assert events.kwargs_for("trial_provisioned") == []


# ---------------------------------------------------------------------------
# _endpoints_to_map helper
# ---------------------------------------------------------------------------


def test_endpoints_to_map_projects_present_urls() -> None:
    from tolokaforge.core.trial import EnvEndpoints
    from tolokaforge.core.trial_executor import _endpoints_to_map

    mapping = _endpoints_to_map(
        EnvEndpoints(
            runner_url="http://r:50051",
            db_url="http://d:8000",
            rag_url="http://rag:9000",
        )
    )
    assert mapping == {
        "runner": "http://r:50051",
        "db": "http://d:8000",
        "rag": "http://rag:9000",
    }


def test_endpoints_to_map_omits_absent_urls() -> None:
    from tolokaforge.core.trial import EnvEndpoints
    from tolokaforge.core.trial_executor import _endpoints_to_map

    mapping = _endpoints_to_map(
        EnvEndpoints(runner_url="http://r:50051", db_url=None, rag_url=None)
    )
    assert mapping == {"runner": "http://r:50051"}


# ---------------------------------------------------------------------------
# OrchestratorDeps — carries the events sink; defaults to _NullRunDisplayEvents
# ---------------------------------------------------------------------------


def test_orchestrator_deps_carries_events_field() -> None:
    from tolokaforge.core.orchestrator import OrchestratorDeps

    field_names = {f.name for f in OrchestratorDeps.__dataclass_fields__.values()}
    assert "events" in field_names


def test_orchestrator_deps_events_defaults_to_null_sink() -> None:
    from dataclasses import fields as dataclass_fields

    from tolokaforge.core.orchestrator import OrchestratorDeps
    from tolokaforge.core.run_display_events import _NullRunDisplayEvents

    events_field = next(f for f in dataclass_fields(OrchestratorDeps) if f.name == "events")
    default_value = events_field.default_factory()
    assert isinstance(default_value, _NullRunDisplayEvents)


def test_orchestrator_stores_events_from_deps() -> None:
    """The injected events sink is captured on ``self._events`` verbatim
    — the orchestrator never wraps or replaces it."""
    from tolokaforge.core.models import (
        EvaluationConfig,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
    )
    from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps

    events = _RecordingEvents()
    config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test-events-wiring"),
    )
    orch = Orchestrator(config, deps=OrchestratorDeps(events=events))
    assert orch._events is events
    assert orch._total_index_by_key == {}


# ---------------------------------------------------------------------------
# _build_pending_trials — populates _total_index_by_key with distinct indices
# ---------------------------------------------------------------------------


def _make_orchestrator_with_tasks(task_ids: list[str], repeats: int, shuffle: bool = False) -> Any:
    """Build a bare :class:`Orchestrator` for pending-trial construction tests."""
    from tolokaforge.core.models import (
        ActorSpec,
        EvaluationConfig,
        InitialStateConfig,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
        TaskConfig,
        ToolsConfig,
    )
    from tolokaforge.core.orchestrator import Orchestrator

    config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=repeats,
            auto_start_services=False,
            shuffle_trials=shuffle,
        ),
        evaluation=EvaluationConfig(output_dir="/tmp/pending-trials-test"),
    )
    orch = Orchestrator(config)
    tasks = [
        TaskConfig(
            task_id=task_id,
            name=task_id,
            category="tool_use",
            description="d",
            initial_state=InitialStateConfig(),
            tools=ToolsConfig(),
            actors={"user": ActorSpec(mode="scripted")},
            grading="grading.yaml",
        )
        for task_id in task_ids
    ]
    return orch, tasks


def test_build_pending_trials_populates_total_index_by_key_with_distinct_values() -> None:
    """The dict must map every ``(task_id, trial_idx)`` pair to a unique
    run-wide index 0..N-1. This is the guardrail against the
    ``trial_started`` emission's ``.get(..., 0)`` fallback silently
    reporting every trial as ``total_index=0``."""
    orch, tasks = _make_orchestrator_with_tasks(["A", "B", "C"], repeats=1)

    orch._build_pending_trials(tasks, repeats=1)

    assert orch._total_index_by_key == {
        ("A", 0): 0,
        ("B", 0): 1,
        ("C", 0): 2,
    }


def test_build_pending_trials_indices_span_full_range_for_multi_repeat() -> None:
    """Six trials (3 tasks × 2 repeats) must map to indices 0..5 with no
    collisions — mirrors the multi-repeat case where the orchestrator
    fans a task out into ``repeats`` per-trial entries."""
    orch, tasks = _make_orchestrator_with_tasks(["A", "B", "C"], repeats=2)

    orch._build_pending_trials(tasks, repeats=2)

    values = sorted(orch._total_index_by_key.values())
    assert values == [0, 1, 2, 3, 4, 5]
    assert set(orch._total_index_by_key.keys()) == {
        ("A", 0),
        ("A", 1),
        ("B", 0),
        ("B", 1),
        ("C", 0),
        ("C", 1),
    }


def test_build_pending_trials_indices_are_distinct_under_shuffle() -> None:
    """Even under ``shuffle_trials``, every key maps to a distinct index
    0..N-1 — the dict is populated from the shuffled order, so lookup
    always resolves to the correct run-wide slot."""
    import random

    orch, tasks = _make_orchestrator_with_tasks(
        [f"T{i}" for i in range(5)], repeats=1, shuffle=True
    )
    random.seed(0)

    orch._build_pending_trials(tasks, repeats=1)

    values = sorted(orch._total_index_by_key.values())
    assert values == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# _declared_engine_service_snapshots / _engine_service_snapshots helpers
# ---------------------------------------------------------------------------


def test_declared_engine_service_snapshots_returns_created_rows() -> None:
    """Fires on ``phase_changed(starting_services)`` before any container is
    up — every declared service ships as ``status="created"`` with empty
    ports so the widget can render the row before docker responds."""
    from tolokaforge.core.orchestrator import _declared_engine_service_snapshots

    stack = MagicMock()
    stack.services = {"runner": object(), "db-service": object()}

    snapshots = _declared_engine_service_snapshots(stack)

    assert snapshots == [
        {"name": "runner", "status": "created", "ports": {}, "role": "engine"},
        {"name": "db-service", "status": "created", "ports": {}, "role": "engine"},
    ]


def test_engine_service_snapshots_prefers_health_over_status() -> None:
    """When a service reports a real health probe verdict, that value
    wins over the raw lifecycle string — a ``running`` container whose
    probe is still starting must not read as ready."""
    from tolokaforge.core.orchestrator import _engine_service_snapshots

    stack = MagicMock()
    runner_status = MagicMock(status="running", health="healthy", ports={50051: 50052})
    db_status = MagicMock(status="running", health="starting", ports={5432: 55432})
    stack.get_status.return_value = {"runner": runner_status, "db-service": db_status}

    snapshots = _engine_service_snapshots(stack)

    assert snapshots == [
        {"name": "runner", "status": "healthy", "ports": {50051: 50052}, "role": "engine"},
        {"name": "db-service", "status": "starting", "ports": {5432: 55432}, "role": "engine"},
    ]


def test_engine_service_snapshots_falls_back_to_status_when_health_absent() -> None:
    """No health probe → ``health`` is ``"unknown"`` on the underlying
    :class:`ServiceStatus`. The snapshot falls back to the container
    ``status`` so the widget still reports a meaningful value."""
    from tolokaforge.core.orchestrator import _engine_service_snapshots

    stack = MagicMock()
    stack.get_status.return_value = {
        "runner": MagicMock(status="running", health="unknown", ports={}),
    }

    (snapshot,) = _engine_service_snapshots(stack)

    assert snapshot["status"] == "running"


# ---------------------------------------------------------------------------
# _build_conductor / _build_trial_executor thread events through
# ---------------------------------------------------------------------------


def _build_orch_for_seam_threading(events: Any) -> Any:
    from tolokaforge.core.models import (
        EvaluationConfig,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
    )
    from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps

    config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/seam-thread-test"),
    )
    orch = Orchestrator(config, deps=OrchestratorDeps(events=events))
    orch.adapter = MagicMock()
    return orch


def test_build_conductor_threads_events_into_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_build_conductor`` writes the orchestrator's ``self._events`` onto
    :class:`ConductorContext.events` — the conductor factory sees the
    same sink the orchestrator holds."""
    from tolokaforge.core.conductor import ConductorContext
    from tolokaforge.core.trial_grader import runner_rpc_trial_grader_factory

    # Grader resolves through the registry; bind the built-in factory directly
    # so the test needs no installed entry-point metadata.
    monkeypatch.setattr(
        "tolokaforge.core.orchestrator.load_trial_grader",
        lambda name: runner_rpc_trial_grader_factory,
    )

    events = _RecordingEvents()
    orch = _build_orch_for_seam_threading(events)

    captured_ctx: list[ConductorContext] = []

    def capture_factory(ctx: ConductorContext) -> Any:
        captured_ctx.append(ctx)
        return MagicMock()

    orch._conductor_factory = capture_factory
    orch._build_conductor(
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        output_dir=tmp_path,
        request_limiter=None,
    )

    assert len(captured_ctx) == 1
    assert captured_ctx[0].events is events


def test_build_trial_executor_threads_events_into_provisioning_executor() -> None:
    """``_build_trial_executor`` constructs a :class:`ProvisioningTrialExecutor`
    whose ``events`` attribute is the same sink threaded through the
    orchestrator's deps — no rewrapping, no silent null fallback."""
    events = _RecordingEvents()
    orch = _build_orch_for_seam_threading(events)

    executor = orch._build_trial_executor(
        runtime_backend=MagicMock(),
        conductor=MagicMock(),
        output_dir=Path("/nonexistent-run-dir"),
    )
    assert executor.events is events


# ---------------------------------------------------------------------------
# Full-run emission ordering + distinct trial_started total_index values
# ---------------------------------------------------------------------------


def _make_task_for_run(task_id: str) -> Any:
    from tolokaforge.core.models import (
        ActorSpec,
        InitialStateConfig,
        TaskConfig,
        ToolsConfig,
    )

    return TaskConfig(
        task_id=task_id,
        name=task_id,
        category="tool_use",
        description="d",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        actors={"user": ActorSpec(mode="scripted")},
        grading="grading.yaml",
    )


def _make_task_description_for_run(task_id: str) -> Any:
    from tolokaforge.runner.models import RunnerGradingConfig, TaskDescription

    return TaskDescription(
        task_id=task_id,
        name=task_id,
        category="test",
        description="d",
        adapter_type="native",
        system_prompt="sys",
        grading=RunnerGradingConfig(llm_judge=None),
    )


def _adapter_for_run(task_dir: Path) -> Any:
    """The adapter seam a full ``run()`` reads, over a pack with no grading file.

    *task_dir* is a real directory: the run's pre-flight resolves each task's
    grading file under it and has nothing to check.
    """
    adapter = MagicMock()
    adapter.to_task_description.side_effect = _make_task_description_for_run
    adapter.docker_stack_requirements.return_value = None
    adapter.trial_grader_name = "runner_rpc"
    adapter.get_task_dir.return_value = task_dir
    return adapter


def test_run_emits_lifecycle_with_distinct_trial_started_total_indices(tmp_path: Path) -> None:
    """Drive a full ``Orchestrator.run()`` with 3 trials against an
    in-memory conductor and runtime backend.

    Locks two invariants:

    1. Every lifecycle emission fires at least once — ``phase_changed``
       for the ``starting_services`` skip path (auto_start_services=False
       so only ``connecting_runtime`` fires), ``run_started``,
       ``trial_started`` × 3, ``trial_completed`` × 3, and
       ``run_finished``.
    2. The three ``trial_started`` emissions carry **distinct**
       ``total_index`` values ``{0, 1, 2}`` — proving the
       ``.get(..., 0)`` fallback in the emission site is never hit
       silently across a multi-trial run.
    """
    from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
    from tolokaforge.core.models import (
        EvaluationConfig,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
    )
    from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
    from tolokaforge.core.runtime import InMemoryRuntimeBackend

    events = _RecordingEvents()

    run_root = tmp_path / "results" / "run_base"
    run_root.parent.mkdir(parents=True, exist_ok=True)
    config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
            shuffle_trials=False,
        ),
        evaluation=EvaluationConfig(output_dir=str(run_root)),
    )

    def make_conductor(_ctx: ConductorContext) -> InMemoryConductor:
        return InMemoryConductor()

    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            events=events,
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=make_conductor,
        ),
    )
    orch.tasks = [_make_task_for_run(tid) for tid in ("TASK-A", "TASK-B", "TASK-C")]

    orch.adapter = _adapter_for_run(tmp_path)

    orch.run()

    # Every lifecycle emission fires — run_started once, 3× trial_started,
    # 3× trial_completed, run_finished once, and connecting_runtime phase.
    kinds = events.kinds()
    assert kinds.count("run_started") == 1
    assert kinds.count("trial_started") == 3
    assert kinds.count("trial_completed") == 3
    assert kinds.count("run_finished") == 1
    assert kinds.count("trial_failed") == 0
    phase_calls = events.kwargs_for("phase_changed")
    phases = [call["phase"] for call in phase_calls]
    assert "connecting_runtime" in phases

    # The critic-required invariant: distinct total_index values across the
    # three trial_started emissions. If ``_total_index_by_key`` were not
    # populated, ``.get(..., 0)`` would emit ``0`` for every trial.
    trial_started = events.kwargs_for("trial_started")
    total_indices = [call["total_index"] for call in trial_started]
    assert sorted(total_indices) == [0, 1, 2]
    assert len(set(total_indices)) == 3

    # run_finished carries the resolved output directory the reports were
    # written into.
    (run_finished,) = events.kwargs_for("run_finished")
    assert run_finished["output_dir"].exists()


def test_run_with_default_null_events_completes_without_raising(tmp_path: Path) -> None:
    """The default sink is :class:`_NullRunDisplayEvents` — a complete run
    against it must never raise a ``TypeError`` from a mis-kwarged
    emission. Guards the null-default across every RunDisplayEvents
    method the engine calls from ``Orchestrator.run()``."""
    from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
    from tolokaforge.core.models import (
        EvaluationConfig,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
    )
    from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
    from tolokaforge.core.runtime import InMemoryRuntimeBackend

    run_root = tmp_path / "results" / "null_base"
    run_root.parent.mkdir(parents=True, exist_ok=True)
    config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(run_root)),
    )

    def make_conductor(_ctx: ConductorContext) -> InMemoryConductor:
        return InMemoryConductor()

    # No `events=...` — the default :class:`_NullRunDisplayEvents` sink applies.
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=make_conductor,
        ),
    )
    orch.tasks = [_make_task_for_run("TASK-A")]

    orch.adapter = _adapter_for_run(tmp_path)

    orch.run()


# ---------------------------------------------------------------------------
# Call-observation threading + trial_started model identity
# ---------------------------------------------------------------------------


class _ObservationCapturingClient:
    """Loop-LLM double that records every ``generate`` kwargs dict.

    Structurally satisfies :class:`~tolokaforge.core.loop.LoopLLMClient` at
    runtime — the loop's ``_generate`` only calls it with kwargs the
    Protocol declares plus the optional ``observation``.
    """

    def __init__(self, results: list[GenerationResult]) -> None:
        self._results = list(results)
        self.received: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> GenerationResult:
        self.received.append(kwargs)
        return self._results.pop(0)


def _run_agent_loop_with(events: _RecordingEvents, *, call_observation: Any) -> Any:
    """Drive one ``ToolCallingLoop`` iteration and return the LLM client."""
    from tolokaforge.core.logging import get_logger
    from tolokaforge.core.loop import (
        LoopConfig,
        TerminationDecision,
        ToolCallingLoop,
        classify_loop_error,
    )
    from tolokaforge.core.models import Message
    from tolokaforge.core.runner import _AgentMetricsSink
    from tolokaforge.tools.registry import ToolResult

    client = _ObservationCapturingClient(
        [GenerationResult(text="done", usage=Usage(prompt_tokens=1))]
    )

    class _NoopExecutor:
        def execute(self, tool_name: str, arguments: Any) -> ToolResult:
            return ToolResult(success=True, output="")

        def get_logs(self) -> list[dict[str, Any]]:
            return []

    def _stop_first_turn(result: Any, turn: int, messages: list[Message]) -> Any:
        from tolokaforge.core.models import TerminationReason

        return TerminationDecision(reason=TerminationReason.AGENT_DONE, system_message="done")

    def _classify(exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    import time as _time

    ToolCallingLoop(
        llm_client=client,
        tool_executor=_NoopExecutor(),
        tool_schemas=[],
        config=LoopConfig(max_turns=1, episode_timeout_s=10_000),
        metrics=_AgentMetricsSink(Metrics(), events=events, trial_id="taskA:0"),
        should_terminate=_stop_first_turn,
        classify_error=_classify,
        logger=get_logger("test-wiring", strict=False),
        call_observation=call_observation,
    ).run("sys", [], _time.time())
    return client


def test_tool_calling_loop_forwards_call_observation_to_generate() -> None:
    """Agent path: when ``call_observation`` is set on the loop, every
    ``LLMClient.generate`` call receives it as the ``observation`` kwarg —
    so the client's per-call retry controller can fire the LLM-call trio
    against the correct trial + role."""
    from tolokaforge.core.run_display_events import LLMCallObservation

    events = _RecordingEvents()
    observation = LLMCallObservation(events=events, trial_id="taskA:0", role="agent")

    client = _run_agent_loop_with(events, call_observation=observation)

    assert len(client.received) == 1
    kwargs = client.received[0]
    assert kwargs["observation"] is observation
    assert kwargs["observation"].role == "agent"
    assert kwargs["observation"].trial_id == "taskA:0"


def test_tool_calling_loop_forwards_none_observation_when_unset() -> None:
    """Judge path (and any caller that leaves ``call_observation`` unset):
    ``generate`` receives ``observation=None`` — the ``LoopLLMClient``
    Protocol declares the kwarg, and the ``LLMClient`` treats ``None`` as
    "no per-call sink", so the LLM-call trio does not fire."""
    events = _RecordingEvents()

    client = _run_agent_loop_with(events, call_observation=None)

    assert len(client.received) == 1
    assert client.received[0]["observation"] is None


def test_user_simulator_llm_reply_forwards_observation_to_generate() -> None:
    """User path: :meth:`UserSimulator.reply` in llm mode forwards
    ``observation`` verbatim to the inner ``LLMClient.generate`` — so the
    user simulator surfaces as ``role="user"`` LLM-call events."""
    from tolokaforge.core.llm import UserSimulator
    from tolokaforge.core.models import Message, MessageRole, ModelConfig
    from tolokaforge.core.run_display_events import LLMCallObservation

    simulator = UserSimulator(
        mode="llm",
        llm_config=ModelConfig(provider="openai", name="gpt-4"),
        backstory="do a thing",
    )
    fake_client = MagicMock()
    fake_client.generate.return_value = GenerationResult(text="hi", tool_calls=[], usage=Usage())
    simulator.llm_client = fake_client

    events = _RecordingEvents()
    observation = LLMCallObservation(events=events, trial_id="taskA:0", role="user")

    simulator.reply(
        [Message(role=MessageRole.ASSISTANT, content="hello?")], observation=observation
    )

    _, kwargs = fake_client.generate.call_args
    assert kwargs["observation"] is observation
    assert kwargs["observation"].role == "user"


def test_user_simulator_scripted_reply_ignores_observation() -> None:
    """Scripted mode never touches the wire — passing an observation is a
    no-op, not a raise. Keeps the reply signature uniform between modes."""
    from tolokaforge.core.llm import UserSimulator
    from tolokaforge.core.models import Message, MessageRole
    from tolokaforge.core.run_display_events import LLMCallObservation

    simulator = UserSimulator(mode="scripted")
    events = _RecordingEvents()
    observation = LLMCallObservation(events=events, trial_id="taskA:0", role="user")

    result = simulator.reply(
        [Message(role=MessageRole.ASSISTANT, content="hello?")], observation=observation
    )

    assert result.text
    assert events.calls == []


def _assert_llm_pairing_invariants(events: _RecordingEvents) -> None:
    """Every ``llm_call_started`` has a matching ``llm_call_finished`` with
    identical ``(trial_id, role, provider, model, attempt)``; every
    ``llm_retry_scheduled`` sits between two consecutive ``started`` calls
    for the same ``(trial_id, role)`` pair. Locked as a helper so any test
    that captures ``_RecordingEvents`` can call it directly."""
    key_fields = ("trial_id", "role", "provider", "model", "attempt")
    open_starts: dict[tuple[str, str], tuple[str, str, str, str, int]] = {}
    for name, kwargs in events.calls:
        if name == "llm_call_started":
            tuple_key = tuple(kwargs[f] for f in key_fields)
            open_starts[(kwargs["trial_id"], kwargs["role"])] = tuple_key
        elif name == "llm_call_finished":
            tuple_key = tuple(kwargs[f] for f in key_fields)
            assert open_starts.pop((kwargs["trial_id"], kwargs["role"])) == tuple_key
        elif name == "llm_retry_scheduled":
            assert (
                kwargs["trial_id"],
                kwargs["role"],
            ) not in open_starts, "llm_retry_scheduled fired while an attempt was still open"
    assert open_starts == {}, f"unmatched llm_call_started: {open_starts}"


def test_pairing_invariants_hold_on_a_clean_success_sequence() -> None:
    events = _RecordingEvents()
    events.llm_call_started(
        trial_id="t:0", role="agent", provider="openai", model="gpt-4", attempt=1
    )
    events.llm_call_finished(
        trial_id="t:0",
        role="agent",
        provider="openai",
        model="gpt-4",
        attempt=1,
        duration_s=0.1,
        error=None,
    )
    _assert_llm_pairing_invariants(events)


def test_pairing_invariants_hold_on_a_retry_sequence() -> None:
    events = _RecordingEvents()
    events.llm_call_started(
        trial_id="t:0", role="agent", provider="openai", model="gpt-4", attempt=1
    )
    events.llm_call_finished(
        trial_id="t:0",
        role="agent",
        provider="openai",
        model="gpt-4",
        attempt=1,
        duration_s=0.1,
        error="boom",
    )
    events.llm_retry_scheduled(
        trial_id="t:0",
        role="agent",
        provider="openai",
        model="gpt-4",
        attempt=1,
        next_attempt_in_s=4.0,
        reason="RuntimeError",
    )
    events.llm_call_started(
        trial_id="t:0", role="agent", provider="openai", model="gpt-4", attempt=2
    )
    events.llm_call_finished(
        trial_id="t:0",
        role="agent",
        provider="openai",
        model="gpt-4",
        attempt=2,
        duration_s=0.1,
        error=None,
    )
    _assert_llm_pairing_invariants(events)


def _run_orch_with_two_role_models(tmp_path: Path, events: _RecordingEvents) -> None:
    from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
    from tolokaforge.core.models import (
        EvaluationConfig,
        ModelConfig,
        OrchestratorConfig,
        RunConfig,
    )
    from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
    from tolokaforge.core.runtime import InMemoryRuntimeBackend

    run_root = tmp_path / "results" / "run_models"
    run_root.parent.mkdir(parents=True, exist_ok=True)
    config = RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4o"),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
            shuffle_trials=False,
        ),
        evaluation=EvaluationConfig(output_dir=str(run_root)),
    )

    def make_conductor(_ctx: ConductorContext) -> InMemoryConductor:
        return InMemoryConductor()

    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            events=events,
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=make_conductor,
        ),
    )
    orch.tasks = [_make_task_for_run("TASK-A")]

    orch.adapter = _adapter_for_run(tmp_path)

    orch.run()


def test_trial_started_carries_agent_and_user_model_identity(tmp_path: Path) -> None:
    """Locks the orchestrator's ``trial_started`` emission at the
    :class:`Orchestrator.run` lease site: ``agent_model`` /
    ``user_model`` are populated from the run config as
    ``"{provider}/{name}"``, so a display can render per-role identity
    without having to look the config up itself."""
    events = _RecordingEvents()
    _run_orch_with_two_role_models(tmp_path, events)

    (trial_started,) = events.kwargs_for("trial_started")
    assert trial_started["agent_model"] == "openai/gpt-4o"
    assert trial_started["user_model"] == "openrouter/anthropic/claude-sonnet-4.6"
