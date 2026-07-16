"""``ScriptedController`` — submits a canned list of interventions.

Two use cases:

* Time-scheduled — supply ``(delay_seconds, intervention)`` pairs. The
  controller sleeps between each, honouring the terminal event.
* Line-triggered — supply a list of raw strings (as the human REPL accepts:
  bare text, ``/kill …``, ``/pause``, etc.) plus a ``line_parser`` callback
  that maps a line to a :class:`TrialIntervention`. Used by
  :class:`~intervener.participants.human.HumanIntervener`'s
  ``non_interactive_script`` back-compat path — one line consumed per
  ``on_event`` at a prompt seam.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from intervener.binding import SessionBinding
from tolokaforge.session import TrialEvent, TrialIntervention

__all__ = ["ScriptedController"]

LineParser = Callable[[str, TrialEvent, SessionBinding], TrialIntervention | None]
SeamPredicate = Callable[[TrialEvent], bool]


class ScriptedController:
    """Two disjoint modes:

    * ``timed_script=[(delay_s, intervention), …]`` → own thread; sleeps,
      submits, sleeps.
    * ``lines`` + ``line_parser`` + ``seam_predicate`` → event-reactive;
      pops one line per seam event and submits the parsed intervention.

    Provide exactly one mode. Providing both is a configuration error.
    """

    def __init__(
        self,
        *,
        timed_script: Iterable[tuple[float, TrialIntervention]] | None = None,
        lines: Iterable[str] | None = None,
        line_parser: LineParser | None = None,
        seam_predicate: SeamPredicate | None = None,
    ) -> None:
        if timed_script is not None and lines is not None:
            raise ValueError("ScriptedController: pass timed_script OR lines, not both")
        if lines is not None and line_parser is None:
            raise ValueError("ScriptedController: lines mode requires line_parser")

        self._timed = list(timed_script) if timed_script is not None else None
        self._lines = list(lines) if lines is not None else None
        self._parser = line_parser
        self._seam = seam_predicate or (lambda _e: True)
        self._binding: SessionBinding | None = None
        self._thread: threading.Thread | None = None
        self._terminal: threading.Event | None = None

    def start(self, binding: SessionBinding, terminal: threading.Event) -> None:
        self._binding = binding
        self._terminal = terminal
        if self._timed is not None:
            self._thread = threading.Thread(
                target=self._run_timed, name="scripted-timed", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._binding = None
        # thread is daemon; drops when process ends

    def on_event(self, event: TrialEvent) -> None:
        if self._lines is None or self._binding is None or self._parser is None:
            return
        if not self._seam(event):
            return
        if not self._lines:
            return
        line = self._lines.pop(0)
        intervention = self._parser(line, event, self._binding)
        if intervention is not None:
            self._binding.submit(intervention)

    def on_terminal(self) -> None:
        return None

    def _run_timed(self) -> None:
        assert self._timed is not None
        assert self._terminal is not None
        for delay, intervention in self._timed:
            if self._terminal.wait(timeout=delay):
                return
            if self._binding is None:
                return
            self._binding.submit(intervention)
