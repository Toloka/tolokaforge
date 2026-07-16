"""intervener — reference sinks, controllers, and participants for the OAL gate.

Top-level re-exports let callers write ``from intervener import …`` for the
common types without descending into submodules. Deeper machinery
(``intervener.sinks``, ``intervener.controllers``,
``intervener.participants``) remains available for advanced use.
"""

from intervener.binding import SessionBinding
from intervener.controllers import (
    EventReactiveController,
    KeyboardController,
    ScriptedController,
    TimerController,
)
from intervener.participants import (
    ComposedParticipant,
    EventReaction,
    HumanIntervener,
    LLMIntervener,
    Participant,
    ParticipantLog,
    SessionLogEntry,
)
from intervener.protocols import EventSink, InputController
from intervener.sinks import (
    CompoundSink,
    JsonlSink,
    PlainLineSink,
    RichConsoleSink,
    SilentSink,
)

__version__ = "0.1.0"

__all__ = [
    "ComposedParticipant",
    "CompoundSink",
    "EventReaction",
    "EventReactiveController",
    "EventSink",
    "HumanIntervener",
    "InputController",
    "JsonlSink",
    "KeyboardController",
    "LLMIntervener",
    "Participant",
    "ParticipantLog",
    "PlainLineSink",
    "RichConsoleSink",
    "ScriptedController",
    "SessionBinding",
    "SessionLogEntry",
    "SilentSink",
    "TimerController",
]
