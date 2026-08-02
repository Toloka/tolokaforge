"""Unit tests locking the stdout / stderr split of the tolokaforge CLI.

``tolokaforge run`` and ``tolokaforge prepare`` emit the resolved absolute
run-dir path as the sole line on ``sys.stdout``; every other line lands on
``sys.stderr``. Failure paths (bad config, orchestrator raise, zero tasks)
exit non-zero and leave ``sys.stdout`` empty. Read-only commands (``status``,
``validate``, ``config validate``, ``assets stamp``) never touch ``sys.stdout``.
These tests exercise the CLI under a stubbed :class:`Orchestrator` (for
``run`` / ``prepare``) and real invocations (for read-only commands) so
nothing hits real LLM / Docker surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tolokaforge.dx.cli.main import cli

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
    prepare_return: dict[str, Any] | None = None,
    prepare_raises: BaseException | None = None,
) -> type:
    """Factory for a stub orchestrator class with configurable behaviour.

    Supports both ``run`` and ``prepare_run`` — the ``run`` tests set
    ``run_return`` / ``run_raises``; the ``prepare`` tests set
    ``prepare_return`` / ``prepare_raises``. ``prepare_run`` creates the
    supplied ``output_dir`` on disk so the CLI's ``emit_artifact_path``
    call resolves against a real path.
    """

    task_list = list(tasks) if tasks is not None else [object()]
    default_prepare_summary: dict[str, Any] = {
        "queued_attempts": 1,
        "queue_counts": {"pending": 1, "total": 1},
        "queue_backend": "sqlite",
    }

    class _StubOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.tasks = list(task_list)

        def load_tasks(self) -> None:
            return None

        def run(self, **_: object) -> Path:
            if run_raises is not None:
                raise run_raises
            assert run_return is not None
            return run_return

        def prepare_run(self, output_dir: Path, reset_queue: bool = False) -> dict[str, Any]:
            if prepare_raises is not None:
                raise prepare_raises
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            return prepare_return if prepare_return is not None else default_prepare_summary

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


class TestPrepareStdoutContract:
    """``tolokaforge prepare`` emits exactly one line on stdout (the
    resolved absolute run-dir path) on success, and nothing on any
    failure path."""

    def test_prepare_success_stdout_is_single_resolved_path(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "queue-run"

        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_stub_orchestrator(),
        )

        result = runner.invoke(
            cli,
            ["prepare", "--config", str(valid_config), "--run-dir", str(run_dir)],
        )

        assert result.exit_code == 0, result.stderr
        assert result.stdout.count("\n") == 1
        emitted = result.stdout.strip()
        assert Path(emitted).is_absolute()
        assert Path(emitted) == run_dir.resolve()

    def test_prepare_failure_stdout_is_empty_on_bad_config(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(
            cli,
            [
                "prepare",
                "--config",
                "/nonexistent/config.yaml",
                "--run-dir",
                str(tmp_path / "queue-run"),
            ],
        )

        assert result.exit_code != 0
        assert result.stdout == ""

    def test_prepare_failure_stdout_is_empty_on_orchestrator_raise(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            cli_main,
            "Orchestrator",
            _make_stub_orchestrator(prepare_raises=RuntimeError("boom")),
        )

        result = runner.invoke(
            cli,
            [
                "prepare",
                "--config",
                str(valid_config),
                "--run-dir",
                str(tmp_path / "queue-run"),
            ],
        )

        assert result.exit_code != 0
        assert result.stdout == ""


class TestReadOnlyCommandsStdoutIsEmpty:
    """Read-only commands never write to ``sys.stdout`` — every human line
    they emit lands on ``sys.stderr`` via the shared display console.
    A regression here would break the shell-composition contract by
    polluting stdout on non-artifact-producing commands.
    """

    def test_status_stdout_is_empty(self, runner: CliRunner, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "run_state.json").write_text(
            json.dumps(
                {
                    "run_id": "test_run",
                    "config_path": "test.yaml",
                    "output_dir": str(run_dir),
                    "start_ts": "2026-07-15T12:00:00+00:00",
                    "last_updated": "2026-07-15T12:00:00+00:00",
                    "status": "completed",
                    "total_trials": 0,
                    "completed_trials": 0,
                    "failed_trials": 0,
                    "trials": {},
                }
            )
        )

        result = runner.invoke(cli, ["status", "--run-dir", str(run_dir)])

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""
        assert "test_run" in result.stderr

    def test_validate_stdout_is_empty(self, runner: CliRunner, tmp_path: Path) -> None:
        pack = tmp_path / "pack"
        pack.mkdir()
        (pack / "grading.yaml").write_text("{}\n")
        (pack / "task.yaml").write_text("task_id: t\ndescription: A task.\n")

        result = runner.invoke(cli, ["validate", "--tasks", str(pack / "task.yaml")])

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""

    def test_validate_stdout_is_empty_on_failure(self, runner: CliRunner, tmp_path: Path) -> None:
        # Empty glob — the failure surface stays entirely on stderr.
        result = runner.invoke(cli, ["validate", "--tasks", str(tmp_path / "no-such" / "*.yaml")])

        assert result.exit_code != 0
        assert result.stdout == ""

    def test_config_validate_stdout_is_empty(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli,
            [
                "config",
                "validate",
                "--config",
                str(tmp_path / "nonexistent-config.yaml"),
            ],
        )

        assert result.exit_code != 0
        assert result.stdout == ""

    def test_assets_stamp_stdout_is_empty(self, runner: CliRunner, tmp_path: Path) -> None:
        # Minimal project.yaml with one seed file — mirrors the write-mode
        # fixture in test_cli_assets.py. Success path: digest is stamped
        # and the "wrote 1 digest" summary lands on stderr.
        seed = tmp_path / "shared" / "seeds" / "base.sql"
        seed.parent.mkdir(parents=True)
        seed.write_bytes(b"-- baseline\n")
        (tmp_path / "project.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "p",
                    "assets": {
                        "seeds": {
                            "base": {
                                "path": "shared/seeds/base.sql",
                                "kind": "sql_dump",
                                "digest": "sha256:placeholder",
                            },
                        },
                    },
                },
                sort_keys=False,
            )
        )

        result = runner.invoke(cli, ["assets", "stamp", str(tmp_path)])

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""

    def test_adapter_convert_stdout_is_empty(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`adapter convert` writes native bundles to disk; console output on stderr."""
        import tolokaforge.adapters as adapters_pkg
        from tolokaforge.adapters.base import NativeTaskBundle
        from tolokaforge.adapters.native import NativeAdapter

        class _Stub(NativeAdapter):
            def __init__(self, params: dict | None = None) -> None:
                super().__init__({"tasks_glob": "unused/**", **(params or {})})

            def get_task_ids(self) -> list[str]:
                return ["t1"]

            def convert_to_native(self, task_id: str) -> NativeTaskBundle:
                return NativeTaskBundle(
                    task_config={"name": f"Task {task_id}", "category": "tool_use"},
                    grading_config={"combine": {"method": "weighted", "pass_threshold": 1.0}},
                    fixtures={},
                    metadata={"source_adapter": "stub"},
                )

        monkeypatch.setattr(adapters_pkg, "get_adapter", lambda name, params: _Stub())
        out = tmp_path / "out"
        result = runner.invoke(
            cli,
            ["adapter", "convert", "--name", "stub", "--tasks-glob", "x/**", "--output", str(out)],
        )

        assert result.exit_code == 0, result.stderr
        assert result.stdout == ""

    def test_docker_status_no_sdk_stdout_is_empty(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`docker status` with no docker SDK exits 1 via stderr; stdout stays empty."""
        import sys as _sys

        # Force ImportError inside the command's `import docker as docker_sdk` call.
        monkeypatch.setitem(_sys.modules, "docker", None)

        result = runner.invoke(cli, ["docker", "status"])

        assert result.exit_code != 0
        assert result.stdout == ""

    def test_analyze_missing_trajectory_stdout_is_empty(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """`analyze` on a missing trajectory exits non-zero via stderr; stdout empty."""
        result = runner.invoke(cli, ["analyze", str(tmp_path / "nonexistent.json")])

        assert result.exit_code != 0
        assert result.stdout == ""
