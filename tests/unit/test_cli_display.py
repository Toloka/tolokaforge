"""Unit tests locking the shared CLI display layer contract.

Every assertion here maps to a documented invariant in
``tolokaforge/cli/_display.py`` — if a test fails, the surface changed.
"""

from __future__ import annotations

import sys
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

from tolokaforge.cli._display import (
    THEME,
    console,
    emit_artifact_path,
    make_live,
    make_progress,
)

pytestmark = pytest.mark.unit


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

    def test_emit_artifact_path_is_flushed(
        self, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # ``capfd`` captures at the file-descriptor level, so anything not
        # flushed to fd 1 by the time ``readouterr`` runs would come back
        # empty. Reading the emitted path here proves the helper flushed.
        emit_artifact_path(tmp_path)
        captured = capfd.readouterr()
        assert captured.out.endswith("\n")
        assert captured.out.strip() == str(tmp_path.resolve())
