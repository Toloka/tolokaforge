"""Graceful-shutdown wiring for :class:`Orchestrator.run` under budgets.

Locks the Stage-2 contract: when a budget hit lands, the orchestrator
stops enqueuing new trials, lets in-flight trials complete, writes
``LIMIT_HIT.json`` under ``output_dir``, sets ``_stopped_reason``, and
calls ``state_manager.mark_run_paused()`` — same shape as the pre-B3
cost-cap code path.

Every case runs a real ``Orchestrator.run()`` end-to-end against an
:class:`InMemoryConductor` (whose trajectory factory sets per-trial
``cost_usd``) and an :class:`InMemoryRuntimeBackend`. No Docker, no LLM.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.budgets import (
    CompositeBudget,
    CostBudget,
    SampleBudget,
    TimeBudget,
)
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
from tolokaforge.runner.models import GradingConfig, TaskDescription

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — trajectory factory with configurable per-trial cost
# ---------------------------------------------------------------------------


def _traj(task_id: str, trial_idx: int, cost_usd: float) -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_idx,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[],
        metrics=Metrics(cost_usd=cost_usd),
        grade=Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(),
            reasons="synthetic-success",
        ),
    )


def _cost_factory(cost_per_trial: float) -> Callable[[str, int], Trajectory]:
    def factory(task_id: str, trial_idx: int) -> Trajectory:
        return _traj(task_id, trial_idx, cost_per_trial)

    return factory


def _task_config(task_id: str) -> TaskConfig:
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


def _task_description(task_id: str) -> TaskDescription:
    return TaskDescription(
        task_id=task_id,
        name=task_id,
        category="test",
        description="d",
        adapter_type="native",
        system_prompt="sys",
        grading=GradingConfig(),
    )


def _make_run_config(*, tmp_path: Path, workers: int = 1) -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(
            repeats=1,
            auto_start_services=False,
        ),
        compute=ComputeConfig(workers=workers),
        evaluation=EvaluationConfig(output_dir=str(tmp_path / "results" / "run")),
    )


def _build_orchestrator(
    *,
    tmp_path: Path,
    task_ids: list[str],
    budget: CompositeBudget | None,
    cost_per_trial: float = 0.0,
    workers: int = 1,
    trajectory_factory: Callable[[str, int], Trajectory] | None = None,
    legacy_max_budget_usd: float | None = None,
) -> tuple[Orchestrator, Path]:
    """Wire an Orchestrator whose ``run()`` will exercise the budget path."""
    config = _make_run_config(tmp_path=tmp_path, workers=workers)
    if legacy_max_budget_usd is not None:
        config.compute.max_budget_usd = legacy_max_budget_usd  # type: ignore[union-attr]

    factory = trajectory_factory or _cost_factory(cost_per_trial)

    def conductor_factory(_ctx: Any) -> InMemoryConductor:
        return InMemoryConductor(trajectory_factory=factory)

    runtime = InMemoryRuntimeBackend()
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=runtime,
            conductor_factory=conductor_factory,
            budget=budget,
        ),
    )
    orch.tasks = [_task_config(tid) for tid in task_ids]
    adapter = MagicMock()
    adapter.to_task_description.side_effect = lambda tid: _task_description(tid)
    adapter.docker_stack_requirements.return_value = MagicMock(needs_rag_service=False)
    orch.adapter = adapter
    return orch, tmp_path / "results" / "run"


def _read_marker(output_dir: Path) -> dict[str, Any] | None:
    marker = output_dir / "LIMIT_HIT.json"
    if not marker.exists():
        return None
    return json.loads(marker.read_text())


# ---------------------------------------------------------------------------
# Case A — cost budget hit
# ---------------------------------------------------------------------------


def test_cost_budget_stops_enqueuing_after_threshold(tmp_path: Path) -> None:
    """4 trials at $0.02 each, cost cap at $0.03 — after two complete
    (total $0.04 ≥ $0.03) the budget fires, no further trials are
    scheduled, and ``LIMIT_HIT.json`` records ``which='cost'``."""
    budget = CompositeBudget([CostBudget(limit_usd=0.03)])
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC", "taskD"],
        budget=budget,
        cost_per_trial=0.02,
    )

    output_dir = orch.run()

    assert orch._stopped_reason == "cost limit"
    marker = _read_marker(output_dir)
    assert marker is not None
    assert marker["which"] == "cost"
    assert marker["threshold"] == pytest.approx(0.03)
    assert marker["value_at_hit"] >= 0.03 - 1e-9
    # In-flight trials complete → completed ∈ {2, 3, 4} depending on how the
    # ThreadPoolExecutor drained; the hard invariant is "cost cap was
    # respected past a small overshoot", not the exact count.
    assert 2 <= len(orch.results) < 4


# ---------------------------------------------------------------------------
# Case B — sample budget hit
# ---------------------------------------------------------------------------


def test_sample_budget_stops_after_two_terminations(tmp_path: Path) -> None:
    budget = CompositeBudget([SampleBudget(limit=2)])
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC", "taskD"],
        budget=budget,
        cost_per_trial=0.01,
    )

    output_dir = orch.run()

    assert orch._stopped_reason == "sample limit"
    marker = _read_marker(output_dir)
    assert marker is not None
    assert marker["which"] == "sample"
    assert marker["threshold"] == pytest.approx(2.0)
    assert marker["value_at_hit"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Case C — time budget hit
# ---------------------------------------------------------------------------


def test_time_budget_stops_after_wall_clock_crosses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkey-patch ``time.monotonic`` to move past the limit after the
    first trial's ``record_trial_terminated`` starts the clock."""
    clock: Iterator[float] = iter([0.0, 0.001, 0.002, 100.0, 100.0, 100.0, 100.0])

    def fake_monotonic() -> float:
        try:
            return next(clock)
        except StopIteration:
            return 100.0

    monkeypatch.setattr("tolokaforge.core.budgets.time.monotonic", fake_monotonic)
    budget = CompositeBudget([TimeBudget(limit_seconds=0.01)])
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC", "taskD"],
        budget=budget,
        cost_per_trial=0.01,
    )

    output_dir = orch.run()

    assert orch._stopped_reason == "time limit"
    marker = _read_marker(output_dir)
    assert marker is not None
    assert marker["which"] == "time"


# ---------------------------------------------------------------------------
# Case D — no budget → run to completion, no marker
# ---------------------------------------------------------------------------


def test_no_budget_runs_all_trials_and_writes_no_marker(tmp_path: Path) -> None:
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC", "taskD"],
        budget=None,
        cost_per_trial=1000.0,  # would blow any conceivable cap
    )

    output_dir = orch.run()

    assert orch._stopped_reason is None
    assert not (output_dir / "LIMIT_HIT.json").exists()
    assert len(orch.results) == 4


# ---------------------------------------------------------------------------
# Case E — in-flight trials complete gracefully
# ---------------------------------------------------------------------------


def test_in_flight_trials_complete_after_budget_hit(tmp_path: Path) -> None:
    """With workers=4 and sample_limit=1, once the first trial terminates
    the budget fires; ThreadPoolExecutor still drains the 3 already-leased
    in-flight trials. The wait loop must not abandon them.
    """
    budget = CompositeBudget([SampleBudget(limit=1)])
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC", "taskD"],
        budget=budget,
        cost_per_trial=0.01,
        workers=4,
    )

    output_dir = orch.run()

    assert orch._stopped_reason == "sample limit"
    # No trials abandoned mid-flight — every leased trial should have
    # produced a trajectory. With 4 workers the initial fill leases all 4;
    # every one of those completes.
    assert len(orch.results) == 4
    marker = _read_marker(output_dir)
    assert marker is not None


# ---------------------------------------------------------------------------
# Case F — legacy ``compute.max_budget_usd`` continues to work
# ---------------------------------------------------------------------------


def test_legacy_max_budget_usd_field_drives_cost_budget(tmp_path: Path) -> None:
    """Regression: constructing an Orchestrator with
    ``config.compute.max_budget_usd=0.03`` and no ``deps.budget`` MUST
    still trigger the same graceful-shutdown shape — the pre-B3
    observable behaviour is preserved by the promotion inside
    ``_resolve_budget``.
    """
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC", "taskD"],
        budget=None,
        cost_per_trial=0.02,
        legacy_max_budget_usd=0.03,
    )

    output_dir = orch.run()

    assert orch._stopped_reason == "cost limit"
    marker = _read_marker(output_dir)
    assert marker is not None
    assert marker["which"] == "cost"
    assert marker["threshold"] == pytest.approx(0.03)
    # Legacy path fires and lets in-flight complete, matching the pre-B3 shape.
    assert 2 <= len(orch.results) < 4


# ---------------------------------------------------------------------------
# state_manager.mark_run_paused fires on any budget hit
# ---------------------------------------------------------------------------


def test_run_paused_state_recorded_on_budget_hit(tmp_path: Path) -> None:
    """The ``state_manager`` records ``status='paused'`` after a budget
    hit — the same state the pre-B3 cost-cap wrote."""
    budget = CompositeBudget([SampleBudget(limit=1)])
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB"],
        budget=budget,
        cost_per_trial=0.01,
    )

    output_dir = orch.run()

    from tolokaforge.core.resume import RunStateManager

    state = RunStateManager(output_dir).load_state()
    assert state is not None
    assert state.status == "paused"


def test_natural_completion_records_run_completed(tmp_path: Path) -> None:
    """Without a budget the ``state_manager`` records ``status='completed'``
    — the "else" branch of the shutdown block."""
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA"],
        budget=None,
        cost_per_trial=0.0,
    )

    output_dir = orch.run()

    from tolokaforge.core.resume import RunStateManager

    state = RunStateManager(output_dir).load_state()
    assert state is not None
    assert state.status == "completed"
