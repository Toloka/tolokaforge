"""``KeyboardController`` — raw-key trigger + step-mode REPL for a live TTY human.

Runs a background thread that reads single keypresses in cbreak mode. When
the configured trigger key (default ``i``) is pressed, submits a ``Pause``,
waits for :class:`PauseAcknowledged` to land, then opens a line-based REPL:

* Enter → step one turn (Resume + Pause pair). Waits for the next
  ``PauseAcknowledged`` before re-prompting so the step's events are
  visible.
* bare text → submit ``InjectMessage`` AND step one turn, so the operator
  sees the agent's reaction without a second Enter.
* ``/quit`` → resume, leave REPL (trial runs to natural end).
* ``/kill <reason>`` → ``Kill`` and end.
* ``/help`` / ``/?`` / ``?`` → reprint hint block.
* ``/<tool-name> <args>`` → invoke a tool from the ``tools`` registry
  (see :class:`~intervener.tools.ToolRegistry`). Tool output is printed
  to the console; a rolling window of recent events is passed in via
  :class:`~intervener.tools.ToolContext`.

Prints its own prompt and hints to a supplied ``Console`` (or the default).
Event *display* is not this controller's job — pair it with an
:class:`~intervener.sinks.RichConsoleSink` for that.

Also implements :class:`~intervener.protocols.EventSink` so it can watch
for ``PauseAcknowledged`` / ``TerminalReached`` and time the REPL prompt
correctly.
"""

from __future__ import annotations

import sys
import termios
import threading
import time
import tty
from collections import deque
from datetime import UTC, datetime
from typing import Any

from rich.console import Console

from intervener.binding import SessionBinding
from intervener.tools.base import LLMCallable, ToolContext
from intervener.tools.registry import ToolRegistry
from tolokaforge.session import (
    InjectMessage,
    Kill,
    Pause,
    PauseAcknowledged,
    Resume,
    ResumeAcknowledged,
    TerminalReached,
    TrialEvent,
)

__all__ = ["KeyboardController"]

_BUILTIN_COMMANDS = frozenset({"quit", "kill", "help", "?"})


class KeyboardController:
    def __init__(
        self,
        trigger_key: str = "i",
        console: Console | None = None,
        pause_wait_timeout: float = 60.0,
        tools: ToolRegistry | None = None,
        task_metadata: dict[str, Any] | None = None,
        llm_call: LLMCallable | None = None,
        recent_events_maxlen: int = 200,
    ) -> None:
        if len(trigger_key) != 1:
            raise ValueError("trigger_key must be a single character")
        if tools is not None:
            for tool_name, _ in tools.list_summary():
                if tool_name in _BUILTIN_COMMANDS:
                    raise ValueError(
                        f"tool name {tool_name!r} collides with a built-in REPL command"
                    )
        self._trigger = trigger_key.lower()
        self._console = console or Console()
        self._pause_wait_timeout = pause_wait_timeout
        self._tools = tools
        self._task_metadata = task_metadata
        self._llm_call = llm_call

        self._binding: SessionBinding | None = None
        self._terminal_flag: threading.Event | None = None
        self._pause_landed = threading.Event()
        self._resume_landed = threading.Event()
        self._last_seq = 0
        self._thread: threading.Thread | None = None
        self._recent_events: deque[TrialEvent] = deque(maxlen=recent_events_maxlen)

    # ── EventSink half ─────────────────────────────────────────────

    def on_event(self, event: TrialEvent) -> None:
        self._last_seq = event.seq
        self._recent_events.append(event)
        if isinstance(event, PauseAcknowledged):
            self._pause_landed.set()
        elif isinstance(event, ResumeAcknowledged):
            self._resume_landed.set()
        elif isinstance(event, TerminalReached) and self._terminal_flag is not None:
            self._terminal_flag.set()

    def on_terminal(self) -> None:
        return None

    # ── InputController half ───────────────────────────────────────

    def start(self, binding: SessionBinding, terminal: threading.Event) -> None:
        self._binding = binding
        self._terminal_flag = terminal
        self._thread = threading.Thread(target=self._loop, name="keyboard-controller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._binding = None

    # ── implementation ─────────────────────────────────────────────

    def _loop(self) -> None:
        assert self._terminal_flag is not None
        while not self._terminal_flag.is_set():
            key = _read_one_key()
            if self._terminal_flag.is_set():
                return
            if key in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
                return
            if key.lower() != self._trigger:
                continue
            if self._binding is None:
                return
            self._enter_repl()

    def _enter_repl(self) -> None:
        binding = self._binding
        if binding is None:
            return

        self._pause_landed.clear()
        self._submit_pause(binding, "human control")
        self._console.print("[dim](waiting for pause to land at next turn boundary…)[/dim]")
        self._pause_landed.wait(timeout=self._pause_wait_timeout)
        if self._terminal_flag is not None and self._terminal_flag.is_set():
            return
        time.sleep(0.15)
        self._print_hints()

        while self._terminal_flag is None or not self._terminal_flag.is_set():
            try:
                cmd = input("\x1b[1mintervene>\x1b[0m ").strip()
            except EOFError:
                cmd = "/continue"

            if self._terminal_flag is not None and self._terminal_flag.is_set():
                return

            if cmd == "":
                self._step(binding)
                continue
            if cmd == "/quit":
                self._submit_resume(binding)
                self._console.print(
                    f"[dim](leaving REPL — trial resumes; press '{self._trigger}' again to return)[/dim]"
                )
                return
            if cmd in ("/help", "/?", "?"):
                self._print_hints()
                continue
            if cmd.startswith("/kill"):
                reason = cmd[5:].strip() or "user kill"
                self._submit_kill(binding, reason)
                self._console.print(
                    "[bold red] ═══ KILL SUBMITTED — trial terminating ═══ [/bold red]"
                )
                return
            if cmd.startswith("/") and self._tools is not None:
                parts = cmd[1:].split(maxsplit=1)
                tool_name = parts[0]
                tool_args = parts[1] if len(parts) > 1 else ""
                tool = self._tools.get(tool_name)
                if tool is not None:
                    self._invoke_tool(tool, tool_args, binding)
                    continue
            if cmd.startswith("/"):
                self._console.print(f"[red]unknown command: {cmd}[/red]")
                self._print_hints()
                continue
            # Bare text — inject the message AND step, so the operator sees
            # the agent's reaction without needing a second Enter.
            self._submit_inject(binding, cmd)
            self._step(binding)

    def _step(self, binding: SessionBinding) -> None:
        """Advance the paused trial by exactly one turn.

        Two-phase: Resume, wait for the pump to actually exit its pause loop
        (``ResumeAcknowledged``), then submit Pause so it lands at the *next*
        turn's top-of-turn drain — not the current pause-loop's inner drain
        (where it would be rejected).
        """
        self._console.print("[dim]stepping…[/dim]")
        self._resume_landed.clear()
        self._pause_landed.clear()
        self._submit_resume(binding)
        self._resume_landed.wait(timeout=self._pause_wait_timeout)
        if self._terminal_flag is not None and self._terminal_flag.is_set():
            return
        self._submit_pause(binding, "step")
        self._pause_landed.wait(timeout=self._pause_wait_timeout)
        time.sleep(0.15)

    def _invoke_tool(self, tool, tool_args: str, binding: SessionBinding) -> None:
        context = ToolContext(
            binding=binding,
            recent_events=list(self._recent_events),
            task_metadata=self._task_metadata,
            console=self._console,
            llm_call=self._llm_call,
        )
        try:
            result = tool.run(tool_args, context)
        except Exception as exc:
            self._console.print(f"[red]tool {tool.name!r} raised {type(exc).__name__}: {exc}[/red]")
            return
        self._console.print(result.output)
        if result.submitted_interventions:
            self._console.print(
                f"[dim](tool submitted {result.submitted_interventions} intervention"
                f"{'s' if result.submitted_interventions != 1 else ''})[/dim]"
            )

    def _print_hints(self) -> None:
        self._console.print(
            "\n[bold black on yellow] ═══ INTERVENING — trial is paused ═══ [/bold black on yellow]"
        )
        self._console.print(
            "[dim]  [bold]Enter[/bold]          → step one turn\n"
            "  [bold]bare text[/bold]      → inject the message AND step one turn\n"
            "  [bold]/quit[/bold]          → leave REPL, let trial run to end\n"
            "  [bold]/kill <reason>[/bold] → terminate the trial now\n"
            "  [bold]/help[/bold]          → show this hint again[/dim]"
        )
        if self._tools is not None and len(self._tools) > 0:
            self._console.print("[dim italic]Registered tools:[/dim italic]")
            for name, description in self._tools.list_summary():
                self._console.print(f"[dim]  [bold]/{name}[/bold]{'':10s}→ {description}[/dim]")

    def _env(self, binding: SessionBinding) -> dict:
        return {
            "trial_id": binding.trial_id,
            "attach_to_seq": self._last_seq,
            "participant_id": binding.participant_id,
            "timestamp": datetime.now(UTC),
        }

    def _submit_pause(self, binding: SessionBinding, reason: str) -> None:
        ack = binding.submit(Pause(**self._env(binding), reason=reason))
        self._console.print(f"[dim]    → pause: {ack.outcome}[/dim]")

    def _submit_resume(self, binding: SessionBinding) -> None:
        ack = binding.submit(Resume(**self._env(binding)))
        self._console.print(f"[dim]    → resume: {ack.outcome}[/dim]")

    def _submit_kill(self, binding: SessionBinding, reason: str) -> None:
        ack = binding.submit(Kill(**self._env(binding), reason=reason))
        self._console.print(f"[dim]    → kill: {ack.outcome}[/dim]")

    def _submit_inject(self, binding: SessionBinding, content: str) -> None:
        ack = binding.submit(InjectMessage(**self._env(binding), content=content))
        preview = content if len(content) <= 100 else content[:97] + "…"
        self._console.print(
            f"[dim]    → inject_message: {ack.outcome}  [/dim]" f'[italic]"{preview}"[/italic]'
        )


def _read_one_key() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
