"""CLI-level integration tests for the A5 run banner wiring.

Locks that ``tolokaforge run``:

- prints the start banner (``→ Run:`` + ``→ Report: file:///…``) on stderr
  BEFORE ``LiveRunDisplay.__enter__``,
- prints the end banner (``✓ Run complete in <duration>`` /
  ``✗ Run failed in <duration>``, ``→ Report:``, ``→ Browse:``) on stderr
  AFTER ``LiveRunDisplay.__exit__`` on BOTH the success and failure paths
  (``finally:`` — the failure banner fires even though the exception
  continues to propagate),
- calls :func:`emit_artifact_path` AFTER the end banner on the success
  path only (the stdout artifact line is gated on success),
- threads the pre-resolved ``(run_id, output_dir)`` pair through
  ``Orchestrator.run(run_id=..., output_dir=...)`` — the values orchestrator
  sees match what the CLI computed via :func:`resolve_run_directory` and
  named in the start banner,
- silences both banners under ``--display=none`` (they route through the
  shared ``console`` that ``silence_console`` quiets); the stdout artifact
  line still fires.

Every test invokes the CLI under a stub Orchestrator + a fake display
factory so no LLM / Docker surface is touched.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tests.utils.orchestrator_stubs import complete_run
from tolokaforge.core.logging import _TOLOKAFORGE_ROOT_HANDLER_SENTINEL
from tolokaforge.dx._display import DisplayMode
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_console_quiet():
    """``--display=none`` sets ``console.quiet = True`` via
    :func:`silence_console`; restore it so subsequent tests in the shared
    session see the same starting state."""
    from tolokaforge.dx._display import console as _console

    saved = _console.quiet
    yield
    _console.quiet = saved


@pytest.fixture(autouse=True)
def _isolated_root_logging():
    """Snapshot root-logger handlers / level so ``silence_root_logging``
    (fired under ``--display=none``) does not leak WARNING-level filters
    into the shared session."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if getattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, False):
            root.removeHandler(handler)
    root.handlers = saved_handlers
    root.setLevel(saved_level)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    """Minimal run config accepted by :func:`load_effective_run_config`."""
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


def _install_stub_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_raises: BaseException | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Install a stub :class:`Orchestrator` on ``cli_main``.

    The stub records the ``run_id`` / ``output_dir`` kwargs it receives
    from the CLI (Stage 2 threads the pre-resolved pair through
    ``Orchestrator.run(run_id=..., output_dir=...)``) and appends to a
    shared ordering list on ``__init__`` / ``load_tasks`` / ``run``. Set
    ``run_raises`` to a ``BaseException`` to exercise the failure path
    without swallowing the exception.

    Returns ``(recorded_kwargs, ordering)`` — the caller can assert on
    both after the CLI invocation returns.
    """

    recorded_kwargs: list[dict[str, Any]] = []
    ordering: list[str] = []

    class _StubOrchestrator:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.tasks = [object()]
            self.grading_completeness = complete_run()

        def load_tasks(self) -> None:
            ordering.append("load_tasks")

        def run(self, **kwargs: Any) -> Path:
            ordering.append("run")
            recorded_kwargs.append(kwargs)
            if run_raises is not None:
                raise run_raises
            output_dir = kwargs["output_dir"]
            output_dir.mkdir(parents=True, exist_ok=True)
            return output_dir.resolve()

    monkeypatch.setattr(cli_main, "Orchestrator", _StubOrchestrator)
    return recorded_kwargs, ordering


# ---------------------------------------------------------------------------
# Start banner
# ---------------------------------------------------------------------------


class TestStartBannerVisible:
    def test_start_banner_lands_on_stderr_with_absolute_file_url(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_stub_orchestrator(monkeypatch)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert "→ Run:" in result.stderr
        assert "→ Report:" in result.stderr
        assert "file:///" in result.stderr


# ---------------------------------------------------------------------------
# End banner — success path
# ---------------------------------------------------------------------------


class TestEndBannerVisibleOnSuccess:
    def test_end_banner_success_landmarks_in_order(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_stub_orchestrator(monkeypatch)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        stderr = result.stderr
        # Start banner appears first, end banner after.
        start_idx = stderr.index("→ Run:")
        complete_idx = stderr.index("✓ Run complete in")
        report_idx = stderr.rindex("→ Report:")
        browse_idx = stderr.index("→ Browse: tolokaforge browse ")
        assert start_idx < complete_idx < report_idx < browse_idx

    def test_end_banner_duration_uses_mmss_format_for_fast_run(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stubbed run completes in well under an hour; the duration on
        the end banner therefore renders as ``MM:SS`` (never ``HH:MM:SS``)."""
        _install_stub_orchestrator(monkeypatch)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        match = re.search(r"✓ Run complete in (\S+)", result.stderr)
        assert match is not None, result.stderr
        assert re.fullmatch(r"\d{2}:\d{2}", match.group(1)), match.group(1)


# ---------------------------------------------------------------------------
# End banner — failure path
# ---------------------------------------------------------------------------


class TestEndBannerVisibleOnFailure:
    def test_orchestrator_raise_fires_end_banner_then_propagates(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_stub_orchestrator(monkeypatch, run_raises=RuntimeError("boom"))

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code != 0
        # Exception still propagated to Click — banner did not swallow it.
        assert isinstance(result.exception, RuntimeError)
        stderr = result.stderr
        assert "✗ Run failed in" in stderr
        assert "→ Report:" in stderr
        assert "→ Browse: tolokaforge browse " in stderr

    def test_failure_path_leaves_stdout_empty(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The end banner is stderr-only; the stdout artifact line is
        skipped on failure (guarded by the ``try:``/``finally:`` layout —
        ``emit_artifact_path`` sits after the ``try`` block)."""
        _install_stub_orchestrator(monkeypatch, run_raises=RuntimeError("boom"))

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code != 0
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# --display=none silences banners; stdout still fires
# ---------------------------------------------------------------------------


class TestBannerSilencedUnderDisplayNone:
    def test_none_silences_banners_but_stdout_line_remains(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_stub_orchestrator(monkeypatch)

        result = runner.invoke(cli, ["--display", "none", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert result.stderr == ""
        assert result.stdout.count("\n") == 1
        assert Path(result.stdout.strip()).is_absolute()


# ---------------------------------------------------------------------------
# Ordering: banner sits between display exit and stdout artifact line
# ---------------------------------------------------------------------------


class TestOrdering:
    def test_success_sequence_is_enter_run_exit_endbanner_emit(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _recorded, ordering = _install_stub_orchestrator(monkeypatch)

        real_for_mode = cli_main.LiveRunDisplay.for_mode

        class _WrappedCtx:
            def __init__(self, inner: Any) -> None:
                self._inner = inner
                self.events = inner.events

            def __enter__(self) -> Any:
                ordering.append("__enter__")
                self._inner.__enter__()
                return self

            def __exit__(self, *exc_info: Any) -> Any:
                res = self._inner.__exit__(*exc_info)
                ordering.append("__exit__")
                return res

        def wrapping_for_mode(mode: DisplayMode, **kwargs: Any) -> _WrappedCtx:
            return _WrappedCtx(real_for_mode(mode, **kwargs))

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", wrapping_for_mode)

        real_end_banner = cli_main.print_run_end_banner

        def recording_end_banner(**kwargs: Any) -> None:
            ordering.append("print_run_end_banner")
            return real_end_banner(**kwargs)

        monkeypatch.setattr(cli_main, "print_run_end_banner", recording_end_banner)

        real_emit = cli_main.emit_artifact_path

        def recording_emit(path: Any) -> None:
            ordering.append("emit_artifact_path")
            return real_emit(path)

        monkeypatch.setattr(cli_main, "emit_artifact_path", recording_emit)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        # __enter__ → run → __exit__ → end banner → emit_artifact_path.
        enter_idx = ordering.index("__enter__")
        run_idx = ordering.index("run")
        exit_idx = ordering.index("__exit__")
        end_banner_idx = ordering.index("print_run_end_banner")
        emit_idx = ordering.index("emit_artifact_path")
        assert enter_idx < run_idx < exit_idx < end_banner_idx < emit_idx

    def test_failure_sequence_ends_at_endbanner_no_emit(
        self,
        runner: CliRunner,
        valid_config: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _recorded, ordering = _install_stub_orchestrator(
            monkeypatch, run_raises=RuntimeError("boom")
        )

        real_end_banner = cli_main.print_run_end_banner

        def recording_end_banner(**kwargs: Any) -> None:
            ordering.append("print_run_end_banner")
            return real_end_banner(**kwargs)

        monkeypatch.setattr(cli_main, "print_run_end_banner", recording_end_banner)

        real_emit = cli_main.emit_artifact_path

        def recording_emit(path: Any) -> None:
            ordering.append("emit_artifact_path")
            return real_emit(path)

        monkeypatch.setattr(cli_main, "emit_artifact_path", recording_emit)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code != 0
        assert "print_run_end_banner" in ordering
        assert "emit_artifact_path" not in ordering


# ---------------------------------------------------------------------------
# Orchestrator receives the pre-resolved (run_id, output_dir) pair
# ---------------------------------------------------------------------------


class TestOrchestratorReceivesPreResolvedRunId:
    def test_run_kwargs_match_resolve_run_directory_output(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded_kwargs, _ordering = _install_stub_orchestrator(monkeypatch)

        # Freeze the timestamp so the CLI's resolve_run_directory returns
        # a predictable pair; the stub records what the CLI actually passed
        # in and we assert it matches.
        from tolokaforge.core import orchestrator as orch_module

        class _FrozenDatetime:
            @staticmethod
            def now() -> Any:
                class _Fake:
                    @staticmethod
                    def strftime(fmt: str) -> str:
                        return "20260715_120000"

                return _Fake()

        monkeypatch.setattr(orch_module, "datetime", _FrozenDatetime)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert len(recorded_kwargs) == 1
        seen = recorded_kwargs[0]
        assert seen["run_id"] == "out_20260715_120000"
        assert seen["output_dir"] == tmp_path / "out_20260715_120000"
        # The frozen run_id appears verbatim in both banners.
        assert "out_20260715_120000" in result.stderr

    def test_start_banner_names_the_same_run_dir_orchestrator_receives(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded_kwargs, _ordering = _install_stub_orchestrator(monkeypatch)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert len(recorded_kwargs) == 1
        seen_output_dir = recorded_kwargs[0]["output_dir"]
        # The absolute file URL for the run dir must appear in the start
        # banner (via `.resolve().as_uri()`).
        expected_url_prefix = seen_output_dir.resolve().as_uri()
        assert expected_url_prefix in result.stderr


# ---------------------------------------------------------------------------
# Backwards-compat: Orchestrator.run() with no kwargs still resolves internally
# ---------------------------------------------------------------------------


class TestOrchestratorRunNoKwargsBackwardCompat:
    def test_run_without_kwargs_falls_back_to_resolve_run_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Existing callers that invoke ``Orchestrator.run()`` with no
        kwargs must still work — the internal ``resolve_run_directory``
        call preserves prior behaviour (used only by test / notebook
        callers; the CLI now threads the pair)."""
        from tolokaforge.core import orchestrator as orch_module

        captured: dict[str, Any] = {}

        def fake_resolve(base: str) -> tuple[str, Path]:
            captured["base"] = base
            run_id = "captured_20260715_120000"
            return run_id, tmp_path / run_id

        monkeypatch.setattr(orch_module, "resolve_run_directory", fake_resolve)

        # Build a bare-bones Orchestrator sufficient to reach the
        # resolve_run_directory call site. We monkeypatch just enough of
        # its I/O to bail after the resolve step.
        class _StopAfterResolve(Exception):
            pass

        def fake_mkdir(self: Path, **_: Any) -> None:
            raise _StopAfterResolve(str(self))

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)

        class _CfgEval:
            output_dir = "results/my_run"

        class _Cfg:
            evaluation = _CfgEval()

        orchestrator = orch_module.Orchestrator.__new__(orch_module.Orchestrator)
        orchestrator.config = _Cfg()  # type: ignore[assignment]

        with pytest.raises(_StopAfterResolve) as excinfo:
            orchestrator.run()

        # resolve_run_directory was called with the config's output_dir,
        # and the mkdir landed on the resolved output_dir.
        assert captured["base"] == "results/my_run"
        assert "captured_20260715_120000" in str(excinfo.value)

    def test_run_with_only_run_id_raises(self) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        orchestrator = Orchestrator.__new__(Orchestrator)
        with pytest.raises(ValueError, match="output_dir"):
            orchestrator.run(run_id="anything", output_dir=None)

    def test_run_with_only_output_dir_raises(self, tmp_path: Path) -> None:
        from tolokaforge.core.orchestrator import Orchestrator

        orchestrator = Orchestrator.__new__(Orchestrator)
        with pytest.raises(ValueError, match="run_id"):
            orchestrator.run(run_id=None, output_dir=tmp_path / "x")
