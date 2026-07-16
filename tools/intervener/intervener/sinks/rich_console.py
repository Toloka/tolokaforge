"""``RichConsoleSink`` — Rich panels for high-signal events, plain lines for the rest.

Turn boundaries render as bold cyan rules; assistant messages as
cyan-bordered panels; tool calls / results / pause / resume / terminal as
plain colored one-liners (Rich console still renders them).
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

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

__all__ = ["RichConsoleSink"]


class RichConsoleSink:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def on_event(self, event: TrialEvent) -> None:
        if isinstance(event, TurnStarted):
            self._console.rule(f"[bold cyan]turn {event.turn_index}  seq={event.seq}[/bold cyan]")
        elif isinstance(event, AssistantMessage):
            self._console.print(
                Panel(
                    event.content_preview,
                    title=f"assistant  seq={event.seq}",
                    border_style="cyan",
                )
            )
        elif isinstance(event, ToolCallEmitted):
            self._console.print(
                f"[magenta]→ tool[/magenta]      seq={event.seq}  "
                f"{event.tool_name}({event.arguments_preview[:80]})"
            )
        elif isinstance(event, ToolResultObserved):
            preview = event.truncated_preview.replace("\n", " ")[:80]
            self._console.print(
                f"[green]← result[/green]    seq={event.seq}  " f"{event.tool_name}  {preview}"
            )
        elif isinstance(event, PauseAcknowledged):
            self._console.print(
                f"[bold yellow on black] ⏸ PAUSED [/bold yellow on black]  "
                f"seq={event.seq}  by {event.triggered_by_participant}"
            )
        elif isinstance(event, ResumeAcknowledged):
            self._console.print(
                f"[bold green]▶ RESUMED[/bold green]  seq={event.seq}  "
                f"by {event.triggered_by_participant}"
            )
        elif isinstance(event, TerminalReached):
            self._console.rule(f"[bold red]■ TERMINAL  status={event.status}[/bold red]")

    def on_terminal(self) -> None:
        return None
