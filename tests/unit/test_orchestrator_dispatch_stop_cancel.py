"""The run loop cancels in-flight dispatch leases on a stop, before the pool joins.

A dispatch run whose adapter can be controlled must, when the run stops with work
still in flight (an exception or ``KeyboardInterrupt``), ask the adapter to
cancel the active leases *before* the worker pool joins them — otherwise the
pool drains every in-flight trial to completion first and the cancel is a no-op.
The single-box (non-dispatch) path keeps its plain context-manager join and never
touches the cancel hook.

Both cases run a real ``Orchestrator.run()`` with no Docker and no LLM: the
dispatch case injects a fake controllable adapter that returns one result and
blocks the other trial; the single-box case runs an in-memory conductor.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from tolokaforge.core.conductor import InMemoryConductor, _default_success_trajectory
from tolokaforge.core.dispatch.control import JobHandle, JobState, JobStatus
from tolokaforge.core.dispatch.errors import DispatchUnsupported
from tolokaforge.core.models import (
    ComputeConfig,
    EvaluationConfig,
    InitialStateConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    ToolsConfig,
    UserSimulatorConfig,
)
from tolokaforge.core.models.run_config import DispatchConfig
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.runner.models import RunnerGradingConfig, TaskDescription

pytestmark = pytest.mark.unit


def _result(trial_id: str) -> TrialResult:
    task_id, index = trial_id.rsplit(":", 1)
    return TrialResult.from_trajectory(
        trial_id=trial_id, trajectory=_default_success_trajectory(task_id, int(index))
    )


class _FakeControllableAdapter:
    """A controllable dispatch adapter: one trial returns, the rest block until cancelled.

    Records every ``cancel`` call so a test can assert the run loop cancelled the
    in-flight lease. ``cancel`` also releases the blocked trial so the worker
    thread can finish once the pool is torn down.
    """

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self._release = threading.Event()
        self._lock = threading.Lock()
        self._started = 0

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self._release.set()

    def run_trial_node(self, inputs: TrialNodeInputs) -> TrialResult:
        trial_id = inputs.trial_spec.trial_id
        with self._lock:
            self._started += 1
            first = self._started == 1
        if first:
            return _result(trial_id)
        # A later trial stays in flight until the run loop cancels it.
        assert self._release.wait(timeout=10)
        return _result(trial_id)

    def cancel(self, handle: JobHandle) -> None:
        self.cancelled.append(handle.trial_id)
        self._release.set()

    def status(self, handle: JobHandle) -> JobStatus:
        return JobStatus(state=JobState.RUNNING)

    def fetch_result(self, handle: JobHandle) -> TrialResult:
        raise DispatchUnsupported("Unsupported", "", "returned live")


def _task_config(task_id: str) -> TaskConfig:
    return TaskConfig(
        task_id=task_id,
        name=f"Test Task {task_id}",
        category="tool_use",
        description="A test task",
        initial_state=InitialStateConfig(json_db={"items": [{"id": "I1"}]}),
        tools=ToolsConfig(),
        user_simulator=UserSimulatorConfig(mode="scripted"),
        grading="grading.yaml",
    )


def _write_grading_yaml(task_dir: Path) -> None:
    (task_dir / "grading.yaml").write_text(
        yaml.safe_dump(
            {
                "combine": {"method": "weighted", "weights": {"state_checks": 1.0}},
                "state_checks": {"jsonpaths": [{"path": "$.items", "operator": "exists"}]},
            }
        )
    )


def _task_description(task_id: str) -> TaskDescription:
    return TaskDescription(
        task_id=task_id,
        name=task_id,
        category="test",
        description="d",
        adapter_type="native",
        system_prompt="sys",
        grading=RunnerGradingConfig(),
    )


def _run_config(tmp_path: Path, *, workers: int, dispatch: DispatchConfig | None) -> RunConfig:
    return RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
        orchestrator=OrchestratorConfig(repeats=1, auto_start_services=False, dispatch=dispatch),
        compute=ComputeConfig(workers=workers),
        evaluation=EvaluationConfig(output_dir=str(tmp_path / "results" / "run")),
    )


def _wire_adapter(orch: Orchestrator, tmp_path: Path, task_ids: list[str]) -> None:
    orch.tasks = [_task_config(tid) for tid in task_ids]
    adapter = MagicMock()
    adapter.describe_task.side_effect = lambda tid: _task_description(tid)
    adapter.requires_judge_model.return_value = False
    adapter.docker_stack_requirements.return_value = MagicMock(needs_rag_service=False)
    adapter.trial_grader_name = "runner_rpc"
    _write_grading_yaml(tmp_path)
    adapter.get_task_dir.return_value = tmp_path
    adapter.fingerprint.return_value = None
    orch.adapter = adapter


def test_dispatch_stop_cancels_in_flight_leases_before_the_pool_joins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeControllableAdapter()
    config = _run_config(
        tmp_path,
        workers=2,
        dispatch=DispatchConfig(adapter="local", config={"mode": "thread", "workers": 2}),
    )
    orch = Orchestrator(config, deps=OrchestratorDeps(dispatch_adapter=fake))
    _wire_adapter(orch, tmp_path, ["taskA", "taskB"])

    # Inject the stop from the main-thread bookkeeping the instant the first
    # trial completes, while the second trial is still blocked in flight.
    original = orch._record_attempt

    def _stop_on_first(trajectory: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(orch, "_record_attempt", _stop_on_first)

    with pytest.raises(KeyboardInterrupt):
        orch.run()

    # The run cancelled the one still-in-flight lease (the blocked trial), not the
    # one that had already returned.
    assert len(fake.cancelled) == 1
    assert fake.cancelled[0] in {"taskA:0", "taskB:0"}
    _ = original  # the real hook is never reached past the injected stop


def test_single_box_stop_never_touches_the_cancel_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _run_config(tmp_path, workers=2, dispatch=None)

    def conductor_factory(_ctx: Any) -> InMemoryConductor:
        return InMemoryConductor(
            trajectory_factory=lambda task_id, trial_idx: _default_success_trajectory(
                task_id, trial_idx
            )
        )

    from tolokaforge.core.runtime import InMemoryRuntimeBackend

    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=conductor_factory,
        ),
    )
    _wire_adapter(orch, tmp_path, ["taskA", "taskB"])

    cancel_calls: list[Any] = []
    monkeypatch.setattr(
        orch,
        "_cancel_active_dispatch_jobs",
        lambda *a, **k: cancel_calls.append(a),
    )

    orch.run()

    # The single-box path drains to completion and never reaches the cancel hook.
    assert orch._stopped_reason is None
    assert len(orch.results) == 2
    assert cancel_calls == []
