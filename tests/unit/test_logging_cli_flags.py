"""Composition-matrix tests for root `--verbose`/`--quiet`/`--log-format`
and subcommand `--verbose` on `run/prepare/worker/convert`.

Each row of the precedence table in
``docs/plans/2026-07-14-issue-279-a3-structured-log-format.md`` is locked
here: root `-v` bumps the console to DEBUG, root `-q` silences to
WARNING, `-v -q` is a `UsageError`, subcommand `--verbose` bumps the
console when root did not pass `-q`, and root `-q` wins over subcommand
`--verbose` on the console (while the orchestrator still receives
``verbose=True`` so per-trial ``logs.yaml`` records DEBUG).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.cli.main as cli_main
import tolokaforge.core.orchestrator as orchestrator_module
from tolokaforge.cli.main import cli
from tolokaforge.core.logging import (
    _TOLOKAFORGE_ROOT_HANDLER_SENTINEL,
    LogFormat,
    configure_root_logging,
)
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _ListHandler(logging.Handler):
    """Captures every record emitted through the root logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def isolated_root_logging():
    """Snapshot root handlers/level so each test starts clean."""

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = []
    root.setLevel(logging.WARNING)
    yield
    # Remove any tolokaforge sentinel handlers installed during the test.
    for handler in list(root.handlers):
        if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
            root.removeHandler(handler)
    root.handlers = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def spy() -> _ListHandler:
    handler = _ListHandler()
    logging.getLogger().addHandler(handler)
    yield handler
    logging.getLogger().removeHandler(handler)


class _RecordingOrchestrator:
    """Stand-in orchestrator that records constructor kwargs.

    The composition-matrix asserts that ``verbose=True`` propagates into
    ``Orchestrator`` for subcommand ``--verbose`` cases, without actually
    running a trial. `load_tasks` returns nothing so the CLI takes the
    empty-tasks early-return branch.
    """

    calls: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.__class__.calls.append(kwargs)
        self.tasks: list = []

    def load_tasks(self) -> None:
        return None

    def run(self) -> None:
        return None

    def prepare_run(self, run_dir: Path, reset_queue: bool = False) -> dict:
        return {
            "queued_attempts": 0,
            "queue_counts": {"pending": 0, "total": 0},
            "queue_backend": "sqlite",
        }

    def run_worker(self, run_dir: Path, max_attempts: int | None = None) -> dict:
        return {
            "processed_attempts": 0,
            "completed_attempts": 0,
            "failed_attempts": 0,
            "requeued_attempts": 0,
            "total_cost_usd": 0.0,
        }


@pytest.fixture
def stub_orchestrator(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingOrchestrator]:
    _RecordingOrchestrator.calls = []
    monkeypatch.setattr(cli_main, "Orchestrator", _RecordingOrchestrator)
    return _RecordingOrchestrator


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    """Minimal `run` config: loader parses it, RunConfig validates it."""

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


# ---------------------------------------------------------------------------
# Root flag surface — level, mutual exclusion, log-format parse
# ---------------------------------------------------------------------------


def test_root_verbose_alone_sets_debug_on_console(runner: CliRunner, spy: _ListHandler) -> None:
    result = runner.invoke(cli, ["-v", "run", "--help"])

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger("tolokaforge.probe").debug("probe")
    assert any(r.levelno == logging.DEBUG for r in spy.records)


def test_root_quiet_alone_silences_info_records(runner: CliRunner, spy: _ListHandler) -> None:
    result = runner.invoke(cli, ["-q", "run", "--help"])

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.WARNING
    logging.getLogger("tolokaforge.probe").info("dropped")
    logging.getLogger("tolokaforge.probe").warning("kept")
    levels = {r.levelno for r in spy.records}
    assert logging.INFO not in levels
    assert logging.WARNING in levels


def test_root_quiet_silences_docker_namespace_even_when_child_level_is_info() -> None:
    """Root ``-q`` must gate ``tolokaforge.docker.*`` INFO records at the handler.

    ``Orchestrator.__init__`` sets ``logging.getLogger("tolokaforge.docker").setLevel(INFO)``
    unconditionally, so the child logger admits INFO records at origin. Without a
    handler-level cap, those records would propagate to the root's tolokaforge
    handler (default level 0) and emit — contradicting the ``-q`` promise. The
    handler-level cap in ``configure_root_logging`` is what makes ``-q`` authoritative.
    """
    import io

    stream = io.StringIO()
    configure_root_logging(level=logging.WARNING, log_format=LogFormat.PLAIN, stream=stream)

    # Verify the fix is in place: the tolokaforge handler carries the level cap.
    root_handler = next(
        h
        for h in logging.getLogger().handlers
        if getattr(h, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False)
    )
    assert root_handler.level == logging.WARNING

    # Simulate Orchestrator.__init__'s setLevel(INFO) on the docker namespace.
    docker_logger = logging.getLogger("tolokaforge.docker")
    saved = docker_logger.level
    try:
        docker_logger.setLevel(logging.INFO)
        docker_logger.info("pulled image")
        docker_logger.warning("pull failed")
    finally:
        docker_logger.setLevel(saved)

    output = stream.getvalue()
    assert "pulled image" not in output, output
    assert "pull failed" in output, output


def test_root_verbose_and_quiet_together_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["-v", "-q", "run", "--help"])

    assert result.exit_code == 2
    combined = (result.output or "") + (result.stderr or "")
    assert "--verbose and --quiet are mutually exclusive" in combined


def test_root_log_format_json_parses(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--log-format", "json", "--help"])

    assert result.exit_code == 0, result.stderr


def test_root_log_format_choice_rejects_unknown(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--log-format", "wombat", "run", "--help"])

    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Subcommand --verbose × root -v/-q composition (symmetric option a)
# ---------------------------------------------------------------------------


def test_root_verbose_with_run_verbose_keeps_debug_and_bumps_orchestrator(
    runner: CliRunner,
    spy: _ListHandler,
    stub_orchestrator: type[_RecordingOrchestrator],
    valid_config: Path,
) -> None:
    result = runner.invoke(
        cli,
        ["-v", "run", "--config", str(valid_config), "--verbose"],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.DEBUG
    assert stub_orchestrator.calls, "Orchestrator was never constructed"
    assert stub_orchestrator.calls[0]["verbose"] is True


def test_root_quiet_with_run_verbose_silences_console_but_orchestrator_still_verbose(
    runner: CliRunner,
    spy: _ListHandler,
    stub_orchestrator: type[_RecordingOrchestrator],
    valid_config: Path,
) -> None:
    result = runner.invoke(
        cli,
        ["-q", "run", "--config", str(valid_config), "--verbose"],
    )

    assert result.exit_code == 0, result.stderr
    # Root `-q` wins: subcommand `--verbose` does NOT bump the console.
    assert logging.getLogger().level == logging.WARNING
    logging.getLogger("tolokaforge.probe").debug("dropped-debug")
    logging.getLogger("tolokaforge.probe").info("dropped-info")
    levels = {r.levelno for r in spy.records}
    assert logging.DEBUG not in levels
    assert logging.INFO not in levels
    # Orchestrator still receives verbose=True (per-trial logs.yaml @ DEBUG).
    assert stub_orchestrator.calls[0]["verbose"] is True


def test_plain_run_verbose_bumps_console_to_debug(
    runner: CliRunner,
    spy: _ListHandler,
    stub_orchestrator: type[_RecordingOrchestrator],
    valid_config: Path,
) -> None:
    result = runner.invoke(
        cli,
        ["run", "--config", str(valid_config), "--verbose"],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger("tolokaforge.probe").debug("probe")
    assert any(r.levelno == logging.DEBUG for r in spy.records)
    assert stub_orchestrator.calls[0]["verbose"] is True


def test_plain_run_without_verbose_stays_at_info(
    runner: CliRunner,
    spy: _ListHandler,
    stub_orchestrator: type[_RecordingOrchestrator],
    valid_config: Path,
) -> None:
    result = runner.invoke(
        cli,
        ["run", "--config", str(valid_config)],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.INFO
    logging.getLogger("tolokaforge.probe").debug("dropped")
    logging.getLogger("tolokaforge.probe").info("kept")
    levels = {r.levelno for r in spy.records}
    assert logging.DEBUG not in levels
    assert logging.INFO in levels
    assert stub_orchestrator.calls[0]["verbose"] is False


# Subcommand-verbose bump parametrised over prepare / worker / convert.
# `convert` uses tasks_glob, so we feed it a temporary empty glob and
# assert only on the root level bump (the command itself exits before
# doing any conversion work when the glob matches nothing).


def test_prepare_verbose_bumps_console_to_debug(
    runner: CliRunner,
    spy: _ListHandler,
    stub_orchestrator: type[_RecordingOrchestrator],
    valid_config: Path,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        cli,
        [
            "prepare",
            "--config",
            str(valid_config),
            "--run-dir",
            str(tmp_path / "run_dir"),
            "--verbose",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.DEBUG
    assert stub_orchestrator.calls[0]["verbose"] is True


def test_root_quiet_with_prepare_verbose_silences_console(
    runner: CliRunner,
    spy: _ListHandler,
    stub_orchestrator: type[_RecordingOrchestrator],
    valid_config: Path,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        cli,
        [
            "-q",
            "prepare",
            "--config",
            str(valid_config),
            "--run-dir",
            str(tmp_path / "run_dir"),
            "--verbose",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.WARNING
    assert stub_orchestrator.calls[0]["verbose"] is True


def test_worker_verbose_bumps_console_to_debug(
    runner: CliRunner,
    spy: _ListHandler,
    stub_orchestrator: type[_RecordingOrchestrator],
    valid_config: Path,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        cli,
        [
            "worker",
            "--config",
            str(valid_config),
            "--run-dir",
            str(tmp_path / "run_dir"),
            "--verbose",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.DEBUG
    assert stub_orchestrator.calls[0]["verbose"] is True


def test_root_quiet_with_worker_verbose_silences_console(
    runner: CliRunner,
    spy: _ListHandler,
    stub_orchestrator: type[_RecordingOrchestrator],
    valid_config: Path,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        cli,
        [
            "-q",
            "worker",
            "--config",
            str(valid_config),
            "--run-dir",
            str(tmp_path / "run_dir"),
            "--verbose",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.WARNING
    assert stub_orchestrator.calls[0]["verbose"] is True


def test_convert_verbose_bumps_console_to_debug(
    runner: CliRunner,
    spy: _ListHandler,
    tmp_path: Path,
) -> None:
    # Empty output dir + glob that matches nothing → exit 0 with a "no tasks"
    # message. `convert --verbose` fires the console bump before the empty
    # short-circuit, so the root level is DEBUG after the run.
    result = runner.invoke(
        cli,
        [
            "adapter",
            "convert",
            "--name",
            "native",
            "--tasks-glob",
            str(tmp_path / "no_such_tasks" / "*"),
            "--output",
            str(tmp_path / "out"),
            "--verbose",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.DEBUG


def test_root_quiet_with_convert_verbose_silences_console(
    runner: CliRunner,
    spy: _ListHandler,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        cli,
        [
            "-q",
            "adapter",
            "convert",
            "--name",
            "native",
            "--tasks-glob",
            str(tmp_path / "no_such_tasks" / "*"),
            "--output",
            str(tmp_path / "out"),
            "--verbose",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert logging.getLogger().level == logging.WARNING


# ---------------------------------------------------------------------------
# StructuredLogger routes through root; docker handler is gone
# ---------------------------------------------------------------------------


def test_structured_logger_extra_lands_in_logrecord_dict(
    spy: _ListHandler,
) -> None:
    from tolokaforge.core.logging import StructuredLogger, clear_logger_registry

    clear_logger_registry()
    configure_root_logging(level=logging.INFO, log_format=LogFormat.PLAIN)
    try:
        logger = StructuredLogger("orch")
        logger.info("hello", task_id="t1")

        # In-memory list keeps the original key.
        assert logger.logs[0]["context"]["task_id"] == "t1"
        # And the stdlib record's `__dict__` carries the sanitised extra.
        emitted = [r for r in spy.records if r.getMessage() == "hello"]
        assert emitted, "record did not reach root"
        assert emitted[0].__dict__["task_id"] == "t1"
    finally:
        clear_logger_registry()


def test_structured_logger_reserved_key_collision_renamed_ctx_prefix(
    spy: _ListHandler,
) -> None:
    from tolokaforge.core.logging import StructuredLogger, clear_logger_registry

    clear_logger_registry()
    configure_root_logging(level=logging.INFO, log_format=LogFormat.PLAIN)
    try:
        logger = StructuredLogger("orch")
        logger.info("hello", module="shadowed")

        emitted = [r for r in spy.records if r.getMessage() == "hello"]
        assert emitted
        # The reserved `module` slot stays as the stdlib default; our value
        # is renamed to `ctx_module` so LogRecord.__init__ does not raise.
        assert emitted[0].__dict__["ctx_module"] == "shadowed"
        assert emitted[0].__dict__["module"] != "shadowed"
    finally:
        clear_logger_registry()


def test_docker_namespace_records_reach_root_exactly_once(
    spy: _ListHandler,
    tmp_path: Path,
) -> None:
    """After orchestrator init, `tolokaforge.docker` no longer has its own
    handler. A single spy on root sees each record exactly once — the
    absence-of-double invariant that locks the removed handler."""

    configure_root_logging(level=logging.INFO, log_format=LogFormat.PLAIN)

    run_config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(tmp_path / "out")),
    )
    orchestrator_module.Orchestrator(run_config)

    # tolokaforge.docker has no handlers of its own — records propagate to root.
    docker_logger = logging.getLogger("tolokaforge.docker")
    assert docker_logger.handlers == []

    spy.records.clear()
    docker_logger.info("pulled image")

    matches = [r for r in spy.records if r.getMessage() == "pulled image"]
    assert len(matches) == 1
