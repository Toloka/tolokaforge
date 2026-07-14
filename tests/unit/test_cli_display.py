"""Unit tests locking the shared CLI display layer contract.

Every assertion here maps to a documented invariant in
``tolokaforge/cli/_display.py`` — if a test fails, the surface changed.
"""

from __future__ import annotations

import sys

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

from tolokaforge.cli._display import THEME, console, make_live, make_progress

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


def test_make_live_accepts_renderable() -> None:
    renderable = Text("hello")
    live = make_live(renderable)
    assert live.renderable is renderable
