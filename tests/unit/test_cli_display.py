"""Unit tests locking the shared CLI display layer contract.

Every assertion here maps to a documented invariant in
``tolokaforge/dx/_display.py`` — if a test fails, the surface changed.
"""

from __future__ import annotations

import importlib.machinery
import io
import os
import sys
import types
from pathlib import Path

import pytest
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.style import Style
from rich.text import Text

from tolokaforge.dx._display import (
    THEME,
    DisplayMode,
    _textual_available,
    console,
    emit_artifact_path,
    make_live,
    make_progress,
    select_display_mode,
    silence_console,
)

pytestmark = pytest.mark.unit


class _FakeStream(io.StringIO):
    """StringIO with a controllable ``isatty()`` result."""

    def __init__(self, *, is_tty: bool) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:  # noqa: D401 — simple accessor
        return self._is_tty


def _fake_tty() -> _FakeStream:
    return _FakeStream(is_tty=True)


def _fake_pipe() -> _FakeStream:
    return _FakeStream(is_tty=False)


def test_console_writes_to_stderr() -> None:
    assert console.stderr is True
    assert console.file is sys.stderr


def test_console_has_soft_wrap() -> None:
    assert console.soft_wrap is True


def test_theme_defines_semantic_tokens() -> None:
    required = {"info", "warn", "error", "success", "muted", "cost", "link"}
    assert set(THEME.styles.keys()) >= required
    for token in required:
        resolved = console.get_style(token)
        assert isinstance(resolved, Style), f"token {token!r} did not resolve to a Style"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("info", "cyan"),
        ("warn", "yellow"),
        ("error", "bold red"),
        ("success", "green"),
        ("muted", "dim"),
        ("cost", "bold magenta"),
        ("link", "underline cyan"),
    ],
)
def test_theme_palette_frozen(token: str, expected: str) -> None:
    assert console.get_style(token) == Style.parse(expected)


def test_make_progress_uses_shared_console() -> None:
    assert make_progress().console is console


def test_make_progress_default_columns() -> None:
    expected_classes = [
        SpinnerColumn,
        TextColumn,
        BarColumn,
        MofNCompleteColumn,
        TaskProgressColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    ]
    progress = make_progress()
    actual_classes = [type(col) for col in progress.columns]
    assert actual_classes == expected_classes


def test_make_progress_disable_kwarg() -> None:
    assert make_progress(disable=True).disable is True


def test_make_progress_transient_kwarg() -> None:
    assert make_progress(transient=True).live.transient is True


def test_make_progress_custom_columns_override_default() -> None:
    progress = make_progress(columns=[SpinnerColumn()])
    assert len(progress.columns) == 1
    assert isinstance(progress.columns[0], SpinnerColumn)


def test_make_progress_default_kwargs() -> None:
    progress = make_progress()
    assert progress.live.transient is False
    assert progress.live.auto_refresh is True
    assert progress.live.refresh_per_second == 10.0
    # Rich stores redirect_stdout/redirect_stderr on Live's private fields
    # (same class of internal-accessor risk documented for Progress.live.transient).
    assert progress.live._redirect_stdout is True
    assert progress.live._redirect_stderr is True


def test_make_progress_redirect_kwargs_passthrough() -> None:
    progress = make_progress(redirect_stdout=False, redirect_stderr=False)
    assert progress.live._redirect_stdout is False
    assert progress.live._redirect_stderr is False


def test_make_progress_auto_refresh_kwarg() -> None:
    assert make_progress(auto_refresh=False).live.auto_refresh is False


def test_make_progress_refresh_per_second_kwarg() -> None:
    assert make_progress(refresh_per_second=2.5).live.refresh_per_second == 2.5


def test_make_live_uses_shared_console() -> None:
    assert make_live().console is console


def test_make_live_defaults() -> None:
    live = make_live()
    assert live.refresh_per_second == 4.0
    assert live.transient is False
    # Rich stores the ``screen`` init kwarg on the private ``_screen`` field;
    # no public accessor exists on Live in rich 15.x.
    assert live._screen is False
    assert live.auto_refresh is True
    assert live.vertical_overflow == "ellipsis"
    assert live._redirect_stdout is True
    assert live._redirect_stderr is True


def test_make_live_redirect_kwargs_passthrough() -> None:
    live = make_live(redirect_stdout=False, redirect_stderr=False)
    assert live._redirect_stdout is False
    assert live._redirect_stderr is False


def test_make_live_accepts_renderable() -> None:
    renderable = Text("hello")
    live = make_live(renderable)
    assert live.renderable is renderable


class TestEmitArtifactPath:
    """``emit_artifact_path`` is the ONE sanctioned stdout write in the
    CLI — it emits a resolved absolute path with a trailing newline,
    flushed, and never colours or prefixes the line."""

    def test_emit_artifact_path_prints_resolved_absolute(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        emit_artifact_path(tmp_path)
        captured = capfd.readouterr()
        assert captured.out == str(tmp_path.resolve()) + "\n"
        assert captured.err == ""

    def test_emit_artifact_path_accepts_string(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        emit_artifact_path(str(tmp_path))
        captured = capfd.readouterr()
        assert captured.out == str(tmp_path.resolve()) + "\n"

    def test_emit_artifact_path_resolves_relative_input(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        emit_artifact_path("results/run")
        captured = capfd.readouterr()
        emitted = captured.out.strip()
        assert Path(emitted).is_absolute()
        assert Path(emitted) == (tmp_path / "results" / "run").resolve()

    def test_emit_artifact_path_calls_flush(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock the ``flush=True`` contract by spying on ``.flush()`` directly.

        The shell-composition idiom depends on the artifact path reaching the
        pipe before the process exits — Python's line buffering usually does
        this, but ``flush=True`` guarantees it. A capture-based test would
        pass even without the flush (line-buffered stdout flushes on newline
        before ``readouterr``), so lock the invariant by intercepting the
        stream directly.
        """
        import sys as _sys

        calls: list[str] = []

        class _Spy:
            def write(self, data: str) -> int:
                calls.append("write")
                return len(data)

            def flush(self) -> None:
                calls.append("flush")

        monkeypatch.setattr(_sys, "stdout", _Spy())
        emit_artifact_path(tmp_path)
        assert "flush" in calls, f"emit_artifact_path did not call flush(); saw {calls}"


class TestDisplayModeEnum:
    def test_members_and_order(self) -> None:
        assert list(DisplayMode.__members__) == ["FULL", "RICH", "PLAIN", "LOG", "NONE"]

    def test_values_match_cli_literals(self) -> None:
        assert DisplayMode.FULL.value == "full"
        assert DisplayMode.RICH.value == "rich"
        assert DisplayMode.PLAIN.value == "plain"
        assert DisplayMode.LOG.value == "log"
        assert DisplayMode.NONE.value == "none"

    def test_is_str_enum(self) -> None:
        # (str, Enum) lets click.Choice accept `.value` literals natively.
        assert isinstance(DisplayMode.RICH, str)
        assert DisplayMode.RICH == "rich"


class TestSelectDisplayMode:
    def test_explicit_wins_over_env_and_ci(self) -> None:
        result = select_display_mode(
            explicit="none",
            env={"CI": "1", "TOLOKAFORGE_DISPLAY": "rich"},
            stream=_fake_tty(),
        )
        assert result is DisplayMode.NONE

    def test_explicit_accepts_display_mode_instance(self) -> None:
        result = select_display_mode(explicit=DisplayMode.LOG, env={"CI": "1"})
        assert result is DisplayMode.LOG

    def test_env_var_wins_over_ci(self) -> None:
        result = select_display_mode(
            explicit=None,
            env={"TOLOKAFORGE_DISPLAY": "log", "CI": "1"},
            stream=_fake_tty(),
        )
        assert result is DisplayMode.LOG

    @pytest.mark.parametrize("ci_value", ["1", "true", "yes", "on", "TRUE"])
    def test_ci_truthy_yields_plain(self, ci_value: str) -> None:
        result = select_display_mode(explicit=None, env={"CI": ci_value}, stream=_fake_tty())
        assert result is DisplayMode.PLAIN

    @pytest.mark.parametrize("ci_value", ["0", "false", "False", "FALSE", "no", "off", ""])
    def test_ci_falsy_falls_through(self, ci_value: str) -> None:
        # With CI falsy and a TTY stream, isatty branch selects RICH.
        result = select_display_mode(explicit=None, env={"CI": ci_value}, stream=_fake_tty())
        assert result is DisplayMode.RICH

    def test_ci_zero_on_tty_resolves_rich(self) -> None:
        # Explicit round-1 critic row: CI=0 is not truthy, falls through to isatty.
        assert (
            select_display_mode(explicit=None, env={"CI": "0"}, stream=_fake_tty())
            is DisplayMode.RICH
        )

    def test_isatty_true_selects_rich(self) -> None:
        assert select_display_mode(explicit=None, env={}, stream=_fake_tty()) is DisplayMode.RICH

    def test_isatty_false_selects_plain(self) -> None:
        assert select_display_mode(explicit=None, env={}, stream=_fake_pipe()) is DisplayMode.PLAIN

    def test_env_var_no_flag_no_ci_no_tty_yields_plain(self) -> None:
        assert select_display_mode(explicit=None, env={}, stream=_fake_pipe()) is DisplayMode.PLAIN

    @pytest.mark.parametrize("mode", ["full", "rich", "plain", "log", "none"])
    def test_env_var_selects_when_no_explicit(self, mode: str) -> None:
        result = select_display_mode(
            explicit=None, env={"TOLOKAFORGE_DISPLAY": mode}, stream=_fake_tty()
        )
        assert result is DisplayMode(mode)

    def test_invalid_explicit_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="invalid --display value"):
            select_display_mode(explicit="wombat", env={})

    def test_invalid_env_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="invalid TOLOKAFORGE_DISPLAY"):
            select_display_mode(explicit=None, env={"TOLOKAFORGE_DISPLAY": "wombat"})

    def test_defaults_read_process_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # env=None → os.environ; CI=1 in the process env should yield PLAIN.
        monkeypatch.setattr(os, "environ", {"CI": "1"})
        assert select_display_mode() is DisplayMode.PLAIN


class TestTextualAvailable:
    def test_no_textual_returns_false(self) -> None:
        # Textual is not a dependency today; find_spec returns None.
        assert _textual_available() is False

    def test_with_fake_textual_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Install a fake `textual` module WITH a spec so find_spec resolves it.
        fake = types.ModuleType("textual")
        fake.__spec__ = importlib.machinery.ModuleSpec("textual", loader=None)
        monkeypatch.setitem(sys.modules, "textual", fake)
        assert _textual_available() is True


class TestSilenceConsole:
    def test_silence_console_sets_quiet(self) -> None:
        original = console.quiet
        try:
            assert console.quiet is False, "baseline drift: console.quiet started True"
            silence_console()
            assert console.quiet is True
        finally:
            console.quiet = original

    def test_silence_console_is_idempotent(self) -> None:
        original = console.quiet
        try:
            silence_console()
            silence_console()
            assert console.quiet is True
        finally:
            console.quiet = original
