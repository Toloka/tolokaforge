"""Unit tests for tolokaforge/core/orchestrator.py — pure-logic helpers and static methods."""

from __future__ import annotations

import json
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
            cost_usd_est=cost,
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
        InitialStateConfig,
        ToolsConfig,
        UserSimulatorConfig,
    )

    defaults: dict[str, Any] = {
        "task_id": task_id,
        "name": f"Test Task {task_id}",
        "category": "tool_use",
        "description": "A test task",
        "initial_state": InitialStateConfig(),
        "tools": ToolsConfig(),
        "user_simulator": UserSimulatorConfig(mode="scripted"),
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
            (trial_dir / "metrics.yaml").write_text(yaml.dump({"cost_usd_est": cost}))

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
        (trial_dir / "metrics.yaml").write_text(yaml.dump({"cost_usd_est": None}))

        assert Orchestrator._collect_existing_cost(tmp_path) == 0.0


# ===================================================================
# _build_system_prompt
# ===================================================================


@pytest.mark.unit
class TestBuildSystemPrompt:
    """System prompt construction with various priority levels."""

    def _make_orchestrator(self) -> Any:
        from tolokaforge.core.orchestrator import Orchestrator

        config = _make_run_config()
        orch = Orchestrator(config)
        orch.adapter = MagicMock()
        return orch

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
