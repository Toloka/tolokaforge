"""Unit tests for tolokaforge/core/orchestrator.py — pure-logic helpers and static methods."""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.models import (
    EvaluationConfig,
    Grade,
    GradeComponents,
    Metrics,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    TaskMetadata,
    TerminationReason,
    Trajectory,
    TrialStatus,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_config(**overrides: Any) -> RunConfig:
    """Build a minimal RunConfig for testing."""
    defaults: dict[str, Any] = {
        "models": {
            "agent": ModelConfig(provider="openai", name="gpt-4"),
        },
        "orchestrator": OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
        ),
        "evaluation": EvaluationConfig(output_dir="/tmp/test_output"),
    }
    defaults.update(overrides)
    return RunConfig(**defaults)


def _make_trajectory(
    task_id: str = "TASK-001",
    trial_index: int = 0,
    status: TrialStatus = TrialStatus.COMPLETED,
    termination_reason: TerminationReason | None = None,
    score: float = 1.0,
    binary_pass: bool = True,
    cost: float = 0.01,
    latency: float = 5.0,
    turns: int = 10,
    tool_calls: int = 5,
) -> Trajectory:
    """Build a Trajectory with realistic defaults."""
    now = datetime.now(tz=timezone.utc)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_index,
        start_ts=now,
        end_ts=now,
        status=status,
        termination_reason=termination_reason,
        messages=[],
        metrics=Metrics(
            latency_total_s=latency,
            turns=turns,
            tool_calls=tool_calls,
            cost_usd=cost,
        ),
        grade=Grade(
            binary_pass=binary_pass,
            score=score,
            components=GradeComponents(state_checks=score),
        ),
    )


def _make_task_config(task_id: str = "TASK-001", **overrides: Any) -> TaskConfig:
    """Build a minimal TaskConfig for testing."""
    from tolokaforge.core.models import (
        ActorSpec,
        InitialStateConfig,
        ToolsConfig,
    )

    defaults: dict[str, Any] = {
        "task_id": task_id,
        "name": f"Test Task {task_id}",
        "category": "tool_use",
        "description": "A test task",
        "initial_state": InitialStateConfig(),
        "tools": ToolsConfig(),
        "actors": {"user": ActorSpec(mode="scripted")},
        "grading": "grading.yaml",
    }
    defaults.update(overrides)
    return TaskConfig(**defaults)


# ===================================================================
# Orchestrator.__init__
# ===================================================================


@pytest.mark.unit
class TestOrchestratorConstruction:
    """Orchestrator basic initialisation."""

    def test_basic_construction(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        assert orch.config is config
        assert orch.resume is False
        assert orch.verbose is False
        assert orch.strict is False
        assert orch.tasks == []
        assert orch.results == []

    def test_verbose_flag(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config, verbose=True)
        assert orch.verbose is True

    def test_resume_flag(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config, resume=True)
        assert orch.resume is True

    def test_strict_flag(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config, strict=True)
        assert orch.strict is True

    def test_build_pending_trials_default_order_is_lexicographic(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config(orchestrator=OrchestratorConfig(shuffle_trials=False))
        orch = Orchestrator(config)
        tasks = [_make_task_config("TASK-1"), _make_task_config("TASK-2")]

        pending = orch._build_pending_trials(tasks, repeats=3)

        assert pending == [
            ("TASK-1", 0),
            ("TASK-1", 1),
            ("TASK-1", 2),
            ("TASK-2", 0),
            ("TASK-2", 1),
            ("TASK-2", 2),
        ]

    def test_build_pending_trials_shuffle_changes_order(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config(orchestrator=OrchestratorConfig(shuffle_trials=True))
        orch = Orchestrator(config)
        # Enough items that an accidental identity permutation is implausible
        # (10! = 3.6M).
        tasks = [_make_task_config(f"TASK-{i}") for i in range(5)]
        lexicographic = [(t.task_id, idx) for t in tasks for idx in range(2)]

        random.seed(0)
        pending = orch._build_pending_trials(tasks, repeats=2)

        assert sorted(pending) == sorted(lexicographic)
        assert pending != lexicographic

    def test_build_pending_trials_skip_completed_filters_out_marked(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        tasks = [_make_task_config("TASK-1"), _make_task_config("TASK-2")]
        completed: set[tuple[str, int]] = {("TASK-1", 0), ("TASK-2", 1)}

        pending = orch._build_pending_trials(
            tasks,
            repeats=2,
            skip_completed=lambda task_id, trial_idx: (task_id, trial_idx) in completed,
        )

        assert pending == [("TASK-1", 1), ("TASK-2", 0)]

    def test_build_pending_trials_skip_completed_all_returns_empty(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        tasks = [_make_task_config("TASK-1")]

        pending = orch._build_pending_trials(
            tasks,
            repeats=3,
            skip_completed=lambda task_id, trial_idx: True,
        )

        assert pending == []


# ===================================================================
# _is_retryable_trajectory (static method)
# ===================================================================


@pytest.mark.unit
class TestIsRetryableTrajectory:
    """Classify retryable infrastructure failures."""

    def test_error_status_is_retryable(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        traj = _make_trajectory(status=TrialStatus.ERROR)
        assert Orchestrator._is_retryable_trajectory(traj) is True

    def test_provision_error_is_not_retryable(self) -> None:
        """Substrate provisioning failures short-circuit to non-retryable —
        ``failure_attribution`` marks them ``deterministic=True`` and
        retrying a deterministic config fault (bad compose file, missing
        manifest) burns cycles without changing the outcome."""
        from tolokaforge.core.orchestrator import Orchestrator

        traj = _make_trajectory(
            status=TrialStatus.ERROR,
            termination_reason=TerminationReason.PROVISION_ERROR,
        )
        assert Orchestrator._is_retryable_trajectory(traj) is False

    def test_timeout_status_is_retryable(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        traj = _make_trajectory(status=TrialStatus.TIMEOUT)
        assert Orchestrator._is_retryable_trajectory(traj) is True

    def test_completed_not_retryable(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        traj = _make_trajectory(status=TrialStatus.COMPLETED)
        assert Orchestrator._is_retryable_trajectory(traj) is False

    def test_failed_not_retryable(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        traj = _make_trajectory(status=TrialStatus.FAILED)
        assert Orchestrator._is_retryable_trajectory(traj) is False

    @pytest.mark.parametrize(
        "reason",
        [
            TerminationReason.RATE_LIMIT,
            TerminationReason.API_ERROR,
            TerminationReason.TIMEOUT,
            TerminationReason.ERROR,
        ],
    )
    def test_retryable_termination_reasons(self, reason: TerminationReason) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        traj = _make_trajectory(
            status=TrialStatus.COMPLETED,
            termination_reason=reason,
        )
        assert Orchestrator._is_retryable_trajectory(traj) is True

    @pytest.mark.parametrize(
        "reason",
        [
            TerminationReason.AGENT_DONE,
            TerminationReason.USER_STOP,
            TerminationReason.MAX_TURNS,
            TerminationReason.STUCK_DETECTED,
        ],
    )
    def test_non_retryable_termination_reasons(self, reason: TerminationReason) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        traj = _make_trajectory(
            status=TrialStatus.COMPLETED,
            termination_reason=reason,
        )
        assert Orchestrator._is_retryable_trajectory(traj) is False

    def test_completed_no_termination_reason(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        traj = _make_trajectory(status=TrialStatus.COMPLETED, termination_reason=None)
        assert Orchestrator._is_retryable_trajectory(traj) is False


# ===================================================================
# _cleanup_runner_state_for_retry (issue #132)
# ===================================================================


@pytest.mark.unit
class TestCleanupRunnerStateForRetry:
    """Discard the prior attempt's runner-side trial registration before retry.

    Without this, ``RegisterTrial`` on the second attempt fails with
    ``Trial 'X' already exists`` and every retry burns on the same error.
    """

    def _make_orchestrator(self) -> Any:
        from tolokaforge.core.orchestrator import Orchestrator

        return Orchestrator(_make_run_config())

    def test_calls_cleanup_with_canonical_trial_id(self) -> None:
        orch = self._make_orchestrator()
        runtime = MagicMock()
        runtime.cleanup_trial.return_value = {"success": True, "error": None}

        orch._cleanup_runner_state_for_retry(runtime, "TASK-001", 3)

        runtime.cleanup_trial.assert_called_once_with("TASK-001:3")

    def test_cleanup_exception_is_swallowed(self) -> None:
        """A stale runner connection must not block the retry attempt itself.

        Re-registration will surface a clearer error if the state is genuinely
        unrecoverable, but a transient cleanup failure must not promote a
        retryable failure into a permanent one.
        """
        orch = self._make_orchestrator()
        runtime = MagicMock()
        runtime.cleanup_trial.side_effect = RuntimeError("connection lost")

        orch._cleanup_runner_state_for_retry(runtime, "TASK-001", 0)  # must not raise

    def test_cleanup_non_success_is_swallowed(self) -> None:
        orch = self._make_orchestrator()
        runtime = MagicMock()
        runtime.cleanup_trial.return_value = {
            "success": False,
            "error": "DB unreachable",
        }

        orch._cleanup_runner_state_for_retry(runtime, "TASK-001", 0)  # must not raise


# ===================================================================
# _collect_existing_cost (static method)
# ===================================================================


@pytest.mark.unit
class TestCollectExistingCost:
    """Aggregate already-recorded trial cost from output artifacts."""

    def test_no_trials_dir(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        assert Orchestrator._collect_existing_cost(tmp_path) == 0.0

    def test_empty_trials_dir(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        (tmp_path / "trials").mkdir()
        assert Orchestrator._collect_existing_cost(tmp_path) == 0.0

    def test_sums_metrics_files(self, tmp_path: Path) -> None:
        import yaml

        from tolokaforge.core.orchestrator import Orchestrator

        trials_root = tmp_path / "trials"
        for task_id, trial_idx, cost in [("T1", 0, 0.05), ("T1", 1, 0.03), ("T2", 0, 0.02)]:
            trial_dir = trials_root / task_id / str(trial_idx)
            trial_dir.mkdir(parents=True)
            (trial_dir / "metrics.yaml").write_text(yaml.dump({"cost_usd": cost}))

        total = Orchestrator._collect_existing_cost(tmp_path)
        assert abs(total - 0.10) < 1e-9

    def test_handles_missing_cost_field(self, tmp_path: Path) -> None:
        import yaml

        from tolokaforge.core.orchestrator import Orchestrator

        trial_dir = tmp_path / "trials" / "T1" / "0"
        trial_dir.mkdir(parents=True)
        (trial_dir / "metrics.yaml").write_text(yaml.dump({"latency_s": 5.0}))

        assert Orchestrator._collect_existing_cost(tmp_path) == 0.0

    def test_handles_corrupt_yaml(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        trial_dir = tmp_path / "trials" / "T1" / "0"
        trial_dir.mkdir(parents=True)
        (trial_dir / "metrics.yaml").write_text("{{{{invalid yaml")

        # Should not raise — corrupt files are silently skipped
        assert Orchestrator._collect_existing_cost(tmp_path) == 0.0

    def test_handles_none_cost(self, tmp_path: Path) -> None:
        import yaml

        from tolokaforge.core.orchestrator import Orchestrator

        trial_dir = tmp_path / "trials" / "T1" / "0"
        trial_dir.mkdir(parents=True)
        (trial_dir / "metrics.yaml").write_text(yaml.dump({"cost_usd": None}))

        assert Orchestrator._collect_existing_cost(tmp_path) == 0.0


# ===================================================================
# _build_system_prompt
# ===================================================================


@pytest.mark.unit
class TestBuildSystemPrompt:
    """``InProcessConductor._build_system_prompt`` priority resolution."""

    def _make_orchestrator(self) -> Any:
        from tolokaforge.core.conductor import InProcessConductor

        config = _make_run_config()
        conductor = InProcessConductor(
            adapter=MagicMock(),
            artifact_writer=MagicMock(),
            config=config,
            logger=MagicMock(),
            verbose=False,
            strict=False,
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            trial_grader=MagicMock(),
            output_dir=Path("/tmp"),
            request_limiter=MagicMock(),
        )
        return conductor

    def test_inline_agent_system_prompt(self) -> None:
        orch = self._make_orchestrator()
        task = _make_task_config(
            policies={"agent_system_prompt": "You are a special assistant."},
        )
        result = orch._build_system_prompt(task, [], Path("/fake/dir"))
        assert result == "You are a special assistant."

    def test_adapter_based_prompt(self) -> None:
        orch = self._make_orchestrator()
        orch.adapter.get_system_prompt.return_value = "Adapter policy content"
        task = _make_task_config(system_prompt="__adapter__")
        result = orch._build_system_prompt(task, [], Path("/fake/dir"))
        assert "Adapter policy content" in result
        assert "<policy>" in result

    def test_adapter_prompt_none_fallthrough(self) -> None:
        """When adapter returns None, should fall through to default."""
        orch = self._make_orchestrator()
        orch.adapter.get_system_prompt.return_value = None
        task = _make_task_config(system_prompt="__adapter__")
        result = orch._build_system_prompt(task, [], Path("/fake/dir"))
        assert result == "You are a helpful assistant."

    def test_system_prompt_file(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator()
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Custom domain prompt here.")
        task = _make_task_config(system_prompt="prompt.md")
        result = orch._build_system_prompt(task, [], tmp_path)
        assert "Custom domain prompt here." in result

    def test_default_fallback(self) -> None:
        orch = self._make_orchestrator()
        task = _make_task_config(system_prompt=None)
        result = orch._build_system_prompt(task, [], Path("/nonexistent"))
        assert result == "You are a helpful assistant."

    def test_main_policy_with_additional(self, tmp_path: Path) -> None:
        """Legacy main_policy.md + additional system prompt file structure."""
        orch = self._make_orchestrator()
        # Setup directory structure: task is in a subdirectory
        task_dir = tmp_path / "tasks" / "TASK-001"
        task_dir.mkdir(parents=True)
        # main_policy.md one level up
        (tmp_path / "tasks" / "main_policy.md").write_text("Main policy content.")
        (tmp_path / "tasks" / "additional_policy.md").write_text("Additional policy content.")
        task = _make_task_config(system_prompt="additional_policy.md")
        result = orch._build_system_prompt(task, [], task_dir)
        assert "Main policy content." in result
        assert "Additional policy content." in result
        assert "<main_policy>" in result


# ===================================================================
# _generate_reports
# ===================================================================


@pytest.mark.unit
class TestGenerateReports:
    """Report generation aggregation logic."""

    def test_no_results_logs_warning(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.results = []
        # Should not raise even with no results
        orch._generate_reports(Path("/tmp/fake"))

    def test_generates_aggregate_files(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [_make_task_config("T1"), _make_task_config("T2")]

        orch.results = [
            _make_trajectory("T1", 0, score=1.0, binary_pass=True, cost=0.01),
            _make_trajectory("T1", 1, score=0.0, binary_pass=False, cost=0.02),
            _make_trajectory("T2", 0, score=1.0, binary_pass=True, cost=0.01),
        ]

        orch._generate_reports(tmp_path)

        # Check generated files
        assert (tmp_path / "aggregate.json").exists()
        assert (tmp_path / "per_task_metrics.json").exists()
        assert (tmp_path / "metadata_slices.json").exists()
        assert (tmp_path / "failure_attribution.json").exists()

    def test_aggregate_content(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [_make_task_config("T1")]

        orch.results = [
            _make_trajectory("T1", 0, score=1.0, binary_pass=True, cost=0.05),
            _make_trajectory("T1", 1, score=0.5, binary_pass=False, cost=0.03),
        ]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "aggregate.json") as f:
            agg = json.load(f)

        assert agg["total_trials"] == 2
        assert agg["total_tasks"] == 1
        assert "avg_latency_s" in agg
        assert "avg_turns" in agg

    def test_per_task_metrics_content(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [_make_task_config("T1", category="tool_use")]
        orch.results = [
            _make_trajectory("T1", 0, score=1.0, binary_pass=True),
        ]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "per_task_metrics.json") as f:
            per_task = json.load(f)

        assert len(per_task) == 1
        assert per_task[0]["task_id"] == "T1"
        assert per_task[0]["benchmark_type"] == "tool_use"

    def test_metadata_slices_by_category(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [
            _make_task_config("T1", category="tool_use"),
            _make_task_config("T2", category="browser"),
        ]
        orch.results = [
            _make_trajectory("T1", 0, score=1.0, binary_pass=True),
            _make_trajectory("T2", 0, score=0.5, binary_pass=False),
        ]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "metadata_slices.json") as f:
            slices = json.load(f)

        assert "tool_use" in slices["by_benchmark_type"]
        assert "browser" in slices["by_benchmark_type"]

    def test_metadata_slices_by_tags(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [
            _make_task_config(
                "T1",
                metadata=TaskMetadata(tags=["search", "api"]),
            ),
        ]
        orch.results = [_make_trajectory("T1", 0)]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "metadata_slices.json") as f:
            slices = json.load(f)

        assert "search" in slices["by_tag"]
        assert "api" in slices["by_tag"]

    def test_failure_attribution_file(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [_make_task_config("T1")]
        orch.results = [
            _make_trajectory(
                "T1",
                0,
                score=0.0,
                binary_pass=False,
                status=TrialStatus.ERROR,
                termination_reason=TerminationReason.ERROR,
            ),
        ]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "failure_attribution.json") as f:
            fa = json.load(f)

        assert "summary" in fa
        assert "failures" in fa

    def test_run_aggregate_writer_kwarg_swaps_writer(self, tmp_path: Path) -> None:
        """``OrchestratorDeps.run_aggregate_writer`` accepts any
        :class:`RunAggregateWriter` impl and routes the four aggregates
        through it instead of the default disk-backed writer."""
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
        from tolokaforge.core.output.aggregates import InMemoryAggregateWriter

        writer = InMemoryAggregateWriter()
        orch = Orchestrator(_make_run_config(), deps=OrchestratorDeps(run_aggregate_writer=writer))
        orch.tasks = [_make_task_config("T1")]
        orch.results = [_make_trajectory("T1", 0, score=1.0, binary_pass=True)]

        orch._generate_reports(tmp_path)

        # No JSON files on disk — the in-memory writer captured them instead.
        assert not (tmp_path / "aggregate.json").exists()
        assert not (tmp_path / "per_task_metrics.json").exists()
        bundle = writer.runs[tmp_path]
        assert bundle.aggregate is not None
        assert bundle.aggregate["schema_version"] == 1
        assert bundle.per_task_metrics is not None
        assert len(bundle.per_task_metrics) == 1
        assert bundle.metadata_slices is not None
        assert bundle.failure_attribution is not None
        assert bundle.failure_attribution.keys() == {"summary", "failures"}

    def test_empty_results_does_not_invoke_writer(self, tmp_path: Path) -> None:
        """The empty-results early-return guard runs before the writer call,
        so no aggregates are recorded when the run produced zero trials."""
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
        from tolokaforge.core.output.aggregates import InMemoryAggregateWriter

        writer = InMemoryAggregateWriter()
        orch = Orchestrator(_make_run_config(), deps=OrchestratorDeps(run_aggregate_writer=writer))
        orch.results = []

        orch._generate_reports(tmp_path)

        assert writer.runs == {}


# ===================================================================
# _create_adapter
# ===================================================================


@pytest.mark.unit
class TestCreateAdapter:
    """Adapter creation based on configuration."""

    @patch("tolokaforge.core.orchestrator.get_adapter")
    def test_default_native_adapter(self, mock_get_adapter: MagicMock) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        mock_adapter = MagicMock()
        mock_get_adapter.return_value = mock_adapter

        config = _make_run_config()
        orch = Orchestrator(config)
        result = orch._create_adapter()

        mock_get_adapter.assert_called_once()
        call_args = mock_get_adapter.call_args
        assert call_args[0][0] == "native"  # adapter_type
        assert result is mock_adapter

    @patch("tolokaforge.core.orchestrator.get_adapter")
    def test_adapter_with_harness_config(self, mock_get_adapter: MagicMock) -> None:
        from tolokaforge.core.models import HarnessAdapterConfig
        from tolokaforge.core.orchestrator import Orchestrator

        mock_adapter = MagicMock()
        mock_get_adapter.return_value = mock_adapter

        config = _make_run_config(
            evaluation=EvaluationConfig(
                output_dir="/tmp/out",
                harness_adapter=HarnessAdapterConfig(
                    type="tau",
                    params={"domain": "retail"},
                ),
            )
        )
        orch = Orchestrator(config)
        orch._create_adapter()

        call_args = mock_get_adapter.call_args
        assert call_args[0][0] == "tau"
        # params should include domain and tasks_glob
        assert call_args[0][1]["domain"] == "retail"
        assert "tasks_glob" in call_args[0][1]

    @patch("tolokaforge.core.orchestrator.get_adapter")
    def test_adapter_tasks_glob_propagated(self, mock_get_adapter: MagicMock) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        mock_get_adapter.return_value = MagicMock()

        config = _make_run_config(
            evaluation=EvaluationConfig(
                output_dir="/tmp/out",
                tasks_glob="custom/**/task.yaml",
            )
        )
        orch = Orchestrator(config)
        orch._create_adapter()

        call_args = mock_get_adapter.call_args
        assert call_args[0][1]["tasks_glob"] == "custom/**/task.yaml"

    def test_default_adapter_requires_no_extra_stack_kwargs(self) -> None:
        from tolokaforge.adapters.base import DockerStackRequirements
        from tolokaforge.adapters.native import NativeAdapter

        adapter = NativeAdapter({"tasks_glob": "tasks/**/task.yaml"})
        reqs = adapter.docker_stack_requirements()

        assert isinstance(reqs, DockerStackRequirements)
        assert reqs.to_core_stack_kwargs() == {}


# ===================================================================
# Report grouping logic (trajectories grouped by task)
# ===================================================================


@pytest.mark.unit
class TestTrajectoryGrouping:
    """Verify trajectories are correctly grouped by task_id in reports."""

    def test_multiple_trials_per_task(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [_make_task_config("T1")]
        orch.results = [
            _make_trajectory("T1", 0, score=1.0, binary_pass=True),
            _make_trajectory("T1", 1, score=0.0, binary_pass=False),
            _make_trajectory("T1", 2, score=1.0, binary_pass=True),
        ]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "per_task_metrics.json") as f:
            per_task = json.load(f)

        assert len(per_task) == 1
        assert per_task[0]["task_id"] == "T1"
        assert per_task[0]["total_trials"] == 3

    def test_multiple_tasks_grouped(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [_make_task_config("T1"), _make_task_config("T2"), _make_task_config("T3")]
        orch.results = [
            _make_trajectory("T1", 0),
            _make_trajectory("T2", 0),
            _make_trajectory("T3", 0),
            _make_trajectory("T1", 1),
        ]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "per_task_metrics.json") as f:
            per_task = json.load(f)

        task_ids = {m["task_id"] for m in per_task}
        assert task_ids == {"T1", "T2", "T3"}


# ===================================================================
# Metadata slicing edge cases
# ===================================================================


@pytest.mark.unit
class TestMetadataSliceEdgeCases:
    """Edge cases in metadata-sliced aggregate reports."""

    def test_missing_metadata_defaults_to_unknown(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        # Task with no explicit category or metadata
        orch.tasks = [_make_task_config("T1", category="")]
        orch.results = [_make_trajectory("T1", 0)]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "metadata_slices.json") as f:
            slices = json.load(f)

        # Empty category should still be present as a key in by_benchmark_type
        assert len(slices["by_benchmark_type"]) >= 1

    def test_complexity_slicing(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [
            _make_task_config("T1", metadata=TaskMetadata(complexity="easy")),
            _make_task_config("T2", metadata=TaskMetadata(complexity="hard")),
        ]
        orch.results = [
            _make_trajectory("T1", 0, score=1.0, binary_pass=True),
            _make_trajectory("T2", 0, score=0.0, binary_pass=False),
        ]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "metadata_slices.json") as f:
            slices = json.load(f)

        assert "easy" in slices["by_complexity"]
        assert "hard" in slices["by_complexity"]

    def test_expected_failure_modes_slicing(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [
            _make_task_config(
                "T1", metadata=TaskMetadata(expected_failure_modes=["hallucination", "tool_misuse"])
            ),
        ]
        orch.results = [_make_trajectory("T1", 0)]

        orch._generate_reports(tmp_path)

        with open(tmp_path / "metadata_slices.json") as f:
            slices = json.load(f)

        assert "hallucination" in slices["by_expected_failure_mode"]
        assert "tool_misuse" in slices["by_expected_failure_mode"]


# ===================================================================
# RunConfig construction
# ===================================================================


@pytest.mark.unit
class TestRunConfigConstruction:
    """RunConfig from various input sources."""

    def test_minimal_config(self) -> None:
        config = _make_run_config()
        assert config.orchestrator.workers == 1
        assert config.orchestrator.repeats == 1
        assert config.evaluation.output_dir == "/tmp/test_output"

    def test_custom_workers_and_repeats(self) -> None:
        config = _make_run_config(
            orchestrator=OrchestratorConfig(workers=4, repeats=3, auto_start_services=False)
        )
        assert config.orchestrator.workers == 4
        assert config.orchestrator.repeats == 3

    def test_budget_limit(self) -> None:
        config = _make_run_config(
            orchestrator=OrchestratorConfig(
                max_budget_usd=10.0,
                auto_start_services=False,
            )
        )
        assert config.orchestrator.max_budget_usd == 10.0

    def test_default_timeouts(self) -> None:
        config = _make_run_config()
        assert config.orchestrator.timeouts.turn_s == 60
        assert config.orchestrator.timeouts.episode_s == 1800

    def test_multiple_model_roles(self) -> None:
        config = _make_run_config(
            models={
                "agent": ModelConfig(provider="openai", name="gpt-4"),
                "user": ModelConfig(provider="anthropic", name="claude-3-sonnet", temperature=0.2),
            }
        )
        assert "agent" in config.models
        assert "user" in config.models
        assert config.models["user"].temperature == 0.2

    def test_queue_backend_config(self) -> None:
        config = _make_run_config(
            orchestrator=OrchestratorConfig(
                queue_backend="sqlite",
                max_attempt_retries=3,
                auto_start_services=False,
            )
        )
        assert config.orchestrator.queue_backend == "sqlite"
        assert config.orchestrator.max_attempt_retries == 3


# ---------------------------------------------------------------------------
# _serialize_model_config
# ---------------------------------------------------------------------------


class TestSerializeModelConfig:
    """Test model config serialization for trial output.

    The orchestrator crashed with ``'dict' object has no attribute 'agent'``
    because eval-orchestrator shard configs produce ``config.models`` as a
    plain dict instead of a ``dict[str, ModelConfig]`` that Pydantic coerces.
    ``_serialize_model_config()`` must handle both.
    """

    def test_serialize_with_pydantic_model_configs(self) -> None:
        """When config.models values are ModelConfig instances, serialization works."""
        orch = MagicMock()
        orch.config.models = {
            "agent": ModelConfig(provider="openrouter", name="openai/gpt-5.4"),
            "user": ModelConfig(
                provider="openrouter", name="anthropic/claude-sonnet-4.6", temperature=0.2
            ),
        }
        from tolokaforge.core.conductor import InProcessConductor

        result = InProcessConductor._serialize_model_config(orch)

        assert isinstance(result, dict)
        assert result["agent"]["provider"] == "openrouter"
        assert result["agent"]["name"] == "openai/gpt-5.4"
        assert result["user"]["name"] == "anthropic/claude-sonnet-4.6"
        assert result["user"]["temperature"] == 0.2

    def test_serialize_with_raw_dicts(self) -> None:
        """When config.models values are raw dicts (eval-orchestrator shards), no crash."""
        orch = MagicMock()
        orch.config.models = {
            "agent": {"provider": "openrouter", "name": "openai/gpt-5.4", "temperature": 0.6},
            "user": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"},
        }
        from tolokaforge.core.conductor import InProcessConductor

        result = InProcessConductor._serialize_model_config(orch)

        assert isinstance(result, dict)
        assert result["agent"]["name"] == "openai/gpt-5.4"
        assert result["agent"]["temperature"] == 0.6
        assert result["user"]["name"] == "anthropic/claude-sonnet-4.6"

    def test_serialize_without_user(self) -> None:
        """When user model is absent, result['user'] is None."""
        orch = MagicMock()
        orch.config.models = {
            "agent": {"provider": "openrouter", "name": "openai/gpt-5.4"},
        }
        from tolokaforge.core.conductor import InProcessConductor

        result = InProcessConductor._serialize_model_config(orch)

        assert result["agent"]["name"] == "openai/gpt-5.4"
        assert result["user"] is None

    def test_serialize_mixed_dict_and_pydantic(self) -> None:
        """When agent is a raw dict but user is a ModelConfig, both are serialized."""
        orch = MagicMock()
        orch.config.models = {
            "agent": {"provider": "openrouter", "name": "openai/gpt-5.4"},
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        }
        from tolokaforge.core.conductor import InProcessConductor

        result = InProcessConductor._serialize_model_config(orch)

        assert isinstance(result["agent"], dict)
        assert result["agent"]["name"] == "openai/gpt-5.4"
        assert isinstance(result["user"], dict)
        assert result["user"]["name"] == "anthropic/claude-sonnet-4.6"

    def test_serialize_records_judge_role_with_resolved_fingerprint(self) -> None:
        """A configured judge role is recorded in the bundle like agent/user —
        carrying provider/name and a ``resolved`` preset fingerprint — so every
        grade bundle records which judge produced it."""
        orch = MagicMock()
        orch.config.models = {
            "agent": ModelConfig(provider="openrouter", name="openai/gpt-5.4"),
            "judge": ModelConfig(
                provider="openrouter", name="openai/gpt-4.1-mini", temperature=0.0
            ),
        }
        from tolokaforge.core.conductor import InProcessConductor

        result = InProcessConductor._serialize_model_config(orch)

        assert result["judge"] is not None
        assert result["judge"]["provider"] == "openrouter"
        assert result["judge"]["name"] == "openai/gpt-4.1-mini"
        # The resolved preset fingerprint must be present (same shape as agent/user).
        assert "resolved" in result["judge"]
        assert "effective_preset" in result["judge"]["resolved"]

    def test_serialize_judge_is_null_when_unconfigured(self) -> None:
        """No judge role configured → ``judge`` key present and null (consistent
        with how an absent user is recorded), with no resolved fingerprint."""
        orch = MagicMock()
        orch.config.models = {
            "agent": ModelConfig(provider="openrouter", name="openai/gpt-5.4"),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        }
        from tolokaforge.core.conductor import InProcessConductor

        result = InProcessConductor._serialize_model_config(orch)

        assert result["judge"] is None

    def test_serialize_judge_arg_overrides_and_is_recorded(self) -> None:
        """The explicit ``judge_config`` arg (the resolved run-level judge passed
        from the trial build) is recorded, mirroring the agent/user arg path."""
        orch = MagicMock()
        orch.config.models = {
            "agent": ModelConfig(provider="openrouter", name="openai/gpt-5.4"),
            "judge": ModelConfig(provider="openrouter", name="openai/gpt-4.1-mini"),
        }
        from tolokaforge.core.conductor import InProcessConductor

        judge = ModelConfig(provider="openrouter", name="openai/gpt-4.1-mini", temperature=0.0)
        result = InProcessConductor._serialize_model_config(orch, judge_config=judge)

        assert result["judge"]["name"] == "openai/gpt-4.1-mini"
        assert "resolved" in result["judge"]


# ===================================================================
# _build_env_endpoints (free function)
# ===================================================================


@pytest.mark.unit
class TestBuildEnvEndpoints:
    """The producer of ``TrialSpec.env_endpoints`` — pure resolver that
    reads the orchestrator's known runner address and the runner-parity
    env vars (``DB_SERVICE_URL`` / ``RAG_SERVICE_URL``). No service-stack
    introspection; suitable for both auto-start and external (worker) runs.
    """

    def test_defaults_match_docker_stack_injection(self, monkeypatch: Any) -> None:
        """With no env override, ``db_url`` falls back to the URL the docker
        stack injects into the runner container — so the wire value matches
        what the runner already sees today."""
        from tolokaforge.core.shared_stack_runtime import _build_env_endpoints

        monkeypatch.delenv("DB_SERVICE_URL", raising=False)
        monkeypatch.delenv("RAG_SERVICE_URL", raising=False)

        endpoints = _build_env_endpoints("executor:50051")

        assert endpoints.db_url == "http://tolokaforge-db-service:8000"
        assert endpoints.rag_url is None
        assert endpoints.runner_url == "http://executor:50051"

    def test_env_overrides_take_precedence(self, monkeypatch: Any) -> None:
        from tolokaforge.core.shared_stack_runtime import _build_env_endpoints

        monkeypatch.setenv("DB_SERVICE_URL", "http://db.example:8000")
        monkeypatch.setenv("RAG_SERVICE_URL", "http://rag.example:8001")

        endpoints = _build_env_endpoints("runner.example:50051")

        assert endpoints.db_url == "http://db.example:8000"
        assert endpoints.rag_url == "http://rag.example:8001"
        assert endpoints.runner_url == "http://runner.example:50051"

    def test_runner_address_with_scheme_passes_through(self) -> None:
        from tolokaforge.core.shared_stack_runtime import _build_env_endpoints

        endpoints = _build_env_endpoints("http://runner.example:50051")
        assert endpoints.runner_url == "http://runner.example:50051"

    def test_runner_address_https_passes_through(self) -> None:
        from tolokaforge.core.shared_stack_runtime import _build_env_endpoints

        endpoints = _build_env_endpoints("https://runner.example:50051")
        assert endpoints.runner_url == "https://runner.example:50051"


# ===================================================================
# Up-front judge-model gate (fail loud, no hidden default)
# ===================================================================


def _task_description_with_judge(task_id: str, *, has_judge: bool):
    """Build a TaskDescription whose grading does / does not declare an llm_judge."""
    from tolokaforge.runner.models import (
        GradingConfig,
        LLMJudgeConfig,
        Rubric,
        TaskDescription,
    )

    llm_judge = None
    if has_judge:
        llm_judge = LLMJudgeConfig(
            rubric=Rubric(
                criteria=[{"id": "c", "description": "d", "kind": "binary", "weight": 1.0}]
            )
        )
    return TaskDescription(
        task_id=task_id,
        name=task_id,
        category="test",
        description="d",
        adapter_type="native",
        system_prompt="sys",
        grading=GradingConfig(llm_judge=llm_judge),
    )


def _orchestrator_with_tasks(config: RunConfig, judge_flags: dict[str, bool]):
    """Construct an Orchestrator with stubbed tasks + adapter for the gate test."""
    from tolokaforge.core.orchestrator import Orchestrator

    orch = Orchestrator(config)
    orch.tasks = [_make_task_config(tid) for tid in judge_flags]
    adapter = MagicMock()
    adapter.to_task_description.side_effect = lambda tid: _task_description_with_judge(
        tid, has_judge=judge_flags[tid]
    )
    orch.adapter = adapter
    return orch


@pytest.mark.unit
class TestJudgeModelGate:
    """The run must abort up front when a task needs an llm_judge but none is set.

    This is the load-bearing relocation guarantee: the judge model lives at the
    run level (models.judge), there is no default and no fallback to the agent
    model, and the failure surfaces BEFORE any trial executes (in the gate the
    run loop calls before scheduling).
    """

    def test_missing_judge_model_aborts_and_names_offending_task(self) -> None:
        config = _make_run_config()  # models has agent only, no judge
        orch = _orchestrator_with_tasks(config, {"TASK-needs-judge": True, "TASK-no-judge": False})

        with pytest.raises(ValueError) as excinfo:
            orch._resolve_judge_config()

        message = str(excinfo.value)
        assert "TASK-needs-judge" in message
        # The non-judge task must NOT be flagged.
        assert "TASK-no-judge" not in message
        assert "models.judge" in message

    def test_judge_model_present_returns_it_no_abort(self) -> None:
        judge = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")
        config = _make_run_config(
            models={"agent": ModelConfig(provider="openai", name="gpt-4"), "judge": judge}
        )
        orch = _orchestrator_with_tasks(config, {"TASK-needs-judge": True})

        assert orch._resolve_judge_config() == judge

    def test_no_judge_task_no_judge_model_is_allowed(self) -> None:
        config = _make_run_config()  # no judge model
        orch = _orchestrator_with_tasks(config, {"TASK-no-judge": False})

        # No task needs a judge, so the absence of models.judge is fine.
        assert orch._resolve_judge_config() is None

    def test_run_worker_aborts_before_any_trial_dispatch(self, tmp_path: Path) -> None:
        """Pin the gate's PLACEMENT: the abort happens before a single trial is
        dispatched. The conductor factory is wired with an
        :class:`InMemoryConductor` whose call log records every ``run()`` invocation;
        if a refactor moved the gate below the scheduling loop, the conductor's
        ``call_log.runs`` would be non-empty and this test would fail.
        """
        from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps

        config = _make_run_config()  # agent only, no judge model

        # Build a conductor factory that returns a single InMemoryConductor so
        # we can assert on its call_log after the run_worker raises.
        recording_conductor = InMemoryConductor()

        def conductor_factory(_ctx: ConductorContext) -> InMemoryConductor:
            return recording_conductor

        orch = Orchestrator(config, deps=OrchestratorDeps(conductor_factory=conductor_factory))
        orch.tasks = [_make_task_config("TASK-needs-judge")]
        adapter = MagicMock()
        adapter.to_task_description.side_effect = lambda tid: _task_description_with_judge(
            tid, has_judge=True
        )
        orch.adapter = adapter

        with patch(
            "tolokaforge.core.shared_stack_runtime.SharedStackRuntimeBackend"
        ) as runtime_backend:
            with pytest.raises(ValueError, match="TASK-needs-judge"):
                orch.run_worker(tmp_path)

        # No trial was dispatched, and the run never reached Docker setup.
        assert recording_conductor.call_log.runs == []
        runtime_backend.assert_not_called()

    def test_run_worker_requires_engine_run_state(self, tmp_path: Path) -> None:
        """``run_worker`` reads the canonical ``run_id`` from
        ``engine_run_state.json``. Absence means the operator skipped
        ``tolokaforge prepare`` — fail loud rather than silently making
        up an identifier."""
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.tasks = [_make_task_config("TASK-001")]
        adapter = MagicMock()
        adapter.to_task_description.side_effect = lambda tid: _task_description_with_judge(
            tid, has_judge=False
        )
        orch.adapter = adapter

        with pytest.raises(RuntimeError, match="engine_run_state.json"):
            orch.run_worker(tmp_path)


# ===================================================================
# Resume canonicalisation
# ===================================================================


@pytest.mark.unit
class TestResumeCanonicalisation:
    """``_canonicalise_resumed_run_id`` heals a legacy ``RunState`` whose
    ``run_id`` disagrees with the directory it lives in. The directory
    basename is the disk fact; the state file is rewritten so every
    surface (engine_run_state.json, TrialSpec.run_id) agrees.
    """

    def test_canonicalises_when_run_id_disagrees(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator
        from tolokaforge.core.resume import RunStateManager

        output_dir = tmp_path / "coding_example_20260626_154233"
        output_dir.mkdir()
        state_manager = RunStateManager(output_dir)
        # Pre-seed a state file with the legacy timestamp-only run_id.
        state_manager.initialize_run(
            run_id="20260626_154233",
            config_path="config.yaml",
            task_ids=["TASK-001"],
            repeats=1,
        )

        orch = Orchestrator(_make_run_config())
        orch.state_manager = state_manager
        loaded = state_manager.load_state()
        assert loaded is not None
        assert loaded.run_id == "20260626_154233"

        orch._canonicalise_resumed_run_id(loaded, "coding_example_20260626_154233")

        # In-memory state is updated.
        assert loaded.run_id == "coding_example_20260626_154233"
        # And the file on disk is rewritten.
        reloaded = state_manager.load_state()
        assert reloaded is not None
        assert reloaded.run_id == "coding_example_20260626_154233"

    def test_no_op_when_run_id_matches(self, tmp_path: Path) -> None:
        """When the loaded ``run_id`` already matches the canonical one
        the state file is not rewritten — verified by comparing the
        ``last_updated`` timestamp."""
        from tolokaforge.core.orchestrator import Orchestrator
        from tolokaforge.core.resume import RunStateManager

        output_dir = tmp_path / "coding_example_20260626_154233"
        output_dir.mkdir()
        state_manager = RunStateManager(output_dir)
        state_manager.initialize_run(
            run_id="coding_example_20260626_154233",
            config_path="config.yaml",
            task_ids=["TASK-001"],
            repeats=1,
        )

        orch = Orchestrator(_make_run_config())
        orch.state_manager = state_manager
        loaded = state_manager.load_state()
        assert loaded is not None
        last_updated_before = loaded.last_updated

        orch._canonicalise_resumed_run_id(loaded, "coding_example_20260626_154233")

        # Same value; no rewrite — last_updated unchanged.
        reloaded = state_manager.load_state()
        assert reloaded is not None
        assert reloaded.last_updated == last_updated_before


# ===================================================================
# prepare_run idempotency
# ===================================================================


@pytest.mark.unit
class TestPrepareRunIdempotency:
    """``prepare_run`` must be safe to re-run on the same output_dir
    without double-enqueuing trials. Re-running with ``reset_queue=True``
    clears the existing queue and re-enqueues from scratch.
    """

    def _make_orch(self, tmp_path: Path):
        from tolokaforge.core.orchestrator import Orchestrator

        orch = Orchestrator(_make_run_config())
        orch.tasks = [_make_task_config("TASK-001"), _make_task_config("TASK-002")]
        adapter = MagicMock()
        adapter.to_task_description.side_effect = lambda tid: _task_description_with_judge(
            tid, has_judge=False
        )
        orch.adapter = adapter
        return orch

    def test_second_call_skips_enqueue(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "example_20260626_154233"
        orch = self._make_orch(tmp_path)

        first = orch.prepare_run(output_dir)
        assert first["queued_attempts"] == 2  # two tasks × repeats=1
        first_total = first["queue_counts"]["total"]
        assert first_total == 2

        second = orch.prepare_run(output_dir)
        # The second call MUST be a no-op — no new attempts enqueued.
        assert second["queued_attempts"] == 0
        # And the queue's total population is unchanged.
        assert second["queue_counts"]["total"] == first_total

    def test_reset_queue_re_enqueues_from_scratch(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "example_20260626_154233"
        orch = self._make_orch(tmp_path)

        first = orch.prepare_run(output_dir)
        assert first["queued_attempts"] == 2

        second = orch.prepare_run(output_dir, reset_queue=True)
        # With reset_queue, the second call clears and re-enqueues — same
        # total as the first call (not doubled).
        assert second["queued_attempts"] == 2
        assert second["queue_counts"]["total"] == 2


# ===================================================================
# Orchestrator.run() — output_dir basename guard
# ===================================================================


@pytest.mark.unit
class TestRunOutputDirBasenameGuard:
    """``run()`` rejects an ``evaluation.output_dir`` whose basename is
    empty (``.``, ``/``, trailing slash). Mirrors the guard already in
    ``prepare_run`` so both entry points fail loud on the pathological
    inputs that would otherwise persist a degenerate run_id.
    """

    def test_empty_basename_dot_is_rejected(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config(evaluation=EvaluationConfig(output_dir="."))
        orch = Orchestrator(config)

        with pytest.raises(ValueError, match="non-empty basename"):
            orch.run()

    def test_empty_basename_root_is_rejected(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config(evaluation=EvaluationConfig(output_dir="/"))
        orch = Orchestrator(config)

        with pytest.raises(ValueError, match="non-empty basename"):
            orch.run()


# ===================================================================
# Orchestrator(deps=OrchestratorDeps(runtime_backend=...))
# ===================================================================


@pytest.mark.unit
class TestRuntimeBackendInjection:
    """``OrchestratorDeps.runtime_backend`` accepts any
    :class:`RuntimeBackend` impl. The orchestrator stores the injected
    instance and uses it instead of constructing
    :class:`SharedStackRuntimeBackend`.
    """

    def test_default_is_none(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        orch = Orchestrator(_make_run_config())
        assert orch._injected_runtime_backend is None

    def test_stores_injected_instance(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
        from tolokaforge.core.runtime import InMemoryRuntimeBackend

        backend = InMemoryRuntimeBackend()
        orch = Orchestrator(_make_run_config(), deps=OrchestratorDeps(runtime_backend=backend))

        assert orch._injected_runtime_backend is backend

    def test_injection_does_not_invoke_backend_lifecycle(self) -> None:
        """Construction must not call ``connect``/``close`` on the injected
        backend — that happens only when ``run()`` / ``run_worker()`` are
        invoked. Tests that construct an Orchestrator with an in-memory
        backend rely on this to assert lifecycle from a known-empty log."""
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
        from tolokaforge.core.runtime import InMemoryRuntimeBackend

        backend = InMemoryRuntimeBackend()
        Orchestrator(_make_run_config(), deps=OrchestratorDeps(runtime_backend=backend))

        assert backend.call_log.connect_calls == []
        assert backend.call_log.close_calls == 0
        assert backend.call_log.health_check_calls == 0


# ===================================================================
# Orchestrator(deps=OrchestratorDeps(artifact_writer=...))
# ===================================================================


@pytest.fixture
def _builtin_registry_loaders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the grader/conductor loaders at their orchestrator-module binding
    to the real built-in factories.

    Keeps ``_build_conductor`` tests decoupled from installed entry-point
    metadata (no ``uv sync`` required to run them under ``-m unit``) while
    still exercising the real dispatch wiring.
    """
    from tolokaforge.core.conductor import in_process_conductor_factory
    from tolokaforge.core.trial_grader import runner_rpc_trial_grader_factory

    monkeypatch.setattr(
        "tolokaforge.core.orchestrator.load_trial_grader",
        lambda name: runner_rpc_trial_grader_factory,
    )
    monkeypatch.setattr(
        "tolokaforge.core.orchestrator.load_conductor",
        lambda name: in_process_conductor_factory,
    )


@pytest.mark.unit
@pytest.mark.usefixtures("_builtin_registry_loaders")
class TestArtifactWriterInjection:
    """``OrchestratorDeps.artifact_writer`` accepts any
    :class:`TrialArtifactWriter` impl. The orchestrator stores the
    injected instance and uses it instead of constructing
    :class:`FileArtifactWriter`.
    """

    def test_default_is_file_writer(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator
        from tolokaforge.core.output.artifacts import FileArtifactWriter

        orch = Orchestrator(_make_run_config())
        assert isinstance(orch._artifact_writer, FileArtifactWriter)

    def test_stores_injected_instance(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
        from tolokaforge.core.output.artifacts import InMemoryArtifactWriter

        writer = InMemoryArtifactWriter()
        orch = Orchestrator(_make_run_config(), deps=OrchestratorDeps(artifact_writer=writer))

        assert orch._artifact_writer is writer

    def test_injected_writer_threads_to_conductor(self, tmp_path: Path) -> None:
        """The conductor constructed by ``_build_conductor`` receives the
        same writer the orchestrator was given — not a fresh
        :class:`FileArtifactWriter`."""
        from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
        from tolokaforge.core.output.artifacts import InMemoryArtifactWriter

        writer = InMemoryArtifactWriter()
        captured: dict[str, ConductorContext] = {}

        def factory(ctx: ConductorContext) -> InMemoryConductor:
            captured["ctx"] = ctx
            return InMemoryConductor()

        orch = Orchestrator(
            _make_run_config(),
            deps=OrchestratorDeps(artifact_writer=writer, conductor_factory=factory),
        )
        orch.adapter = MagicMock()

        orch._build_conductor(
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            output_dir=tmp_path,
            request_limiter=None,
        )

        assert captured["ctx"].artifact_writer is writer


# ===================================================================
# Orchestrator(deps=OrchestratorDeps(conductor_factory=...))
# ===================================================================


@pytest.mark.unit
@pytest.mark.usefixtures("_builtin_registry_loaders")
class TestConductorInjection:
    """``OrchestratorDeps.conductor_factory`` accepts a
    ``Callable[[ConductorContext], Conductor]`` that the orchestrator
    invokes inside ``run()`` / ``run_worker()`` once the adapter and
    per-run dependencies are resolved.
    """

    def test_default_is_none(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        orch = Orchestrator(_make_run_config())
        assert orch._conductor_factory is None

    def test_stores_injected_factory(self) -> None:
        from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps

        def factory(_ctx: ConductorContext) -> InMemoryConductor:
            return InMemoryConductor()

        orch = Orchestrator(_make_run_config(), deps=OrchestratorDeps(conductor_factory=factory))
        assert orch._conductor_factory is factory

    def test_default_factory_builds_in_process_conductor(self, tmp_path: Path) -> None:
        """When no factory is injected, ``_build_conductor`` resolves the
        built-in ``in_process`` conductor and ``runner_rpc`` grader through the
        registry — yielding an :class:`InProcessConductor` holding a
        :class:`RunnerRPCTrialGrader`."""
        from tolokaforge.core.conductor import InProcessConductor
        from tolokaforge.core.orchestrator import Orchestrator
        from tolokaforge.core.trial_grader import RunnerRPCTrialGrader

        orch = Orchestrator(_make_run_config())
        orch.adapter = MagicMock()  # _build_conductor raises if adapter is unset

        conductor = orch._build_conductor(
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            output_dir=tmp_path,
            request_limiter=MagicMock(),
        )
        assert isinstance(conductor, InProcessConductor)
        assert isinstance(conductor.trial_grader, RunnerRPCTrialGrader)

    def test_build_conductor_raises_when_adapter_is_unset(self, tmp_path: Path) -> None:
        """Fail-fast: building a conductor before ``load_tasks()`` ran (so
        ``self.adapter`` is still ``None``) raises immediately, instead of
        silently propagating ``None`` into the Conductor's body where it
        would crash 600+ lines deep on ``self.adapter.get_task_dir(...)``."""
        from tolokaforge.core.orchestrator import Orchestrator

        orch = Orchestrator(_make_run_config())
        assert orch.adapter is None

        with pytest.raises(RuntimeError, match="adapter is loaded"):
            orch._build_conductor(
                agent_client=MagicMock(),
                runtime_backend=MagicMock(),
                output_dir=tmp_path,
                request_limiter=MagicMock(),
            )

    def test_injected_factory_receives_conductor_context(self, tmp_path: Path) -> None:
        """The orchestrator calls the factory with a
        :class:`ConductorContext` carrying its resolved per-run deps.
        Pinned so a future refactor that changes the dependency surface
        forces this test to update deliberately."""
        from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
        from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps

        captured: dict[str, ConductorContext] = {}

        def factory(ctx: ConductorContext) -> InMemoryConductor:
            captured["ctx"] = ctx
            return InMemoryConductor()

        orch = Orchestrator(_make_run_config(), deps=OrchestratorDeps(conductor_factory=factory))
        orch.adapter = MagicMock()

        orch._build_conductor(
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            output_dir=tmp_path,
            request_limiter=MagicMock(),
        )

        ctx = captured["ctx"]
        assert isinstance(ctx, ConductorContext)
        assert ctx.adapter is orch.adapter
        assert ctx.artifact_writer is orch._artifact_writer
        assert ctx.config is orch.config
        assert ctx.output_dir == tmp_path
        assert ctx.trial_grader is not None
