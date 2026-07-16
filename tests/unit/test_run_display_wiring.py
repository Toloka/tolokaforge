"""Wiring tests for :class:`RunDisplayEvents` propagation.

Locks that the per-trial engine surfaces — ``_AgentMetricsSink``,
``InProcessConductor._grade``, and ``ProvisioningTrialExecutor.execute``
— fire the RunDisplayEvents seam events they own:

- ``_AgentMetricsSink.record_generation`` fires ``trial_progress`` after
  internal metrics accumulation.
- ``InProcessConductor._grade`` fires ``judgment_scored`` right after
  ``trajectory.grade`` is populated.
- ``ProvisioningTrialExecutor.execute`` fires ``trial_provisioned``
  after ``runtime_backend.await_ready(handle)`` succeeds.

The tests use a small ``_RecordingEvents`` double that appends each call
to a shared list so kwargs and call kinds can be asserted directly.
Orchestrator run-level emissions (``run_started`` / ``trial_started`` /
``trial_completed`` / ``trial_failed`` / ``run_finished`` /
``phase_changed``) are covered in the same file once the orchestrator
wiring lands.
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
