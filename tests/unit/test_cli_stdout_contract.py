"""Unit tests locking the stdout / stderr split of the tolokaforge CLI.

``tolokaforge run`` emits the resolved absolute run-dir path as the sole
line on ``sys.stdout``; every other line lands on ``sys.stderr``. Failure
paths (bad config, orchestrator raise, zero tasks) exit non-zero and
leave ``sys.stdout`` empty. These tests exercise the ``run`` command
under a stubbed :class:`Orchestrator` so nothing hits real LLM / Docker
surfaces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.cli.main as cli_main
from tolokaforge.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    """Minimal run config that ``load_effective_run_config`` accepts and
    ``RunConfig`` validates."""

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


def _make_stub_orchestrator(
    *,
    tasks: list[Any] | None = None,
    run_return: Path | None = None,
    run_raises: BaseException | None = None,
) -> type:
    """Factory for a stub orchestrator class with configurable behaviour."""

    task_list = list(tasks) if tasks is not None else [object()]

    class _StubOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.tasks = list(task_list)

        def load_tasks(self) -> None:
            return None

        def run(self) -> Path:
            if run_raises is not None:
                raise run_raises
            assert run_return is not None
            return run_return

    return _StubOrchestrator


class TestRunStdoutContract:
    """``tolokaforge run`` emits exactly one line on stdout (the resolved
    absolute run-dir path) on success, and nothing on any failure path."""

    def test_run_success_stdout_is_single_resolved_path(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run_20260715_120000").resolve()
        expected_dir.mkdir(parents=True)

        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_stub_orchestrator(run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert result.stdout.count("\n") == 1
        emitted = result.stdout.strip()
        assert Path(emitted).is_absolute()
        assert Path(emitted) == expected_dir

    def test_run_stdout_line_has_no_ansi_no_markup(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = (tmp_path / "results" / "run_20260715_120000").resolve()
        expected_dir.mkdir(parents=True)

        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_stub_orchestrator(run_return=expected_dir),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code == 0
        assert "\x1b" not in result.stdout
        assert "[" not in result.stdout

    def test_run_failure_stdout_is_empty_on_bad_config(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["run", "--config", "/nonexistent/config.yaml"])

        assert result.exit_code != 0
        assert result.stdout == ""

    def test_run_failure_stdout_is_empty_on_orchestrator_raise(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_stub_orchestrator(run_raises=RuntimeError("boom")),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code != 0
        assert result.stdout == ""

    def test_run_no_tasks_exits_nonzero(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_stub_orchestrator(tasks=[]),
        )

        result = runner.invoke(cli, ["run", "--config", str(valid_config)])

        assert result.exit_code != 0
        assert result.stdout == ""
        assert "No tasks found" in result.stderr
