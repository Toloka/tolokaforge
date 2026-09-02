"""Canonical lock for ``tolokaforge run`` / ``tolokaforge worker`` exit codes.

The exit-code table at ``docs/CLI.md § Run and worker exit codes`` is a CI
contract: automation reads shell exit codes to gate deploys and decide
whether to alert. This file fixes that contract in one place so any drift
in the widened completion-gate function (`_fail_on_completeness_gates` at
``tolokaforge/dx/cli/main.py``) or the documented table breaks a test
rather than silently changing a caller's behaviour. See
``docs/adr/0041-zero-coverage-exit-signal.md``.

Uses the same in-process stubbed-orchestrator harness as
``tests/unit/dx/test_cli_stdout_contract.py`` — no Docker, no LLM key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tests.utils.orchestrator_stubs import complete_run
from tolokaforge.core.orchestrator import GradingCompleteness
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.canonical


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
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


def _stub_orchestrator(*, completeness: GradingCompleteness, run_return: Path) -> type:
    """Minimal stub matching the ``run`` and ``run_worker`` reads of the
    completion-gate path. Publishes the supplied ``GradingCompleteness``
    verbatim so the tests below drive the gate function directly."""

    class _StubOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.tasks: list[object] = [object()]
            self.grading_completeness = completeness

        def load_tasks(self) -> None:
            return None

        def run(self, **_: object) -> Path:
            return run_return

        def run_worker(self, run_dir: Path, max_attempts: int | None = None) -> dict[str, Any]:
            return {
                "processed_attempts": 1,
                "completed_attempts": 1,
                "failed_attempts": 0,
                "requeued_attempts": 0,
                "total_cost_usd": 0.0,
            }

    return _StubOrchestrator


_CLEAN = GradingCompleteness(
    total_attempts=4,
    ungradeable_trial_ids=(),
    measured_trials=4,
    scored_trials=4,
    judge_errored_trials=0,
)
_ZERO_COVERAGE = GradingCompleteness(
    total_attempts=4,
    ungradeable_trial_ids=(),
    measured_trials=0,
    scored_trials=0,
    judge_errored_trials=0,
)
_ZERO_JUDGE_GRADED = GradingCompleteness(
    total_attempts=3,
    ungradeable_trial_ids=(),
    measured_trials=3,
    scored_trials=3,
    judge_errored_trials=3,
)
_UNGRADEABLE = GradingCompleteness(
    total_attempts=4,
    ungradeable_trial_ids=("TASK-A:0", "TASK-A:1"),
    measured_trials=4,
    scored_trials=2,
    judge_errored_trials=0,
)
_BOTH_ZERO_COVERAGE_AND_UNGRADEABLE = GradingCompleteness(
    total_attempts=4,
    ungradeable_trial_ids=("TASK-A:0",),
    measured_trials=0,
    scored_trials=0,
    judge_errored_trials=0,
)


class TestRunExitCodeContract:
    """Every row in ``docs/CLI.md § Run and worker exit codes`` locked as a cell."""

    def test_clean_run_exits_zero_with_no_completion_gate_line(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Row 1: every attempt reached a verdict, no opt-in gate fires → ``0``.

        Locks the default: a caller that ignores the two flags sees the
        shipped exit-0 semantics unchanged for a clean run.
        """
        expected_dir = (tmp_path / "results" / "run_20260715_120000").resolve()
        expected_dir.mkdir(parents=True)
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(completeness=_CLEAN, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert "Run measured no trials" not in result.stderr
        assert "LLM judge errored" not in result.stderr
        assert "could not be graded" not in result.stderr

    def test_ungradeable_without_flags_exits_one_with_ungradeable_line(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Row 4: the run completed and any trial is ungradeable, no exit-2
        gate fires → ``1`` with the ungradeable line naming the shape."""
        expected_dir = (tmp_path / "results" / "run_20260715_120000").resolve()
        expected_dir.mkdir(parents=True)
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(completeness=_UNGRADEABLE, run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 1
        assert "2 of 4 attempts could not be graded" in result.stderr
        assert "TASK-A:0" in result.stderr

    def test_zero_coverage_with_flag_exits_two_with_zero_coverage_line(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Row 2: ``--fail-on-zero-coverage`` set and the run measured
        nothing → ``2`` with the "Run measured no trials" line."""
        expected_dir = (tmp_path / "results" / "run_20260715_120000").resolve()
        expected_dir.mkdir(parents=True)
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(completeness=_ZERO_COVERAGE, run_return=expected_dir),
        )

        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config), "--fail-on-zero-coverage"],
        )

        assert result.exit_code == 2, result.stderr
        assert "Run measured no trials on 4 attempted" in result.stderr
        assert "See infrastructure_aborts in aggregate.json" in result.stderr

    def test_zero_judge_graded_with_flag_exits_two_with_judge_errored_line(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Row 3: ``--fail-on-zero-judge-graded`` set and every produced grade
        has ``judge_status == ERRORED`` → ``2`` with the judge-errored line."""
        expected_dir = (tmp_path / "results" / "run_20260715_120000").resolve()
        expected_dir.mkdir(parents=True)
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(completeness=_ZERO_JUDGE_GRADED, run_return=expected_dir),
        )

        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config), "--fail-on-zero-judge-graded"],
        )

        assert result.exit_code == 2, result.stderr
        assert "LLM judge errored on every scored trial" in result.stderr
        assert "0 of 3 grades succeeded" in result.stderr
        assert "See judge_status in trajectory.json" in result.stderr

    def test_zero_coverage_dominates_ungradeable_when_only_coverage_flag_set(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Precedence cell: ``zero_coverage`` and ``ungradeable > 0`` both true,
        only ``--fail-on-zero-coverage`` set → exit ``2`` on the zero-coverage
        line. Exit 2 dominates exit 1 whenever a flag fires. See ADR-0041 §
        Decision precedence."""
        expected_dir = (tmp_path / "results" / "run_20260715_120000").resolve()
        expected_dir.mkdir(parents=True)
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(
                completeness=_BOTH_ZERO_COVERAGE_AND_UNGRADEABLE,
                run_return=expected_dir,
            ),
        )

        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config), "--fail-on-zero-coverage"],
        )

        assert result.exit_code == 2, result.stderr
        assert "Run measured no trials" in result.stderr
        assert "could not be graded" not in result.stderr


class TestWorkerExitCodeContract:
    """The worker gates on the same table over its own attempts.

    A sharded CI reads the worker's exit code, not the ``run`` process's, so
    the four-cell matrix has to be locked at the worker surface too.
    """

    def test_clean_worker_exits_zero(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(completeness=complete_run(total_attempts=3), run_return=tmp_path),
        )

        result = runner.invoke(
            cli,
            ["worker", "--config", str(valid_config), "--run-dir", str(tmp_path / "run_dir")],
        )

        assert result.exit_code == 0, result.stderr

    def test_ungradeable_worker_exits_one(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(completeness=_UNGRADEABLE, run_return=tmp_path),
        )

        result = runner.invoke(
            cli,
            ["worker", "--config", str(valid_config), "--run-dir", str(tmp_path / "run_dir")],
        )

        assert result.exit_code == 1
        assert "could not be graded" in result.stderr

    def test_worker_zero_coverage_flag_exits_two(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(completeness=_ZERO_COVERAGE, run_return=tmp_path),
        )

        result = runner.invoke(
            cli,
            [
                "worker",
                "--config",
                str(valid_config),
                "--run-dir",
                str(tmp_path / "run_dir"),
                "--fail-on-zero-coverage",
            ],
        )

        assert result.exit_code == 2, result.stderr
        assert "Run measured no trials on 4 attempted" in result.stderr

    def test_worker_zero_judge_graded_flag_exits_two(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _stub_orchestrator(completeness=_ZERO_JUDGE_GRADED, run_return=tmp_path),
        )

        result = runner.invoke(
            cli,
            [
                "worker",
                "--config",
                str(valid_config),
                "--run-dir",
                str(tmp_path / "run_dir"),
                "--fail-on-zero-judge-graded",
            ],
        )

        assert result.exit_code == 2, result.stderr
        assert "LLM judge errored on every scored trial" in result.stderr
