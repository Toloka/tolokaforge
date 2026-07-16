"""Trial Session — the participant gate for a running trial.

This subpackage defines the ``TrialSession`` seam that lets an external
participant — a human, an LLM copilot, a rule-based monitor, a cross-trial
orchestrator — attach to a trial, receive typed events, and submit typed
interventions. See ``docs/OPEN_AGENT_LOOP.md`` for the full design.

Two symmetric Protocols form the contract:

* :class:`TrialEvents` — out-of-trial event stream.
* :class:`TrialInterventions` — into-trial intervention stream.

A :class:`TrialSession` pairs the two with lifecycle (``attach`` / ``detach``),
a ``participant_id``, and a role (``ParticipantRole``). Transports (in-process,
recorded, Unix-socket, WebSocket) are separate concerns from the Protocols.

M0 ships the Protocols, the event and intervention discriminated unions, and
:class:`RecordedTrialSession` — a transport that replays a captured trajectory
event-by-event so participants can be developed and tested without a live
conductor. M1 adds :class:`InProcessTrialSession`, the live thread-safe
transport driven by the :class:`GatedConductor`.
"""

from __future__ import annotations

from tolokaforge.session.events import (
    AssistantMessage,
    BudgetUpdate,
    PauseAcknowledged,
    ResumeAcknowledged,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TrialEventEnvelope,
    TurnStarted,
)
from tolokaforge.session.in_process import InProcessTrialSession, QueuedIntervention
from tolokaforge.session.interventions import (
    ApproveTool,
    EditState,
    InjectMessage,
    InterventionAck,
    Kill,
    Pause,
    RejectTool,
    Resume,
    TrialIntervention,
)
from tolokaforge.session.loop_observer import SessionLoopObserver
from tolokaforge.session.protocols import (
    ParticipantHandle,
    ParticipantRole,
    TrialEvents,
    TrialInterventions,
    TrialSession,
)
from tolokaforge.session.recorded import RecordedTrialSession

__all__ = [
    "ApproveTool",
    "AssistantMessage",
    "BudgetUpdate",
    "EditState",
    "InProcessTrialSession",
    "InjectMessage",
    "InterventionAck",
    "Kill",
    "ParticipantHandle",
    "ParticipantRole",
    "Pause",
    "PauseAcknowledged",
    "QueuedIntervention",
    "RecordedTrialSession",
    "RejectTool",
    "Resume",
    "ResumeAcknowledged",
    "SessionLoopObserver",
    "TerminalReached",
    "ToolCallEmitted",
    "ToolResultObserved",
    "TrialEvent",
    "TrialEventEnvelope",
    "TrialEvents",
    "TrialIntervention",
    "TrialInterventions",
    "TrialSession",
    "TurnStarted",
]
