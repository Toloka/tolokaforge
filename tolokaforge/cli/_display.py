"""Shared display primitives for the tolokaforge CLI.

Every CLI module imports :data:`console` from here instead of instantiating
its own ``rich.Console`` — one theme, one stream posture, one place to change.

Public surface:

- :data:`THEME` — semantic-token palette (``info``, ``warn``, ``error``,
  ``success``, ``muted``, ``cost``, ``link``).
- :data:`console` — shared ``rich.Console`` writing to stderr with soft wrap
  and :data:`THEME` installed.
- :func:`make_progress` — factory for ``rich.progress.Progress`` bound to
  :data:`console` with the CLI's default column set.
- :func:`make_live` — factory for ``rich.live.Live`` bound to :data:`console`.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console, RenderableType
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.theme import Theme

THEME = Theme(
    {
        "info": "cyan",
        "warn": "yellow",
        "error": "bold red",
        "success": "green",
        "muted": "dim",
        "cost": "bold magenta",
        "link": "underline cyan",
    }
)

_SHARED_CONSOLE = Console(stderr=True, soft_wrap=True, theme=THEME)
console = _SHARED_CONSOLE


def _default_progress_columns() -> list[ProgressColumn]:
    return [
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ]


def make_progress(
    *,
    console: Console | None = None,
    transient: bool = False,
    disable: bool = False,
    columns: Sequence[ProgressColumn] | None = None,
    refresh_per_second: float = 10.0,
    auto_refresh: bool = True,
    redirect_stdout: bool = True,
    redirect_stderr: bool = True,
) -> Progress:
    """Return a ``Progress`` bound to the shared console.

    ``columns`` overrides the default column set entirely. Passing ``None``
    yields the CLI's opinionated set: spinner, description, bar, m/n,
    percent, elapsed, remaining.
    """

    target_console = console if console is not None else _SHARED_CONSOLE
    resolved_columns = list(columns) if columns is not None else _default_progress_columns()
    return Progress(
        *resolved_columns,
        console=target_console,
        transient=transient,
        disable=disable,
        refresh_per_second=refresh_per_second,
        auto_refresh=auto_refresh,
        redirect_stdout=redirect_stdout,
        redirect_stderr=redirect_stderr,
    )


def make_live(
    renderable: RenderableType | None = None,
    *,
    console: Console | None = None,
    refresh_per_second: float = 4.0,
    transient: bool = False,
    screen: bool = False,
    auto_refresh: bool = True,
    vertical_overflow: str = "ellipsis",
    redirect_stdout: bool = True,
    redirect_stderr: bool = True,
) -> Live:
    """Return a ``Live`` bound to the shared console."""

    target_console = console if console is not None else _SHARED_CONSOLE
    return Live(
        renderable,
        console=target_console,
        refresh_per_second=refresh_per_second,
        transient=transient,
        screen=screen,
        auto_refresh=auto_refresh,
        vertical_overflow=vertical_overflow,
        redirect_stdout=redirect_stdout,
        redirect_stderr=redirect_stderr,
    )


__all__ = ["THEME", "console", "make_live", "make_progress"]
