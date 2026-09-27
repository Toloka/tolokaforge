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
import yaml

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
from tolokaforge.runner.models import RunnerGradingConfig, TaskDescription

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
    """The task every budget test runs, sized to what the run's pre-flight reads.

    It seeds a table because the grading block beside it asserts a ``path:`` over
    the trial's database, which is authorable only on a task that provisions one.
    """
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
    """The block every task here declares, so the run's pre-flight has one to read.

    These runs are about the budget, not about grading, so the block is the smallest
    gradeable one: a single state check carrying the whole weight.
    """
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


def _make_run_config(*, tmp_path: Path, workers: int = 1) -> RunConfig:
    # models.user is required — the orchestrator fails loud without it (see
    # require_user_simulator_config); pick a non-Anthropic placeholder so
    # the fixture matches the docs example.
    return RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
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
    adapter.trial_grader_name = "runner_rpc"
    _write_grading_yaml(tmp_path)
    adapter.get_task_dir.return_value = tmp_path
    adapter.fingerprint.return_value = None
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


# ---------------------------------------------------------------------------
# grading_completeness is published on every path that returns from run()
# ---------------------------------------------------------------------------


def test_grading_completeness_published_on_budget_pause(tmp_path: Path) -> None:
    """A budget-paused ``run()`` still binds ``grading_completeness``.

    Every caller of :meth:`Orchestrator.run` reads the attribute
    unconditionally — ``dx.cli.main`` feeds it to the completeness gates
    straight after ``run()`` returns — so leaving it unbound turns a clean
    budget stop into an ``AttributeError`` and a failed run.

    The counts must describe the attempts that actually ran, not the trial
    set that was planned: a paused run is truncated by construction.
    """
    budget = CompositeBudget([SampleBudget(limit=1)])
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC"],
        budget=budget,
        cost_per_trial=0.01,
    )

    output_dir = orch.run()

    completeness = orch.grading_completeness
    assert completeness.total_attempts == len(orch.results)
    assert completeness.total_attempts < 3, "the budget did not truncate the run"

    from tolokaforge.core.resume import RunStateManager

    state = RunStateManager(output_dir).load_state()
    assert state is not None
    assert state.status == "paused", "publishing must not stamp a paused run completed"


def test_budget_pause_writes_the_reports_its_gates_point_at(tmp_path: Path) -> None:
    """A paused run writes ``aggregate.json``, because its gates cite it.

    ``_fail_on_completeness_gates`` runs on this path and, when it fires,
    tells the operator to read ``ungradeable`` / ``infrastructure_aborts``
    in ``aggregate.json``. Skipping report generation would leave that
    message pointing at a file that was never written — or, on a run
    directory an earlier pass already wrote, at a stale one that disagrees
    with the counts just printed.
    """
    budget = CompositeBudget([SampleBudget(limit=1)])
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC"],
        budget=budget,
        cost_per_trial=0.01,
    )

    output_dir = orch.run()

    aggregate = output_dir / "aggregate.json"
    assert aggregate.exists(), "a paused run must write the report its gates name"
    payload = json.loads(aggregate.read_text())
    assert payload, "aggregate.json is empty"

    from tolokaforge.core.resume import RunStateManager

    state = RunStateManager(output_dir).load_state()
    assert state is not None
    assert state.status == "paused", "writing reports must not stamp the run completed"


def test_budget_pause_refreshes_a_stale_report_from_an_earlier_pass(
    tmp_path: Path,
) -> None:
    """The resumed-directory case: a pre-existing report must not survive.

    A run directory that already holds an ``aggregate.json`` from an
    earlier pass is the situation where a missing refresh does real
    damage — the operator reads numbers that describe a different run.
    """
    budget = CompositeBudget([SampleBudget(limit=1)])
    orch, run_dir = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB", "taskC"],
        budget=budget,
        cost_per_trial=0.01,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    stale = run_dir / "aggregate.json"
    stale.write_text(json.dumps({"sentinel": "from-an-earlier-pass"}))

    output_dir = orch.run()

    payload = json.loads((output_dir / "aggregate.json").read_text())
    assert "sentinel" not in payload, "the stale report from the earlier pass survived"


def test_grading_completeness_published_when_budget_exhausted_at_start(
    tmp_path: Path,
) -> None:
    """The degenerate pause — the cap is already spent, so nothing is scheduled.

    ``run()`` returns through the same paused branch with zero results, which
    is the one case where publishing could plausibly be skipped as pointless.
    It cannot be: the caller reads the attribute either way.

    The published value is deliberately the empty one, and it is worth being
    explicit about what that buys and costs. ``zero_coverage`` is guarded on
    ``total_attempts > 0`` (ADR-0041), so a run that scheduled nothing does not
    trip it and the CLI exits ``0`` even under ``--fail-on-zero-coverage``.
    That is correct on the ADR's terms — there were no trials to measure — but
    it means an automated caller resuming an already-spent run sees success
    without any work having happened, and must read ``run_state.json``'s
    ``paused`` status or the stopped banner to tell the two apart. Documented
    in ``docs/CLI.md`` under the run exit codes.
    """
    budget = CompositeBudget([SampleBudget(limit=0)])
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB"],
        budget=budget,
        cost_per_trial=0.01,
    )

    orch.run()

    completeness = orch.grading_completeness
    assert completeness.total_attempts == 0
    assert completeness.zero_coverage is False


def test_natural_completion_still_publishes_grading_completeness(tmp_path: Path) -> None:
    """The unpaused path keeps publishing — the pause branch is additive."""
    orch, _ = _build_orchestrator(
        tmp_path=tmp_path,
        task_ids=["taskA", "taskB"],
        budget=None,
        cost_per_trial=0.0,
    )

    orch.run()

    assert orch.grading_completeness.total_attempts == 2
