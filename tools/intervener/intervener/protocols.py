"""Two narrow Protocols the intervener composes.

* :class:`EventSink` — pure read side. Receives each event from the session bus.
  Reference impls in :mod:`intervener.sinks`.
* :class:`InputController` — pure write side. Decides *when* and *what* to
  submit into the session. Reference impls in :mod:`intervener.controllers`.

A :class:`~intervener.participants.base.ComposedParticipant` wires N sinks
and M controllers around one :class:`~intervener.binding.SessionBinding`.
An event-reactive controller (e.g. LLM drafter) also implements
:class:`EventSink` so its submissions are triggered by events; an
independent controller (e.g. keyboard) ignores events and submits when its
own trigger fires.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from intervener.binding import SessionBinding
from tolokaforge.session import TrialEvent

__all__ = ["EventSink", "InputController"]


@runtime_checkable
class EventSink(Protocol):
    """Receives every event drained from the session bus.

    Sinks are read-only — they never submit interventions. They may render to
    a terminal, persist to disk, forward over a socket, or accumulate for
    later inspection. Called on the drain thread; keep ``on_event`` cheap or
    hand off to a background worker.
    """

    def on_event(self, event: TrialEvent) -> None: ...

    def on_terminal(self) -> None:
        """Optional cleanup hook after the terminal event has been dispatched.
        Called exactly once per session run."""


@runtime_checkable
class InputController(Protocol):
    """Decides when and what to submit into the session.

    ``start`` is called once when the binding is ready. Implementations may
    spawn a background thread (independent controllers like keyboard, HTTP,
    timer) or just cache the binding (event-reactive controllers, which
    additionally implement :class:`EventSink`). ``start`` MUST return
    promptly — long-running work belongs on a background thread.

    ``stop`` is called on teardown. Idempotent. Long-running threads owned
    by a controller should honour the passed-in ``terminal`` event during
    ``start`` and exit when it fires.
    """

    def start(self, binding: SessionBinding, terminal: threading.Event) -> None: ...

    def stop(self) -> None: ...
