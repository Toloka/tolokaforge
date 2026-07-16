"""``Participant`` — shared contract for LLM and human co-pilots.

Both reference implementations bind to the same abstract base: consume events
from a :class:`~tolokaforge.session.TrialEvents`, submit interventions via a
:class:`~tolokaforge.session.TrialInterventions`, and produce a structured
session log so both participant paths emit identical trace shape.

The base drives the event loop. Subclasses only implement ``handle_event``
which returns zero or one :class:`~tolokaforge.session.TrialIntervention` per
event (plus a diagnostic ``note`` if it wants one on the log).

For a compositional alternative (multiple sinks + independent-thread
controllers like keyboard / timer / HTTP), see :class:`ComposedParticipant`
in the same module. ``Participant`` is the event-reactive shape;
``ComposedParticipant`` supports both event-reactive and independent
input controllers.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from intervener.binding import SessionBinding
from intervener.protocols import EventSink, InputController
from tolokaforge.session import (
    InterventionAck,
    ParticipantHandle,
    ParticipantRole,
    TerminalReached,
    TrialEvent,
    TrialIntervention,
    TrialSession,
)

__all__ = [
    "ComposedParticipant",
    "EventReaction",
    "Participant",
    "ParticipantLog",
    "SessionLogEntry",
]


@dataclass(frozen=True)
class EventReaction:
    """Subclass return type from :meth:`Participant.handle_event`.

    ``intervention`` is optional — a participant may observe without acting.
    ``note`` is an optional diagnostic string included in the session log.
    ``payload`` is optional structured data attached to the log (e.g. the
    full :class:`InterventionSuggestion` for the LLM path).
    """

    intervention: TrialIntervention | None = None
    note: str | None = None
    payload: dict[str, Any] | None = None


class SessionLogEntry(BaseModel):
    """One entry in the participant's session log.

    Both participant types produce entries with identical shape — this is
    the proof that the contract is genuinely shared.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: str
    participant_id: str
    event_seq: int
    event_kind: str
    ack_outcome: str | None = None
    ack_reason: str | None = None
    intervention_kind: str | None = None
    note: str | None = None
    payload: dict[str, Any] | None = None
    at: datetime


@dataclass
class ParticipantLog:
    """In-memory session log for one participant across one session."""

    entries: list[SessionLogEntry] = field(default_factory=list)

    def to_yaml_dict(self) -> list[dict[str, Any]]:
        return [entry.model_dump(mode="json") for entry in self.entries]


class Participant(ABC):
    """Abstract base — implementers override :meth:`handle_event` (and,
    optionally, :meth:`on_terminal`).
    """

    def __init__(
        self, participant_id: str, role: ParticipantRole = ParticipantRole.PARTICIPANT
    ) -> None:
        self.participant_id = participant_id
        self.role = role
        self.log = ParticipantLog()

    def run(self, session: TrialSession) -> ParticipantLog:
        """Attach, drain events, dispatch to :meth:`handle_event`, log every step,
        detach, return the accumulated :class:`ParticipantLog`.
        """
        handle = session.attach(self.participant_id, self.role)
        try:
            for event in session.events().iter_events(handle):
                reaction = self.handle_event(event, handle, session)
                ack = self._maybe_submit(session, handle, reaction.intervention)
                self._log(handle, event, reaction, ack)
            self.on_terminal(handle, session)
        finally:
            session.detach(handle)
        return self.log

    @abstractmethod
    def handle_event(
        self,
        event: TrialEvent,
        handle: ParticipantHandle,
        session: TrialSession,
    ) -> EventReaction:
        """React to a single event. Return an :class:`EventReaction` with an
        optional intervention, note, and structured payload for the log."""
        ...

    def on_terminal(self, handle: ParticipantHandle, session: TrialSession) -> None:
        """Optional hook, invoked after the last event is processed.

        Subclasses override for cleanup; default no-op returns without side effects.
        """
        return None

    def _maybe_submit(
        self,
        session: TrialSession,
        handle: ParticipantHandle,
        intervention: TrialIntervention | None,
    ) -> InterventionAck | None:
        if intervention is None:
            return None
        return session.interventions().submit(handle, intervention)

    def _log(
        self,
        handle: ParticipantHandle,
        event: TrialEvent,
        reaction: EventReaction,
        ack: InterventionAck | None,
    ) -> None:
        self.log.entries.append(
            SessionLogEntry(
                trial_id=handle.trial_id,
                participant_id=handle.participant_id,
                event_seq=event.seq,
                event_kind=event.kind,
                ack_outcome=ack.outcome if ack else None,
                ack_reason=ack.reason if ack else None,
                intervention_kind=reaction.intervention.kind if reaction.intervention else None,
                note=reaction.note,
                payload=reaction.payload,
                at=datetime.now(UTC),
            )
        )


class ComposedParticipant:
    """Compositional alternative to :class:`Participant`.

    Wires N :class:`EventSink`\\ s and M :class:`InputController`\\ s around
    a single :class:`SessionBinding`. On :meth:`run`:

    1. Attach as a participant.
    2. Start each controller (may spawn background threads).
    3. Drain events on the calling thread; forward each to every sink and to
       any controller that also implements :class:`EventSink` (event-reactive
       controllers).
    4. On :class:`~tolokaforge.session.TerminalReached`, set the shared
       terminal event; controllers observe it to exit cleanly.
    5. Stop controllers, call ``on_terminal`` on sinks, detach.

    A :class:`ParticipantLog` is maintained with one entry per event drained.
    Independent-thread submissions (keyboard, timer) are NOT added to
    ``ParticipantLog`` — they are captured by the OAL session trace
    (``open_agent_loop.yaml``) which is the authoritative record for
    submissions.
    """

    def __init__(
        self,
        participant_id: str,
        role: ParticipantRole = ParticipantRole.PARTICIPANT,
        sinks: list[EventSink] | None = None,
        controllers: list[InputController] | None = None,
    ) -> None:
        self.participant_id = participant_id
        self.role = role
        self.sinks: list[EventSink] = list(sinks or [])
        self.controllers: list[InputController] = list(controllers or [])
        self.log = ParticipantLog()

    def run(self, session: TrialSession) -> ParticipantLog:
        binding = SessionBinding(session, self.participant_id, self.role)
        terminal = threading.Event()

        listeners: list[EventSink] = list(self.sinks)
        for ctrl in self.controllers:
            if hasattr(ctrl, "on_event") and ctrl not in listeners:
                listeners.append(ctrl)  # type: ignore[arg-type]

        for ctrl in self.controllers:
            ctrl.start(binding, terminal)

        try:
            for event in session.events().iter_events(binding.handle):
                for listener in listeners:
                    try:
                        listener.on_event(event)
                    except Exception:
                        pass
                self._log_event(binding.handle, event)
                if isinstance(event, TerminalReached):
                    terminal.set()
                    break
        finally:
            for ctrl in self.controllers:
                try:
                    ctrl.stop()
                except Exception:
                    pass
            for listener in listeners:
                try:
                    listener.on_terminal()
                except Exception:
                    pass
            binding.detach()
        return self.log

    def _log_event(self, handle: ParticipantHandle, event: TrialEvent) -> None:
        self.log.entries.append(
            SessionLogEntry(
                trial_id=handle.trial_id,
                participant_id=handle.participant_id,
                event_seq=event.seq,
                event_kind=event.kind,
                ack_outcome=None,
                ack_reason=None,
                intervention_kind=None,
                note=None,
                payload=None,
                at=datetime.now(UTC),
            )
        )
