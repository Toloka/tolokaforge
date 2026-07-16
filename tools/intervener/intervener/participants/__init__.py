"""Reference participants for the Trial Session gate.

The ``Participant`` abstract base is the shared contract. ``LLMIntervener``
and ``HumanIntervener`` are the two reference implementations M2 ships.
Both consume ``tolokaforge.session.TrialEvents`` and produce
``tolokaforge.session.TrialIntervention``s.
"""

from intervener.participants.base import Participant, ParticipantLog, SessionLogEntry
from intervener.participants.human import HumanIntervener
from intervener.participants.llm import LLMIntervener

__all__ = [
    "HumanIntervener",
    "LLMIntervener",
    "Participant",
    "ParticipantLog",
    "SessionLogEntry",
]
