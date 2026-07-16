"""Reference participants for the Trial Session gate.

The ``Participant`` abstract base is the shared contract. ``LLMCopilotParticipant``
and ``HumanCLIParticipant`` are the two reference implementations M2 ships.
Both consume ``tolokaforge.session.TrialEvents`` and produce
``tolokaforge.session.TrialIntervention``s.
"""

from copilot.participants.base import Participant, ParticipantLog, SessionLogEntry
from copilot.participants.human import HumanCLIParticipant
from copilot.participants.llm import LLMCopilotParticipant

__all__ = [
    "HumanCLIParticipant",
    "LLMCopilotParticipant",
    "Participant",
    "ParticipantLog",
    "SessionLogEntry",
]
