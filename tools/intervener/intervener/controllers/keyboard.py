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
from datetime import UTC, datetime

from rich.console import Console

from intervener.binding import SessionBinding
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


class KeyboardController:
    def __init__(
        self,
        trigger_key: str = "i",
        console: Console | None = None,
        pause_wait_timeout: float = 60.0,
    ) -> None:
        if len(trigger_key) != 1:
            raise ValueError("trigger_key must be a single character")
        self._trigger = trigger_key.lower()
        self._console = console or Console()
        self._pause_wait_timeout = pause_wait_timeout

        self._binding: SessionBinding | None = None
        self._terminal_flag: threading.Event | None = None
        self._pause_landed = threading.Event()
        self._resume_landed = threading.Event()
        self._last_seq = 0
        self._thread: threading.Thread | None = None

    # ── EventSink half ─────────────────────────────────────────────

    def on_event(self, event: TrialEvent) -> None:
        self._last_seq = event.seq
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
