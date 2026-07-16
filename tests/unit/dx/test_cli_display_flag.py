"""CLI-level tests for the root ``--display`` flag and ``TOLOKAFORGE_DISPLAY``
env-var wiring.

Every test invokes ``tolokaforge`` under a stub :class:`Orchestrator` (from
:mod:`tests.unit.test_cli_stdout_contract`) so nothing hits real LLM /
Docker surfaces. The assertions cover:

- explicit ``--display`` flag surface (five modes + invalid values → exit 2),
- ``TOLOKAFORGE_DISPLAY`` env var propagation and its precedence rules
  (explicit > env > CI > isatty),
- ``ctx.obj["display_mode"]`` propagation for every mode,
- Textual fallback: ``--display=full`` rewrites to ``RICH`` with a
  WARNING log line, unless a fake ``textual`` module is installed,
- ``--display=none`` silencing on success (empty stderr; single stdout line)
  and preserved traceback on failure paths,
- composition with ``-v`` / ``-q`` and with ``--log-format``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tests.unit.dx.test_cli_stdout_contract import _make_stub_orchestrator
from tolokaforge.core.logging import (
    _TOLOKAFORGE_ROOT_HANDLER_SENTINEL,
    LogFormat,
)
from tolokaforge.dx._display import DisplayMode
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    """Minimal run config that ``load_effective_run_config`` accepts and
    ``RunConfig`` validates. Kept in sync with the fixture in
    ``test_cli_stdout_contract``."""

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


@pytest.fixture(autouse=True)
def isolated_root_logging():
    """Snapshot root handlers/level so each test starts clean. Mirrors
    the fixture in ``test_logging_cli_flags`` — B2 mutates handler level
    (``silence_root_logging``) so we must restore aggressively."""

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.handlers = []
    root.setLevel(logging.WARNING)
    yield
    for handler in list(root.handlers):
        if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
            root.removeHandler(handler)
    root.handlers = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture(autouse=True)
def restore_console_quiet():
    """``silence_console`` mutates the module-level ``console.quiet``.
    Reset between tests so state doesn't leak into ``test_cli_display``
    baselines that assert ``console.quiet is False``."""

    from tolokaforge.dx._display import console as _console

    saved = _console.quiet
    yield
    _console.quiet = saved


def _stub_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    run_raises: BaseException | None = None,
) -> Path:
    """Install a stub Orchestrator on ``cli_main`` and return the run-dir
    it would emit on success. Callers pass ``run_raises=...`` for the
    failure-path variant."""

    run_return = (tmp_path / "results" / "run_20260715_120000").resolve()
    if run_raises is None:
        run_return.mkdir(parents=True)
    stub = _make_stub_orchestrator(run_return=run_return, run_raises=run_raises)
    monkeypatch.setattr(cli_main, "Orchestrator", stub)
    return run_return


# ---------------------------------------------------------------------------
# Explicit-flag surface
# ---------------------------------------------------------------------------


class TestDisplayFlagSurface:
    def test_help_renders_when_valid_flag_supplied(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--display", "none", "run", "--help"])
        assert result.exit_code == 0, result.stderr

    def test_invalid_flag_value_exits_two(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--display", "wombat", "run", "--help"])
        assert result.exit_code == 2
        combined = (result.output or "") + (result.stderr or "")
        assert "wombat" in combined or "invalid choice" in combined.lower()

    def test_invalid_env_var_value_exits_two(
        self, runner: CliRunner, valid_config: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_run(monkeypatch, tmp_path)
        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config)],
            env={"TOLOKAFORGE_DISPLAY": "wombat"},
        )
        assert result.exit_code == 2
        combined = (result.output or "") + (result.stderr or "")
        assert "TOLOKAFORGE_DISPLAY" in combined


# ---------------------------------------------------------------------------
# ctx.obj propagation
# ---------------------------------------------------------------------------


class TestCtxObjPropagation:
    """The group callback stashes the resolved ``DisplayMode`` on
    ``ctx.obj["display_mode"]``. Every downstream consumer (B1 / C3) reads
    from that key. Locked here across the five explicit-flag modes plus
    the Textual fallback.
    """

    @pytest.fixture
    def captured(self, runner: CliRunner) -> dict[str, Any]:
        """Attach a ``probe`` subcommand to ``cli`` that records ``ctx.obj``.

        The command is removed via ``cli.commands.pop`` after the test to
        keep the shared ``cli`` group clean for other tests in the module.
        """

        import click as _click

        recorded: dict[str, Any] = {}

        @cli.command(name="_display_probe")
        @_click.pass_context
        def _probe(ctx: _click.Context) -> None:
            recorded["display_mode"] = ctx.obj.get("display_mode")
            recorded["log_format"] = ctx.obj.get("log_format")

        yield recorded
        cli.commands.pop("_display_probe", None)

    @pytest.mark.parametrize(
        "flag_value, expected",
        [
            ("rich", DisplayMode.RICH),
            ("plain", DisplayMode.PLAIN),
            ("log", DisplayMode.LOG),
            ("none", DisplayMode.NONE),
        ],
    )
    def test_explicit_flag_stashes_mode(
        self,
        runner: CliRunner,
        captured: dict[str, Any],
        flag_value: str,
        expected: DisplayMode,
    ) -> None:
        result = runner.invoke(cli, ["--display", flag_value, "_display_probe"])
        assert result.exit_code == 0, result.stderr
        assert captured["display_mode"] is expected

    def test_explicit_full_without_textual_stashes_rich(
        self,
        runner: CliRunner,
        captured: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`textual` ships in the `[dx]` extras, so simulate its absence by
        forcing :func:`importlib.util.find_spec` to return ``None`` for the
        `textual` module. The CLI's ``_resolve_display_mode`` reads that
        signal via :func:`tolokaforge.dx._display._textual_available` and
        rewrites ``--display=full`` to ``--display=rich`` with a WARNING.
        """
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def _fake_find_spec(name: str, package: str | None = None):  # type: ignore[no-untyped-def]
            if name == "textual":
                return None
            return real_find_spec(name, package)

        monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)

        result = runner.invoke(cli, ["--display", "full", "_display_probe"])
        assert result.exit_code == 0, result.stderr
        assert captured["display_mode"] is DisplayMode.RICH
        assert "textual is not installed" in result.stderr

    def test_explicit_full_with_textual_installed_keeps_full(
        self,
        runner: CliRunner,
        captured: dict[str, Any],
    ) -> None:
        """`textual` is a `[dx]` extra and always importable in the test env.

        No monkey-patch required — the CLI must pass ``FULL`` through
        untouched and never emit the fallback WARNING line.
        """
        result = runner.invoke(cli, ["--display", "full", "_display_probe"])
        assert result.exit_code == 0, result.stderr
        assert captured["display_mode"] is DisplayMode.FULL
        assert "textual is not installed" not in result.stderr


# ---------------------------------------------------------------------------
# Env-var precedence via CliRunner env=
# ---------------------------------------------------------------------------


class TestEnvVarPrecedence:
    """``TOLOKAFORGE_DISPLAY`` env var beats ``CI``; explicit flag beats
    both. ``CI=1`` alone auto-selects ``PLAIN``. Every branch runs through
    the group callback + a probe subcommand for ``ctx.obj`` inspection.
    """

    @pytest.fixture
    def captured(self) -> dict[str, Any]:
        import click as _click

        recorded: dict[str, Any] = {}

        @cli.command(name="_display_probe")
        @_click.pass_context
        def _probe(ctx: _click.Context) -> None:
            recorded["display_mode"] = ctx.obj.get("display_mode")

        yield recorded
        cli.commands.pop("_display_probe", None)

    def test_env_var_selects_when_no_flag(
        self, runner: CliRunner, captured: dict[str, Any]
    ) -> None:
        result = runner.invoke(cli, ["_display_probe"], env={"TOLOKAFORGE_DISPLAY": "log"})
        assert result.exit_code == 0, result.stderr
        assert captured["display_mode"] is DisplayMode.LOG

    def test_explicit_flag_beats_env_var(self, runner: CliRunner, captured: dict[str, Any]) -> None:
        result = runner.invoke(
            cli,
            ["--display", "none", "_display_probe"],
            env={"TOLOKAFORGE_DISPLAY": "rich"},
        )
        assert result.exit_code == 0, result.stderr
        assert captured["display_mode"] is DisplayMode.NONE

    def test_ci_env_auto_selects_plain(self, runner: CliRunner, captured: dict[str, Any]) -> None:
        result = runner.invoke(cli, ["_display_probe"], env={"CI": "1"})
        assert result.exit_code == 0, result.stderr
        assert captured["display_mode"] is DisplayMode.PLAIN

    def test_env_var_beats_ci(self, runner: CliRunner, captured: dict[str, Any]) -> None:
        result = runner.invoke(
            cli,
            ["_display_probe"],
            env={"CI": "1", "TOLOKAFORGE_DISPLAY": "rich"},
        )
        assert result.exit_code == 0, result.stderr
        assert captured["display_mode"] is DisplayMode.RICH


# ---------------------------------------------------------------------------
# --display=none silencing (AC-critical)
# ---------------------------------------------------------------------------


class TestDisplayNoneSilencing:
    """``--display=none`` silences both the shared ``console`` and the
    tolokaforge root log handler on the success path. The single stdout
    write (``emit_artifact_path``) still fires. Failure paths bypass the
    silencer — click's ``UsageError`` and Python's uncaught traceback
    both write to stderr directly.
    """

    def test_success_stderr_is_empty_and_stdout_has_artifact_path(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = _stub_run(monkeypatch, tmp_path)

        result = runner.invoke(cli, ["--display", "none", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert result.stderr == ""
        assert result.stdout.count("\n") == 1
        assert Path(result.stdout.strip()) == expected_dir

    def test_env_var_none_matches_flag_none(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        expected_dir = _stub_run(monkeypatch, tmp_path)

        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config)],
            env={"TOLOKAFORGE_DISPLAY": "none"},
        )

        assert result.exit_code == 0, result.stderr
        assert result.stderr == ""
        assert result.stdout.count("\n") == 1
        assert Path(result.stdout.strip()) == expected_dir

    def test_failure_path_surfaces_exception_not_silenced(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Silencing must not swallow errors: click captures uncaught
        exceptions on ``result.exception`` and exits non-zero. In
        production, ``sys.excepthook`` prints the traceback to stderr
        outside the tolokaforge log handler — the equivalent
        ``CliRunner`` observable is ``result.exception is not None``."""

        _stub_run(monkeypatch, tmp_path, run_raises=RuntimeError("boom"))

        result = runner.invoke(cli, ["--display", "none", "run", "--config", str(valid_config)])

        assert result.exit_code != 0
        assert result.stdout == ""
        assert isinstance(result.exception, RuntimeError)
        assert "boom" in str(result.exception)

    def test_usage_error_reaches_stderr_even_with_display_none(self, runner: CliRunner) -> None:
        """Click writes ``UsageError`` output directly to stderr, bypassing
        the tolokaforge log handler. ``--display=none`` cannot silence it —
        which is the whole point (operator misconfiguration must fail loud)."""

        result = runner.invoke(
            cli, ["--display", "none", "run", "--config", "/nonexistent/config.yaml"]
        )

        assert result.exit_code != 0
        assert result.stdout == ""
        assert result.stderr != ""

    def test_none_with_root_verbose_stderr_still_empty(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_run(monkeypatch, tmp_path)

        result = runner.invoke(
            cli, ["--display", "none", "-v", "run", "--config", str(valid_config)]
        )

        assert result.exit_code == 0, result.stderr
        assert result.stderr == ""

    def test_none_with_root_quiet_stderr_still_empty(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_run(monkeypatch, tmp_path)

        result = runner.invoke(
            cli, ["--display", "none", "-q", "run", "--config", str(valid_config)]
        )

        assert result.exit_code == 0, result.stderr
        assert result.stderr == ""


# ---------------------------------------------------------------------------
# Composition with --log-format
# ---------------------------------------------------------------------------


class TestCompositionWithLogFormat:
    """``--display`` and ``--log-format`` are orthogonal axes. Both are
    stored on ``ctx.obj`` independently; ``--display=none`` still silences
    stderr even when ``--log-format=json`` is requested.
    """

    @pytest.fixture
    def captured(self) -> dict[str, Any]:
        import click as _click

        recorded: dict[str, Any] = {}

        @cli.command(name="_display_probe")
        @_click.pass_context
        def _probe(ctx: _click.Context) -> None:
            recorded["display_mode"] = ctx.obj.get("display_mode")
            recorded["log_format"] = ctx.obj.get("log_format")

        yield recorded
        cli.commands.pop("_display_probe", None)

    def test_log_display_and_json_log_format_stored_independently(
        self, runner: CliRunner, captured: dict[str, Any]
    ) -> None:
        result = runner.invoke(cli, ["--display", "log", "--log-format", "json", "_display_probe"])
        assert result.exit_code == 0, result.stderr
        assert captured["display_mode"] is DisplayMode.LOG
        assert captured["log_format"] is LogFormat.JSON

    def test_none_display_with_json_log_format_silences_stderr(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub_run(monkeypatch, tmp_path)

        result = runner.invoke(
            cli,
            [
                "--display",
                "none",
                "--log-format",
                "json",
                "run",
                "--config",
                str(valid_config),
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert result.stderr == ""
