"""``PlainLineSink`` — one plain colored line per event to a stream.

Suitable for scripts, CI logs, tail -f monitoring. No panels, no rules —
just an ANSI-colored one-liner per event.
"""

from __future__ import annotations

import sys
from typing import TextIO

from tolokaforge.session import (
    AssistantMessage,
    PauseAcknowledged,
    ResumeAcknowledged,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TurnStarted,
)

__all__ = ["PlainLineSink"]

_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_YELLOW_BG = "\x1b[43;30m"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"
_MAGENTA = "\x1b[35m"
_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"


class PlainLineSink:
    def __init__(self, stream: TextIO | None = None, color: bool = True) -> None:
        self._stream = stream or sys.stdout
        self._color = color

    def on_event(self, event: TrialEvent) -> None:
        line = self._format(event) if self._color else self._plain(event)
        print(line, file=self._stream, flush=True)

    def on_terminal(self) -> None:
        return None

    def _format(self, event: TrialEvent) -> str:
        if isinstance(event, TurnStarted):
            return f"{_DIM}── turn {event.turn_index}  seq={event.seq} ──{_RESET}"
        if isinstance(event, AssistantMessage):
            preview = event.content_preview.replace("\n", " ")[:110]
            return f"{_CYAN}assistant{_RESET}   seq={event.seq}  {preview}"
        if isinstance(event, ToolCallEmitted):
            return (
                f"{_MAGENTA}→ tool{_RESET}      seq={event.seq}  "
                f"{event.tool_name}({event.arguments_preview[:80]})"
            )
        if isinstance(event, ToolResultObserved):
            preview = event.truncated_preview.replace("\n", " ")[:80]
            return f"{_GREEN}← result{_RESET}    seq={event.seq}  " f"{event.tool_name}  {preview}"
        if isinstance(event, PauseAcknowledged):
            return (
                f"{_BOLD}{_YELLOW_BG} ⏸ PAUSED {_RESET}  seq={event.seq}  "
                f"by {event.triggered_by_participant}"
            )
        if isinstance(event, ResumeAcknowledged):
            return (
                f"{_BOLD}{_GREEN}▶ RESUMED{_RESET}  seq={event.seq}  "
                f"by {event.triggered_by_participant}"
            )
        if isinstance(event, TerminalReached):
            return f"{_BOLD}{_RED}■ TERMINAL{_RESET}  seq={event.seq}  " f"status={event.status}"
        return f"{event.kind}  seq={event.seq}"

    def _plain(self, event: TrialEvent) -> str:
        if isinstance(event, TurnStarted):
            return f"── turn {event.turn_index}  seq={event.seq} ──"
        if isinstance(event, AssistantMessage):
            preview = event.content_preview.replace("\n", " ")[:110]
            return f"assistant   seq={event.seq}  {preview}"
        if isinstance(event, ToolCallEmitted):
            return f"→ tool      seq={event.seq}  {event.tool_name}({event.arguments_preview[:80]})"
        if isinstance(event, ToolResultObserved):
            preview = event.truncated_preview.replace("\n", " ")[:80]
            return f"← result    seq={event.seq}  {event.tool_name}  {preview}"
        if isinstance(event, PauseAcknowledged):
            return f"PAUSED  seq={event.seq}  by {event.triggered_by_participant}"
        if isinstance(event, ResumeAcknowledged):
            return f"RESUMED  seq={event.seq}  by {event.triggered_by_participant}"
        if isinstance(event, TerminalReached):
            return f"TERMINAL  seq={event.seq}  status={event.status}"
        return f"{event.kind}  seq={event.seq}"
