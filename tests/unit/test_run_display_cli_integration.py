"""CLI-level integration tests for :class:`LiveRunDisplay` wiring.

Locks that ``tolokaforge run``:

- picks up ``ctx.obj["display_mode"]`` and passes it through
  :meth:`LiveRunDisplay.for_mode` at the start of the command body,
- wraps ``orchestrator.load_tasks() + orchestrator.run()`` in the
  returned context-manager,
- threads ``display.events`` into ``OrchestratorDeps(events=...)``,
- fires ``emit_artifact_path`` **after** the ``with`` block exits (a
  load-bearing ordering — Live redraw artefacts must not leak into the
  stdout artifact line),
- propagates exceptions raised by ``orchestrator.run`` through the
  display's ``__exit__``.

Every test invokes the CLI under a stub Orchestrator + a fake display
factory so no LLM / Docker surface is touched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tolokaforge.core.logging import _TOLOKAFORGE_ROOT_HANDLER_SENTINEL
from tolokaforge.core.run_display_events import _NULL_EVENTS
from tolokaforge.dx._display import DisplayMode
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_console_quiet():
    """``--display=none`` sets ``console.quiet = True`` via
    :func:`silence_console`; restore it so subsequent tests in the shared
    session see the same starting state (mirrors the fixture in
    ``test_cli_display_flag`` — B2 mutates module-level state)."""
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


class _FakeDisplay:
    """Fake :class:`LiveRunDisplay` returned by a patched ``for_mode``.

    Records ``__enter__`` / ``__exit__`` in a shared ordering list so a
    test can assert relative order against other recorded events (e.g.
    ``emit_artifact_path``).
    """

    def __init__(self, ordering: list[str], events: Any) -> None:
        self._ordering = ordering
        self.events = events
        self.entered = False
        self.exited = False
        self.exit_exc_info: tuple[Any, ...] | None = None

    def __enter__(self) -> _FakeDisplay:
        self.entered = True
        self._ordering.append("__enter__")
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.exited = True
        self.exit_exc_info = exc_info
        self._ordering.append("__exit__")
        return None


class _RecordingStubOrchestrator:
    """Stub orchestrator recording the ``events`` kwarg passed via ``deps``.

    Adds two behaviours on top of the plain ``_make_stub_orchestrator``:
    (1) records how the CLI wired ``OrchestratorDeps.events`` for the
    running test, and (2) fires a ``run_started`` / ``run_finished``
    round-trip on the events sink so we can verify end-to-end wiring.
    """

    # Populated per-instance from the class fixture below.
    _run_return: Path | None = None
    _run_raises: BaseException | None = None
    _events_recorder: list[Any] = []
    _ordering: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        deps = kwargs.get("deps")
        events = deps.events if deps is not None else None
        type(self)._events_recorder.append(events)
        self._events = events
        self.tasks = [object()]

    def load_tasks(self) -> None:
        type(self)._ordering.append("load_tasks")

    def run(self, **_: object) -> Path:
        type(self)._ordering.append("run")
        if self._events is not None:
            self._events.run_started(total_trials=1, initial_completed=0)
        if self._run_raises is not None:
            raise self._run_raises
        assert self._run_return is not None
        if self._events is not None:
            self._events.run_finished(output_dir=self._run_return)
        return self._run_return


def _install_recording_stub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    run_raises: BaseException | None = None,
) -> tuple[type, Path, list[Any], list[str]]:
    """Wire the recording stub onto ``cli_main.Orchestrator`` and return the
    recording buffers so tests can assert on them post-invocation."""

    run_return = (tmp_path / "results" / "run_20260715_120000").resolve()
    if run_raises is None:
        run_return.mkdir(parents=True)

    events_recorder: list[Any] = []
    ordering: list[str] = []

    stub = type(
        "_RecordingStub",
        (_RecordingStubOrchestrator,),
        {
            "_run_return": run_return,
            "_run_raises": run_raises,
            "_events_recorder": events_recorder,
            "_ordering": ordering,
        },
    )
    monkeypatch.setattr(cli_main, "Orchestrator", stub)
    return stub, run_return, events_recorder, ordering


# ---------------------------------------------------------------------------
# --display=rich → LiveRunDisplay activated
# ---------------------------------------------------------------------------


class TestDisplayRichActivatesLiveRunDisplay:
    def test_for_mode_receives_display_mode_from_ctx(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_recording_stub(monkeypatch, tmp_path)
        seen_modes: list[DisplayMode] = []

        real_for_mode = cli_main.LiveRunDisplay.for_mode

        def spying_for_mode(mode: DisplayMode, **kwargs: Any):
            seen_modes.append(mode)
            return real_for_mode(mode, **kwargs)

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", spying_for_mode)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert seen_modes == [DisplayMode.RICH]

    def test_rich_wires_display_events_into_orchestrator_deps(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub, _run_return, events_recorder, ordering = _install_recording_stub(
            monkeypatch, tmp_path
        )

        emit_marker: list[str] = []
        real_emit = cli_main.emit_artifact_path

        def recording_emit(path: Any) -> None:
            emit_marker.append("emit_artifact_path")
            ordering.append("emit_artifact_path")
            return real_emit(path)

        monkeypatch.setattr(cli_main, "emit_artifact_path", recording_emit)

        fake_ordering: list[str] = []
        fake_events = _FakeEvents()

        def fake_for_mode(mode: DisplayMode, **_: Any) -> _FakeDisplay:
            return _FakeDisplay(fake_ordering, fake_events)

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", fake_for_mode)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        # The events kwarg on OrchestratorDeps is the fake display's events.
        assert events_recorder == [fake_events]
        # Enter fired before load_tasks + run; exit fired after run returned;
        # emit_artifact_path fired AFTER exit (round-2 ordering lock).
        assert fake_ordering[0] == "__enter__"
        assert fake_ordering[-1] == "__exit__"
        assert emit_marker == ["emit_artifact_path"]

    def test_emit_artifact_path_fires_after_display_exit(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shared ordering list captures both ``__exit__`` and
        ``emit_artifact_path`` — the latter must land AFTER the former so
        Live redraw artefacts cannot contaminate the single stdout line."""
        _stub, _run_return, _events_recorder, ordering = _install_recording_stub(
            monkeypatch, tmp_path
        )

        real_emit = cli_main.emit_artifact_path

        def recording_emit(path: Any) -> None:
            ordering.append("emit_artifact_path")
            return real_emit(path)

        monkeypatch.setattr(cli_main, "emit_artifact_path", recording_emit)

        shared_ordering = ordering  # display and emit push into the same list

        def fake_for_mode(mode: DisplayMode, **_: Any) -> _FakeDisplay:
            return _FakeDisplay(shared_ordering, _FakeEvents())

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", fake_for_mode)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        exit_idx = shared_ordering.index("__exit__")
        emit_idx = shared_ordering.index("emit_artifact_path")
        assert (
            exit_idx < emit_idx
        ), f"emit_artifact_path must fire AFTER display.__exit__; got ordering {shared_ordering!r}"


# ---------------------------------------------------------------------------
# --display={plain,log,none} → _NoopDisplayCtx / _NULL_EVENTS
# ---------------------------------------------------------------------------


class TestPassiveModesUseNullEvents:
    @pytest.mark.parametrize("mode", ["plain", "log", "none"])
    def test_passive_modes_threads_null_events(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        _stub, _run_return, events_recorder, _ordering = _install_recording_stub(
            monkeypatch, tmp_path
        )

        result = runner.invoke(cli, ["--display", mode, "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert events_recorder == [_NULL_EVENTS]

    def test_none_display_still_silences_stderr(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_recording_stub(monkeypatch, tmp_path)

        result = runner.invoke(cli, ["--display", "none", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert result.stderr == ""


# ---------------------------------------------------------------------------
# Failure paths — display.__exit__ still fires
# ---------------------------------------------------------------------------


class TestFailurePathHandling:
    def test_orchestrator_raise_still_exits_display(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_recording_stub(monkeypatch, tmp_path, run_raises=RuntimeError("boom"))

        fake_ordering: list[str] = []
        fake_display = _FakeDisplay(fake_ordering, _FakeEvents())

        def fake_for_mode(mode: DisplayMode, **_: Any) -> _FakeDisplay:
            return fake_display

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", fake_for_mode)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        # __exit__ ran despite the raise; exc_info carries the exception.
        assert fake_display.exited is True
        assert fake_display.exit_exc_info is not None
        exc_type = fake_display.exit_exc_info[0]
        assert exc_type is RuntimeError


# ---------------------------------------------------------------------------
# Composition with -v / -q / --log-format
# ---------------------------------------------------------------------------


class TestCompositionWithOtherFlags:
    def test_rich_with_log_format_json(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub, _run_return, events_recorder, _ordering = _install_recording_stub(
            monkeypatch, tmp_path
        )

        fake_events = _FakeEvents()

        def fake_for_mode(mode: DisplayMode, **_: Any) -> _FakeDisplay:
            return _FakeDisplay([], fake_events)

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", fake_for_mode)

        result = runner.invoke(
            cli,
            [
                "--display",
                "rich",
                "--log-format",
                "json",
                "run",
                "--config",
                str(valid_config),
            ],
        )

        assert result.exit_code == 0, result.stderr
        assert events_recorder == [fake_events]

    def test_rich_with_verbose(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub, _run_return, events_recorder, _ordering = _install_recording_stub(
            monkeypatch, tmp_path
        )

        fake_events = _FakeEvents()

        def fake_for_mode(mode: DisplayMode, **_: Any) -> _FakeDisplay:
            return _FakeDisplay([], fake_events)

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", fake_for_mode)

        result = runner.invoke(
            cli, ["-v", "--display", "rich", "run", "--config", str(valid_config)]
        )

        assert result.exit_code == 0, result.stderr
        assert events_recorder == [fake_events]

    def test_rich_with_quiet(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _stub, _run_return, events_recorder, _ordering = _install_recording_stub(
            monkeypatch, tmp_path
        )

        fake_events = _FakeEvents()

        def fake_for_mode(mode: DisplayMode, **_: Any) -> _FakeDisplay:
            return _FakeDisplay([], fake_events)

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", fake_for_mode)

        result = runner.invoke(
            cli, ["-q", "--display", "rich", "run", "--config", str(valid_config)]
        )

        assert result.exit_code == 0, result.stderr
        assert events_recorder == [fake_events]


# ---------------------------------------------------------------------------
# End-to-end: stub orchestrator fires events through the display
# ---------------------------------------------------------------------------


class _FakeEvents:
    """Minimal :class:`RunDisplayEvents` — accepts every call, records nothing."""

    def run_started(self, **_: Any) -> None: ...
    def trial_started(self, **_: Any) -> None: ...
    def trial_progress(self, **_: Any) -> None: ...
    def trial_completed(self, **_: Any) -> None: ...
    def trial_failed(self, **_: Any) -> None: ...
    def judgment_scored(self, **_: Any) -> None: ...
    def run_finished(self, **_: Any) -> None: ...
    def phase_changed(self, **_: Any) -> None: ...
    def trial_provisioned(self, **_: Any) -> None: ...


class TestEventsFlowEndToEnd:
    def test_stub_orchestrator_fires_events_on_wired_display(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The stub orchestrator's ``run()`` fires ``run_started`` /
        ``run_finished`` on the events sink the CLI wired up — proving
        that the display's ``.events`` reaches the runner-side code."""

        _stub, _run_return, events_recorder, _ordering = _install_recording_stub(
            monkeypatch, tmp_path
        )

        received: list[str] = []

        class _RecordingFakeEvents:
            def run_started(self, **_: Any) -> None:
                received.append("run_started")

            def trial_started(self, **_: Any) -> None:
                received.append("trial_started")

            def trial_progress(self, **_: Any) -> None:
                received.append("trial_progress")

            def trial_completed(self, **_: Any) -> None:
                received.append("trial_completed")

            def trial_failed(self, **_: Any) -> None:
                received.append("trial_failed")

            def judgment_scored(self, **_: Any) -> None:
                received.append("judgment_scored")

            def run_finished(self, **_: Any) -> None:
                received.append("run_finished")

            def phase_changed(self, **_: Any) -> None:
                received.append("phase_changed")

            def trial_provisioned(self, **_: Any) -> None:
                received.append("trial_provisioned")

        recording_events = _RecordingFakeEvents()

        def fake_for_mode(mode: DisplayMode, **_: Any) -> _FakeDisplay:
            return _FakeDisplay([], recording_events)

        monkeypatch.setattr(cli_main.LiveRunDisplay, "for_mode", fake_for_mode)

        result = runner.invoke(cli, ["--display", "rich", "run", "--config", str(valid_config)])

        assert result.exit_code == 0, result.stderr
        assert received == ["run_started", "run_finished"]
