"""Persistent tmux terminal: one long-lived pane per trial, keystrokes in,
new terminal output back.

:mod:`~tolokaforge.tools.tmux_terminal.session` holds the backend seam and the
session engine; :mod:`~tolokaforge.tools.tmux_terminal.pure` holds every
decision that needs no terminal (capture-range arithmetic, keystroke batching,
probe parsing, observation rendering).

=========================================================================
THIRD-PARTY ATTRIBUTION — THIS CODE HAS BEEN MODIFIED
=========================================================================
This package is derived from ``harbor/agents/terminus_2/tmux_session.py`` in
harbor 0.23.0, Copyright the harbor authors, licensed under the Apache
License, Version 2.0. Upstream ships no NOTICE file. A copy of the Apache
License, Version 2.0 is at the repository root (``LICENSE``); tolokaforge is
distributed under the same licence.

Both derived modules carry their own statement of modification and a list of
what changed; ``session.py`` has the full list.
"""

from tolokaforge.tools.tmux_terminal.pure import (
    CURRENT_SCREEN_HEADER,
    EVICTED_MARKER,
    NEW_OUTPUT_HEADER,
    CaptureKind,
    CapturePlan,
    PaneStatus,
    PaneWatermark,
    format_status_line,
    parse_pane_probe,
    plan_capture,
    render_observation,
)
from tolokaforge.tools.tmux_terminal.session import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_PANE_HEIGHT,
    DEFAULT_PANE_WIDTH,
    DockerExecTmuxBackend,
    ExecResult,
    LocalTmuxBackend,
    TmuxBackend,
    TmuxSessionError,
    TmuxTerminalSession,
    TmuxUnavailableError,
)

__all__ = [
    "CURRENT_SCREEN_HEADER",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_PANE_HEIGHT",
    "DEFAULT_PANE_WIDTH",
    "EVICTED_MARKER",
    "NEW_OUTPUT_HEADER",
    "CaptureKind",
    "CapturePlan",
    "DockerExecTmuxBackend",
    "ExecResult",
    "LocalTmuxBackend",
    "PaneStatus",
    "PaneWatermark",
    "TmuxBackend",
    "TmuxSessionError",
    "TmuxTerminalSession",
    "TmuxUnavailableError",
    "format_status_line",
    "parse_pane_probe",
    "plan_capture",
    "render_observation",
]
