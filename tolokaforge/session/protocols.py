"""Protocols for the Trial Session gate.

Two symmetric Protocols form the contract between a running trial and any
attached participant. Transports are a separate concern — the Protocol says
*what* the exchange looks like, not *how* it travels.

* :class:`TrialEvents` — the trial publishes typed events.
* :class:`TrialInterventions` — participants submit typed interventions.
* :class:`TrialSession` — the pair, plus lifecycle and role.

Match the conventions of :mod:`tolokaforge.core.trial_grader` and
:mod:`tolokaforge.core.trial_executor` — narrow, `@runtime_checkable`,
value-in / value-out. See ``docs/OPEN_AGENT_LOOP.md`` for the design.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from tolokaforge.session.events import TrialEvent
from tolokaforge.session.interventions import InterventionAck, TrialIntervention

__all__ = [
    "ParticipantHandle",
    "ParticipantRole",
    "TrialEvents",
    "TrialInterventions",
    "TrialSession",
]


class ParticipantRole(str, Enum):
    """Role assigned to an attached participant.

    Priority for intervention conflict resolution is
    ``ADMIN > PARTICIPANT > OBSERVER``; later-wins within a tier. Every
    submitted intervention is recorded in the trajectory trace under open mode,
    including those superseded or rejected. See ``docs/OPEN_AGENT_LOOP.md`` §5.
    """

    OBSERVER = "observer"
    PARTICIPANT = "participant"
    ADMIN = "admin"


@dataclass(frozen=True)
class ParticipantHandle:
    """Opaque handle returned from :meth:`TrialSession.attach`.

    Carries the assigned ``participant_id`` and the confirmed ``role`` (which
    may have been downgraded by policy). Held by the caller and passed back
    to :meth:`TrialSession.detach`.
    """

    participant_id: str
    role: ParticipantRole
    trial_id: str


@runtime_checkable
class TrialEvents(Protocol):
    """Out-of-trial event stream — a participant subscribes and iterates
    events in monotonic ``seq`` order.

    The Protocol is deliberately narrow. Live in-process transports implement
    this over a thread-safe queue; the recorded transport implements it over
    a pre-loaded list. Slow consumers do not block the trial loop; a bounded
    per-participant queue drops laggards with a visible signal event (see
    ``docs/OPEN_AGENT_LOOP.md`` §5).
    """

    def iter_events(self, handle: ParticipantHandle) -> Iterator[TrialEvent]:
        """Yield events for the given attached participant in ``seq`` order.

        Blocks until the next event is available or the trial reaches its
        terminal state. Returns cleanly when the transport reports terminal.
        """
        ...


@runtime_checkable
class TrialInterventions(Protocol):
    """Into-trial intervention stream — a participant submits interventions
    and receives an :class:`InterventionAck` recording the outcome.

    Interventions are processed in submission order; conflict resolution uses
    the role priority in :class:`ParticipantRole`. Non-pause-time interventions
    queue and apply at the next pause point owned by the gated conductor.
    """

    def submit(
        self,
        handle: ParticipantHandle,
        intervention: TrialIntervention,
    ) -> InterventionAck:
        """Submit an intervention. Returns the accepted / queued / superseded /
        rejected outcome so the participant can react (retry, escalate, or
        give up)."""
        ...


@runtime_checkable
class TrialSession(Protocol):
    """The bus that pairs :class:`TrialEvents` and :class:`TrialInterventions`
    for one trial, with lifecycle (``attach`` / ``detach``) and multi-participant
    semantics.

    A session is a broadcast bus for events and a serialised queue for
    interventions. Multiple participants may attach concurrently; each sees
    every event; interventions are labelled and applied per the role-priority
    rule. See ``docs/OPEN_AGENT_LOOP.md`` §5.
    """

    @property
    def trial_id(self) -> str:
        """The trial this session belongs to."""
        ...

    def attach(
        self,
        participant_id: str,
        role: ParticipantRole,
    ) -> ParticipantHandle:
        """Register a participant. May downgrade the requested role by policy;
        the confirmed role lives on the returned :class:`ParticipantHandle`.
        """
        ...

    def detach(self, handle: ParticipantHandle) -> None:
        """Deregister a participant. Idempotent."""
        ...

    def events(self) -> TrialEvents:
        """Return the events Protocol implementation bound to this session."""
        ...

    def interventions(self) -> TrialInterventions:
        """Return the interventions Protocol implementation bound to this
        session."""
        ...
