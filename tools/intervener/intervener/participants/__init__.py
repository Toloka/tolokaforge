"""Reference participants for the Trial Session gate.

Two shapes:

* :class:`Participant` — event-reactive abstract base with a single
  ``handle_event`` hook. :class:`LLMIntervener` and :class:`HumanIntervener`
  subclass it.
* :class:`ComposedParticipant` — compositional alternative that wires a
  list of :class:`~intervener.protocols.EventSink`\\ s and
  :class:`~intervener.protocols.InputController`\\ s around one
  :class:`~intervener.binding.SessionBinding`. Use this when you want an
  independent-thread input (keyboard, timer, HTTP) that isn't naturally
  event-reactive.
"""

from intervener.participants.base import (
    ComposedParticipant,
    EventReaction,
    Participant,
    ParticipantLog,
    SessionLogEntry,
)
from intervener.participants.human import HumanIntervener
from intervener.participants.llm import LLMIntervener

__all__ = [
    "ComposedParticipant",
    "EventReaction",
    "HumanIntervener",
    "LLMIntervener",
    "Participant",
    "ParticipantLog",
    "SessionLogEntry",
]
