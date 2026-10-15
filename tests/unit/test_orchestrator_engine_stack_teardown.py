"""The auto-started EngineStack is torn down on every exit path.

``Orchestrator.run`` auto-starts a fixed-name engine stack
(``tolokaforge-runner`` / ``tolokaforge-db-service`` on ``runner-net``). If a
run can exit without destroying it — a failed ``start_all``, a runtime-backend
``connect`` that raises, a mid-run exception — the next run is blocked by the
stale, name-collided stack.

These tests pin the guarantee at every seam:

- ``_teardown_engine_services`` itself: ``None``-safe, stops the TypeSense
  bridge *before* destroying the stack, swallows teardown exceptions, and does
  not re-stop TypeSense on a second call.
- A real ``run()`` whose runtime backend fails in the pre-loop window
  (``connect``) or mid-run (``health_check``), and a run that succeeds — each
  must reach the teardown exactly once.
- The auto-start error path: a stack whose ``start_all`` raises is destroyed
  before the failure propagates.

Everything runs against an :class:`InMemoryConductor` and an
:class:`InMemoryRuntimeBackend`. No Docker, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.models import (
    ComputeConfig,
    EvaluationConfig,
    Grade,
    GradeComponents,
    InitialStateConfig,
    Metrics,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    ToolsConfig,
    Trajectory,
    TrialStatus,
    UserSimulatorConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.runner.models import RunnerGradingConfig, TaskDescription

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Builders shared by the run()-level tests
# ---------------------------------------------------------------------------


def _traj(task_id: str, trial_idx: int) -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_idx,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[],
        metrics=Metrics(cost_usd=0.0),
        grade=Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(),
            reasons="synthetic-success",
        ),
    )


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


def _write_grading_yaml(task_dir: Path) -> None:
    import yaml

    (task_dir / "grading.yaml").write_text(
        yaml.safe_dump(
            {
                "combine": {"method": "weighted", "weights": {"state_checks": 1.0}},
                "state_checks": {"jsonpaths": [{"path": "$.items", "operator": "exists"}]},
            }
        )
    )


def _run_config(*, tmp_path: Path, auto_start_services: bool = False) -> RunConfig:
    return RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
        orchestrator=OrchestratorConfig(repeats=1, auto_start_services=auto_start_services),
        compute=ComputeConfig(workers=1),
        evaluation=EvaluationConfig(output_dir=str(tmp_path / "results" / "run")),
    )


def _build_orchestrator(
    *,
    tmp_path: Path,
    runtime_backend: Any,
    auto_start_services: bool = False,
) -> Orchestrator:
    config = _run_config(tmp_path=tmp_path, auto_start_services=auto_start_services)

    def conductor_factory(_ctx: Any) -> InMemoryConductor:
        return InMemoryConductor(trajectory_factory=_traj)

    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=runtime_backend,
            conductor_factory=conductor_factory,
        ),
    )
    orch.tasks = [_task_config("TASK-A")]
    adapter = MagicMock()
    adapter.describe_task.side_effect = lambda tid: _task_description(tid)
    adapter.requires_judge_model.return_value = False
    adapter.docker_stack_requirements.return_value = None
    adapter.trial_grader_name = "runner_rpc"
    adapter.get_task_dir.return_value = tmp_path
    adapter.fingerprint.return_value = None
    orch.adapter = adapter
    _write_grading_yaml(tmp_path)
    return orch


class _ConnectRaisingBackend(InMemoryRuntimeBackend):
    """Fails in the pre-loop window — ``connect`` is called before the trial loop."""

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        raise RuntimeError("connect boom")


class _HealthRaisingBackend(InMemoryRuntimeBackend):
    """Connects cleanly, then fails mid-run at the health probe inside the loop body."""

    def health_check(self) -> bool:
        raise RuntimeError("health boom")


# ---------------------------------------------------------------------------
# _teardown_engine_services — the teardown primitive
# ---------------------------------------------------------------------------


class TestTeardownEngineServices:
    def _orch(self, tmp_path: Path) -> Orchestrator:
        return Orchestrator(_run_config(tmp_path=tmp_path))

    def test_none_stack_is_a_noop(self, tmp_path: Path) -> None:
        self._orch(tmp_path)._teardown_engine_services(None)  # must not raise

    def test_destroys_the_stack(self, tmp_path: Path) -> None:
        orch = self._orch(tmp_path)
        stack = MagicMock()
        orch._teardown_engine_services(stack)
        stack.destroy.assert_called_once()

    def test_stops_typesense_before_destroying_stack(self, tmp_path: Path) -> None:
        orch = self._orch(tmp_path)
        order: list[str] = []
        typesense = MagicMock()
        typesense.stop.side_effect = lambda: order.append("typesense_stop")
        stack = MagicMock()
        stack.destroy.side_effect = lambda *a, **k: order.append("stack_destroy")
        orch._typesense_server = typesense

        orch._teardown_engine_services(stack)

        assert order == ["typesense_stop", "stack_destroy"]
        assert orch._typesense_server is None

    def test_swallows_destroy_exception(self, tmp_path: Path) -> None:
        orch = self._orch(tmp_path)
        stack = MagicMock()
        stack.destroy.side_effect = RuntimeError("destroy boom")
        orch._teardown_engine_services(stack)  # must not raise

    def test_does_not_re_stop_typesense_on_a_second_call(self, tmp_path: Path) -> None:
        orch = self._orch(tmp_path)
        typesense = MagicMock()
        orch._typesense_server = typesense

        orch._teardown_engine_services(None)
        orch._teardown_engine_services(None)

        typesense.stop.assert_called_once()


# ---------------------------------------------------------------------------
# run() reaches teardown on every exit path
# ---------------------------------------------------------------------------


class TestRunAlwaysTearsDown:
    def _spy_teardown(self, orch: Orchestrator) -> list[Any]:
        calls: list[Any] = []
        original = orch._teardown_engine_services

        def spy(service_stack: Any) -> None:
            calls.append(service_stack)
            original(service_stack)

        orch._teardown_engine_services = spy  # type: ignore[method-assign]
        return calls

    def test_success_path_tears_down_once(self, tmp_path: Path) -> None:
        orch = _build_orchestrator(tmp_path=tmp_path, runtime_backend=InMemoryRuntimeBackend())
        calls = self._spy_teardown(orch)

        orch.run()

        assert len(calls) == 1

    def test_pre_loop_connect_failure_tears_down(self, tmp_path: Path) -> None:
        orch = _build_orchestrator(tmp_path=tmp_path, runtime_backend=_ConnectRaisingBackend())
        calls = self._spy_teardown(orch)

        with pytest.raises(RuntimeError, match="connect boom"):
            orch.run()

        assert len(calls) == 1

    def test_mid_run_failure_tears_down(self, tmp_path: Path) -> None:
        orch = _build_orchestrator(tmp_path=tmp_path, runtime_backend=_HealthRaisingBackend())
        calls = self._spy_teardown(orch)

        with pytest.raises(RuntimeError, match="health boom"):
            orch.run()

        assert len(calls) == 1


# ---------------------------------------------------------------------------
# The auto-start error path destroys the partially-started stack
# ---------------------------------------------------------------------------


class _StartRaisingStack:
    """A stack whose ``start_all`` raises after the stack object exists —
    the window where a fixed-name container/network leak would otherwise occur."""

    def __init__(self) -> None:
        self.destroyed = False
        # Read by the pre-start ``phase_changed`` snapshot of declared services.
        self.services: dict[str, Any] = {}

    def start_all(self, wait: bool = True) -> None:
        raise RuntimeError("start boom")

    def destroy(self, *args: Any, **kwargs: Any) -> None:
        self.destroyed = True


def test_auto_start_failure_destroys_the_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = _build_orchestrator(
        tmp_path=tmp_path,
        runtime_backend=InMemoryRuntimeBackend(),
        auto_start_services=True,
    )
    stack = _StartRaisingStack()
    monkeypatch.setattr("tolokaforge.docker.stacks.core_stack", lambda **kwargs: stack)
    monkeypatch.setattr("tolokaforge.docker.stacks.full_stack", lambda **kwargs: stack)

    with pytest.raises(RuntimeError, match="start boom"):
        orch.run()

    assert stack.destroyed is True
