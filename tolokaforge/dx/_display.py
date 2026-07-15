"""Shared display primitives for the tolokaforge CLI.

Every CLI module imports :data:`console` from here instead of instantiating
its own ``rich.Console`` — one theme, one stream posture, one place to change.

Public surface:

- :data:`THEME` — semantic-token palette (``info``, ``warn``, ``error``,
  ``success``, ``muted``, ``cost``, ``link``).
- :data:`console` — shared ``rich.Console`` writing to stderr with soft wrap
  and :data:`THEME` installed.
- :func:`emit_artifact_path` — the one sanctioned ``sys.stdout`` write in the
  CLI; every human/progress/log line goes through :data:`console` (stderr).
- :func:`format_duration` — shared ``MM:SS`` / ``HH:MM:SS`` formatter for
  wall-clock spans (run-end banner, live-panel ETA, and anything else that
  needs a uniform duration shape).
- :func:`make_progress` — factory for ``rich.progress.Progress`` bound to
  :data:`console` with the CLI's default column set.
- :func:`make_live` — factory for ``rich.live.Live`` bound to :data:`console`.
- :class:`DisplayMode` — the overall stderr UI selection enum
  (``full``/``rich``/``plain``/``log``/``none``).
- :func:`select_display_mode` — pure resolver that applies the precedence
  chain (explicit flag > env var > ``CI`` > isatty > plain).
- :func:`silence_console` — sets ``console.quiet = True`` for
  ``--display=none``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import TextIO

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


def emit_artifact_path(path: Path | str) -> None:
    """Write the resolved absolute path to ``sys.stdout`` with a trailing
    newline, flushed.

    This is the ONE stdout write the CLI is allowed. Every human, progress,
    and log line goes through the shared :data:`console` (stderr) or
    ``configure_root_logging`` (stderr). The whole emitted line is the
    resolved absolute path — no prefix, no colour, no markup — so shell
    idioms like ``RUN_DIR=$(tolokaforge run --config …)`` capture the
    artifact cleanly. ``Path(..).resolve()`` canonicalises symlinks and
    guarantees an absolute path regardless of caller cwd.
    """
    print(str(Path(path).resolve()), file=sys.stdout, flush=True)


def format_duration(seconds: float) -> str:
    """Render a wall-clock span as ``MM:SS`` under one hour, ``HH:MM:SS`` above.

    Truncates to whole seconds via ``int(seconds)`` — no rounding, no
    fractional display. Zero-padded fields. Callers compose the "unknown"
    case themselves (this helper never returns ``"n/a"``).
    """
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


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
    """Return a ``Progress`` bound to the module's shared console.

    ``columns`` overrides the default column set entirely. Passing ``None``
    yields the CLI's opinionated set: spinner, description, bar, m/n,
    percent, elapsed, remaining.

    The default ``console`` is early-bound at import time — reassigning
    ``_display.console`` afterwards does not reach this default; pass
    ``console=`` explicitly to redirect.
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
    """Return a ``Live`` bound to the module's shared console.

    The default ``console`` is early-bound at import time — reassigning
    ``_display.console`` afterwards does not reach this default; pass
    ``console=`` explicitly to redirect.
    """

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


class DisplayMode(str, Enum):
    """Overall stderr UI selection for a tolokaforge invocation.

    Values match the CLI flag literals; ``(str, Enum)`` inheritance means
    ``click.Choice([m.value for m in DisplayMode])`` accepts them without a
    coercion adapter.
    """

    FULL = "full"
    RICH = "rich"
    PLAIN = "plain"
    LOG = "log"
    NONE = "none"


_CI_FALSY: frozenset[str] = frozenset({"0", "false", "False", "FALSE", "no", "off", ""})


def _resolve_mode(value: str, *, source: str) -> DisplayMode:
    accepted = ", ".join(m.value for m in DisplayMode)
    try:
        return DisplayMode(value.lower())
    except ValueError:
        raise ValueError(f"invalid {source} value {value!r}; expected one of: {accepted}") from None


def select_display_mode(
    *,
    explicit: str | DisplayMode | None = None,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> DisplayMode:
    """Resolve the effective display mode.

    Precedence (highest first): ``explicit`` > ``env["TOLOKAFORGE_DISPLAY"]``
    > ``env["CI"]`` truthy → ``PLAIN`` > ``stream.isatty()`` → ``RICH`` > ``PLAIN``.

    ``env`` defaults to :data:`os.environ`; ``stream`` defaults to
    :data:`sys.stderr`. Unrecognised values in ``explicit`` or the env var
    raise :class:`ValueError` naming the accepted set. Does NOT apply the
    Textual fallback — that lives at the CLI callback so the fallback log
    line can render under the active ``--log-format``.
    """
    if explicit is not None:
        if isinstance(explicit, DisplayMode):
            return explicit
        return _resolve_mode(explicit, source="--display")

    env_map = env if env is not None else os.environ

    env_display = env_map.get("TOLOKAFORGE_DISPLAY", "")
    if env_display:
        return _resolve_mode(env_display, source="TOLOKAFORGE_DISPLAY")

    if env_map.get("CI", "") not in _CI_FALSY:
        return DisplayMode.PLAIN

    target_stream = stream if stream is not None else sys.stderr
    if target_stream.isatty():
        return DisplayMode.RICH
    return DisplayMode.PLAIN


def silence_console() -> None:
    """Set ``console.quiet = True`` on the shared console.

    Rich's ``Console.quiet`` short-circuits every write at buffer-check
    time — no bytes reach the wrapped stream. Idempotent.
    """
    console.quiet = True


def _textual_available() -> bool:
    """Return ``True`` iff :mod:`textual` is importable.

    Uses :func:`importlib.util.find_spec` so the module is not actually
    imported — the CLI callback only needs to know whether ``--display=full``
    should fall back to ``--display=rich``.
    """
    try:
        return importlib.util.find_spec("textual") is not None
    except (ImportError, ValueError):
        return False


__all__ = [
    "DisplayMode",
    "THEME",
    "console",
    "emit_artifact_path",
    "format_duration",
    "make_live",
    "make_progress",
    "select_display_mode",
    "silence_console",
]
