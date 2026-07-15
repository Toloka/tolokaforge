"""Stage-3 CLI flag surface — ``--cost-limit`` / ``--time-limit`` /
``--sample-limit`` / ``--fallback-models`` / ``--model-cost-config``.

Every test uses ``CliRunner`` against a stubbed :class:`Orchestrator`
that captures the ``OrchestratorDeps`` the CLI wires so we can assert
the flags produced the intended budget composite, agent-client factory,
and pricing overlay side effect. No real LLM, Docker, or filesystem
surfaces are touched beyond the run's own output directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.cli.main as cli_main
from tolokaforge.cli.main import cli
from tolokaforge.core.budgets import (
    CompositeBudget,
    CostBudget,
    SampleBudget,
    TimeBudget,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {
                    "agent": {"provider": "openai", "name": "gpt-4"},
                    "user": {"provider": "openai", "name": "gpt-4o"},
                },
                "evaluation": {
                    "output_dir": str(tmp_path / "out"),
                    "tasks_glob": str(tmp_path / "tasks" / "*"),
                },
                "orchestrator": {"repeats": 1, "auto_start_services": False},
                "compute": {"workers": 1},
            }
        )
    )
    return config_path


def _make_capturing_orchestrator(
    captured: dict[str, Any],
    *,
    run_return: Path,
) -> type:
    """Stub :class:`Orchestrator` that records the ``deps`` the CLI passed."""

    class _CapturingOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["deps"] = kwargs.get("deps")
            captured["config"] = args[0] if args else kwargs.get("config")
            self.tasks = [object()]

        def load_tasks(self) -> None:
            return None

        def run(self, **_: object) -> Path:
            return run_return

    return _CapturingOrchestrator


# ---------------------------------------------------------------------------
# --cost-limit / --time-limit / --sample-limit
# ---------------------------------------------------------------------------


class TestCostLimit:
    def test_flag_produces_composite_with_cost_budget(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config), "--cost-limit", "0.03"])

        assert result.exit_code == 0, result.stderr
        # --cost-limit writes to compute.max_budget_usd; effective_max_budget_usd
        # surfaces the merged value.
        assert captured["config"].effective_max_budget_usd == pytest.approx(0.03)
        budget = captured["deps"].budget
        assert isinstance(budget, CompositeBudget)
        trackers = budget.trackers
        assert len(trackers) == 1
        assert isinstance(trackers[0], CostBudget)

    def test_cli_cost_limit_beats_config_max_budget_usd(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config_path = tmp_path / "run.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "models": {"agent": {"provider": "openai", "name": "gpt-4"}},
                    "evaluation": {
                        "output_dir": str(tmp_path / "out"),
                        "tasks_glob": str(tmp_path / "t" / "*"),
                    },
                    "orchestrator": {"repeats": 1, "auto_start_services": False},
                    "compute": {"workers": 1, "max_budget_usd": 5.0},
                }
            )
        )
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(config_path), "--cost-limit", "0.10"])

        assert result.exit_code == 0, result.stderr
        assert captured["config"].effective_max_budget_usd == pytest.approx(0.10)


class TestTimeLimit:
    def test_valid_duration_produces_time_budget(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config), "--time-limit", "30m"])

        assert result.exit_code == 0, result.stderr
        budget = captured["deps"].budget
        assert isinstance(budget, CompositeBudget)
        trackers = budget.trackers
        assert len(trackers) == 1
        assert isinstance(trackers[0], TimeBudget)

    def test_bad_duration_surfaces_click_bad_parameter(
        self,
        runner: CliRunner,
        valid_config: Path,
    ) -> None:
        result = runner.invoke(
            cli, ["run", "--config", str(valid_config), "--time-limit", "not-a-duration"]
        )
        assert result.exit_code != 0
        # click.BadParameter puts the message on stderr and includes the flag name.
        assert "--time-limit" in result.stderr or "time-limit" in result.stderr


class TestSampleLimit:
    def test_flag_produces_composite_with_sample_budget(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config), "--sample-limit", "5"])

        assert result.exit_code == 0, result.stderr
        budget = captured["deps"].budget
        assert isinstance(budget, CompositeBudget)
        trackers = budget.trackers
        assert len(trackers) == 1
        assert isinstance(trackers[0], SampleBudget)


class TestComposedFlags:
    def test_all_three_limits_produce_a_three_child_composite(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(
            cli,
            [
                "run",
                "--config",
                str(valid_config),
                "--cost-limit",
                "0.5",
                "--time-limit",
                "1h",
                "--sample-limit",
                "10",
            ],
        )

        assert result.exit_code == 0, result.stderr
        budget = captured["deps"].budget
        assert isinstance(budget, CompositeBudget)
        tracker_types = {type(t).__name__ for t in budget.trackers}
        assert tracker_types == {"CostBudget", "TimeBudget", "SampleBudget"}


class TestNoLimits:
    def test_no_limit_flags_leaves_budget_none(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run").resolve()
        expected_dir.mkdir(parents=True)
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_capturing_orchestrator(captured, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        # No CLI flags AND no ``compute.max_budget_usd`` in the config → the
        # CLI passes ``budget=None`` and the orchestrator's ``_resolve_budget``
        # sees nothing to promote.
        assert captured["deps"].budget is None
