"""``HumanIntervener`` — reference human participant.

Renders every event to a Rich console. At natural seams (assistant turn,
tool result, terminal) prompts the operator for an intervention. Empty
input skips (observe-only); a leading ``/`` selects a structured
intervention (``/inject <text>``, ``/kill <reason>``, ``/pause``, ``/resume``).
Plain text is treated as an :class:`InjectMessage` shortcut.

Composes with the ``feat/terminal-dx`` display layer at the seam level — we
use Rich here directly to avoid pulling terminal-dx internals into the peer
package before that branch merges.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from rich.console import Console
from rich.panel import Panel

from intervener.participants.base import EventReaction, Participant
from tolokaforge.session import (
    AssistantMessage,
    InjectMessage,
    Kill,
    ParticipantHandle,
    ParticipantRole,
    Pause,
    Resume,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TrialIntervention,
    TrialSession,
    TurnStarted,
)

__all__ = ["HumanIntervener"]


class HumanIntervener(Participant):
    """Interactive human participant driven from stdin.

    ``non_interactive_script`` — optional list of lines to feed in place of
    stdin, for tests and non-TTY demos. Consumed one entry per prompt.
    """

    def __init__(
        self,
        participant_id: str = "human_intervener",
        role: ParticipantRole = ParticipantRole.PARTICIPANT,
        console: Console | None = None,
        non_interactive_script: list[str] | None = None,
    ) -> None:
        super().__init__(participant_id=participant_id, role=role)
        self._console = console or Console(stderr=True)
        self._script = list(non_interactive_script) if non_interactive_script is not None else None

    def handle_event(
        self,
        event: TrialEvent,
        handle: ParticipantHandle,
        session: TrialSession,
    ) -> EventReaction:
        self._render(event)
        if not self._is_prompt_seam(event):
            return EventReaction()

        raw = self._prompt().strip()
        if not raw:
            return EventReaction(note="observer: no intervention")

        intervention = self._parse(raw, handle=handle, at_seq=event.seq)
        if intervention is None:
            return EventReaction(note=f"unrecognised command: {raw!r}")
        return EventReaction(
            intervention=intervention,
            note=f"command: {raw[:80]}",
        )

    def _is_prompt_seam(self, event: TrialEvent) -> bool:
        return isinstance(event, (AssistantMessage, ToolResultObserved, TerminalReached))

    def _prompt(self) -> str:
        if self._script is not None:
            if not self._script:
                return ""
            return self._script.pop(0)
        if not sys.stdin.isatty():
            return ""
        try:
            return input("intervene> ")
        except EOFError:
            return ""

    def _parse(
        self,
        raw: str,
        handle: ParticipantHandle,
        at_seq: int,
    ) -> TrialIntervention | None:
        now = datetime.now(UTC)
        base = {
            "trial_id": handle.trial_id,
            "attach_to_seq": at_seq,
            "participant_id": handle.participant_id,
            "timestamp": now,
        }
        if not raw.startswith("/"):
            return InjectMessage(**base, content=raw)

        parts = raw[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd == "inject" and arg:
            return InjectMessage(**base, content=arg)
        if cmd == "kill" and arg:
            return Kill(**base, reason=arg)
        if cmd == "pause":
            return Pause(**base, reason=arg or None)
        if cmd == "resume":
            return Resume(**base)
        return None

    def _render(self, event: TrialEvent) -> None:
        if isinstance(event, TurnStarted):
            self._console.rule(f"[bold]turn {event.turn_index}[/bold]  seq={event.seq}")
            return
        if isinstance(event, AssistantMessage):
            self._console.print(Panel(event.content_preview, title=f"assistant  seq={event.seq}"))
            return
        if isinstance(event, ToolCallEmitted):
            self._console.print(
                f"[cyan]tool_call[/cyan] seq={event.seq} {event.tool_name}({event.arguments_preview})"
            )
            return
        if isinstance(event, ToolResultObserved):
            self._console.print(
                f"[magenta]tool_result[/magenta] seq={event.seq} {event.tool_name} "
                f"({event.duration_ms}ms) -> {event.truncated_preview}"
            )
            return
        if isinstance(event, TerminalReached):
            self._console.rule(f"[bold red]terminal[/bold red]  status={event.status.value}")
            return
        self._console.print(f"[dim]{event.kind} seq={event.seq}[/dim]")
