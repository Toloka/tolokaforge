"""``InProcessTrialSession`` — thread-safe in-process transport for the Trial Session gate.

The M1 live transport. Where :class:`RecordedTrialSession` (M0) replays a
captured trajectory for participant development, this transport carries a
**running** trial's event stream out to attached participants and their
interventions back to the conductor.

Threading model — one producer, many consumers:

* Producer: the trial-running thread (M1's ``GatedConductor``). Calls
  :meth:`next_seq` to allocate monotonic per-trial sequence numbers,
  :meth:`publish` to broadcast a :class:`TrialEvent`, and
  :meth:`drain_pending_interventions` at natural pause points to collect
  what's queued.
* Consumers: attached participants, each on their own thread. Call
  :meth:`iter_events` to block on their own event queue, and
  :meth:`submit` to hand an intervention back to the producer.

The event stream is broadcast: every attached participant sees every
event in ``seq`` order. Interventions are serialised into a single
producer-facing queue and processed later; :meth:`submit` returns
``outcome="queued"`` immediately without waiting for the conductor to
decide accepted / superseded / rejected — that verdict lives in the
trajectory trace once the conductor's pause pump processes it.

The Protocol from :mod:`tolokaforge.session.protocols` is deliberately
transport-agnostic; this concrete class adds the producer-side methods
(:meth:`publish`, :meth:`drain_pending_interventions`, :meth:`next_seq`,
:meth:`close`) that the sealed Protocol has no reason to declare.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tolokaforge.session.events import TerminalReached, TrialEvent
from tolokaforge.session.interventions import InterventionAck, TrialIntervention
from tolokaforge.session.protocols import (
    ParticipantHandle,
    ParticipantRole,
    TrialEvents,
    TrialInterventions,
)

__all__ = ["InProcessTrialSession", "QueuedIntervention"]

_TERMINAL_SENTINEL: Any = object()


@dataclass(frozen=True)
class QueuedIntervention:
    """One pending intervention, with the handle of the participant that submitted it.

    Producer (:class:`GatedConductor`) receives a list of these from
    :meth:`InProcessTrialSession.drain_pending_interventions` and decides
    accept / supersede / reject per the role-priority rule (see
    ``docs/OPEN_AGENT_LOOP.md`` §5).
    """

    handle: ParticipantHandle
    intervention: TrialIntervention
    submitted_at: datetime


@dataclass
class _InterventionRecord:
    """Durable trace-side record of one submitted intervention.

    Kept separate from :class:`QueuedIntervention` (which lives on the
    volatile producer queue and gets drained by the pause pump) — this
    record persists for the trial's lifetime and lands in the
    ``open_agent_loop`` trajectory-trace section. Includes the ack outcome
    that ``submit`` returned; the intervention pump updates
    ``ack_outcome`` and ``ack_reason`` in-place via
    :meth:`InProcessTrialSession.record_intervention_outcome` once it
    processes the intervention (``queued`` → ``accepted`` / ``rejected``).
    """

    trial_id: str
    participant_id: str
    intervention: TrialIntervention
    ack_outcome: str
    ack_reason: str | None
    submitted_at: datetime


@dataclass
class _ParticipantSlot:
    """Per-participant queue + handle. One slot per attached participant."""

    handle: ParticipantHandle
    event_queue: queue.Queue = field(default_factory=queue.Queue)


class InProcessTrialSession:
    """Live in-process implementation of :class:`~tolokaforge.session.TrialSession`.

    Instantiated by the orchestrator once per open-mode trial. The gated
    conductor holds a reference and publishes events into it; attached
    participants drain events from their own slot queues and submit
    interventions back through the session.

    Late attach receives events from the current ``seq`` forward — a
    participant that attaches at ``seq=5`` never sees events 0..4.
    Historical replay of a live trial belongs to a separate transport
    (see :class:`~tolokaforge.session.RecordedTrialSession` for the
    read-only equivalent).
    """

    def __init__(self, trial_id: str) -> None:
        self._trial_id = trial_id
        self._participants: dict[str, _ParticipantSlot] = {}
        self._intervention_queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._seq_counter = 0
        self._closed = False
        # History captured for the durable open_agent_loop trace. Every
        # published event lands here in seq order regardless of whether any
        # participant is attached; every intervention submitted through
        # :meth:`submit` is recorded with the ack outcome so the trace
        # replays deterministically once written to disk (see
        # :meth:`snapshot`). Bounded implicitly by the trial's turn budget.
        self._event_history: list[TrialEvent] = []
        self._intervention_history: list[_InterventionRecord] = []

    # ------------------------------------------------------------------
    # TrialSession Protocol
    # ------------------------------------------------------------------

    @property
    def trial_id(self) -> str:
        return self._trial_id

    def attach(
        self,
        participant_id: str,
        role: ParticipantRole,
    ) -> ParticipantHandle:
        """Register a participant on the session. Idempotent-friendly:
        double-attach of the same participant id raises ``ValueError``.
        Late attach after :meth:`close` also raises — a closed session
        does not accept new participants.
        """
        with self._lock:
            if self._closed:
                raise ValueError(f"Cannot attach {participant_id!r} to a closed session.")
            if participant_id in self._participants:
                raise ValueError(
                    f"Participant {participant_id!r} is already attached to this session."
                )
            handle = ParticipantHandle(
                participant_id=participant_id,
                role=role,
                trial_id=self._trial_id,
            )
            self._participants[participant_id] = _ParticipantSlot(handle=handle)
            return handle

    def detach(self, handle: ParticipantHandle) -> None:
        """Deregister a participant. Idempotent — detaching an already-
        detached handle is a no-op. Any in-flight :meth:`iter_events`
        call on this handle returns cleanly (the queue is signalled with
        a terminal sentinel).
        """
        with self._lock:
            slot = self._participants.pop(handle.participant_id, None)
        if slot is not None:
            slot.event_queue.put(_TERMINAL_SENTINEL)

    def events(self) -> TrialEvents:
        return self

    def interventions(self) -> TrialInterventions:
        return self

    # ------------------------------------------------------------------
    # TrialEvents Protocol
    # ------------------------------------------------------------------

    def iter_events(self, handle: ParticipantHandle) -> Iterator[TrialEvent]:
        """Block on the participant's slot queue and yield events until the
        session is closed or the participant is detached.
        """
        slot = self._require_slot(handle)
        while True:
            event = slot.event_queue.get()
            if event is _TERMINAL_SENTINEL:
                return
            yield event

    # ------------------------------------------------------------------
    # TrialInterventions Protocol
    # ------------------------------------------------------------------

    def submit(
        self,
        handle: ParticipantHandle,
        intervention: TrialIntervention,
    ) -> InterventionAck:
        """Enqueue the intervention for the conductor to process at its
        next pause point. Returns ``outcome="queued"`` synchronously —
        the accept / supersede / reject verdict is decided later and
        recorded in the trajectory trace, not returned here.
        """
        if intervention.trial_id != self._trial_id:
            raise ValueError(
                f"Intervention trial_id={intervention.trial_id!r} does not match "
                f"session trial_id={self._trial_id!r}"
            )
        if intervention.participant_id != handle.participant_id:
            raise ValueError(
                f"Intervention participant_id={intervention.participant_id!r} does not match "
                f"handle participant_id={handle.participant_id!r}"
            )
        # Confirm the handle is still attached; a detached participant
        # cannot submit further interventions.
        self._require_slot(handle)
        submitted_at = datetime.now(UTC)
        self._intervention_queue.put(
            QueuedIntervention(
                handle=handle,
                intervention=intervention,
                submitted_at=submitted_at,
            )
        )
        ack = InterventionAck(
            intervention_kind=intervention.kind,
            trial_id=self._trial_id,
            participant_id=handle.participant_id,
            outcome="queued",
            reason=None,
        )
        with self._lock:
            self._intervention_history.append(
                _InterventionRecord(
                    trial_id=self._trial_id,
                    participant_id=handle.participant_id,
                    intervention=intervention,
                    ack_outcome=ack.outcome,
                    ack_reason=ack.reason,
                    submitted_at=submitted_at,
                )
            )
        return ack

    # ------------------------------------------------------------------
    # Producer-side API (concrete class, not on the Protocol)
    # ------------------------------------------------------------------

    def next_seq(self) -> int:
        """Allocate the next monotonic ``seq`` for a fresh event. Called
        by the producer just before constructing a :class:`TrialEvent`.
        """
        with self._lock:
            seq = self._seq_counter
            self._seq_counter += 1
            return seq

    def publish(self, event: TrialEvent) -> None:
        """Broadcast an event to every attached participant.

        If the event is a :class:`TerminalReached`, the session
        auto-closes after fanning the event out: every slot receives a
        terminal sentinel and subsequent :meth:`attach` / :meth:`publish`
        calls raise.

        Publishing after close is a programmer error and raises
        ``RuntimeError`` — the sealed conductor would never do this;
        catching this early avoids silent event loss.

        Records the event on the durable history buffer regardless of
        whether any participant is attached, so the ``open_agent_loop``
        trajectory-trace snapshot is faithful even for unattached open-mode
        trials.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    f"Cannot publish {event.kind!r} to a closed session (trial_id={self._trial_id!r})."
                )
            self._event_history.append(event)
            for slot in self._participants.values():
                slot.event_queue.put(event)
            if isinstance(event, TerminalReached):
                self._closed = True
                for slot in self._participants.values():
                    slot.event_queue.put(_TERMINAL_SENTINEL)

    def drain_pending_interventions(self, max_batch: int | None = None) -> list[QueuedIntervention]:
        """Non-blocking pull of all interventions submitted since the last drain.

        Called by the conductor's pause pump at natural seams (before
        each turn, before a tool call dispatch, on ``Pause``). Returns
        interventions in submission order. ``max_batch`` caps the pull;
        ``None`` means "everything currently queued".
        """
        drained: list[QueuedIntervention] = []
        while True:
            if max_batch is not None and len(drained) >= max_batch:
                break
            try:
                drained.append(self._intervention_queue.get_nowait())
            except queue.Empty:
                break
        return drained

    def record_intervention_outcome(
        self,
        intervention: TrialIntervention,
        outcome: str,
        reason: str | None = None,
    ) -> None:
        """Update the durable trace record for ``intervention`` with the
        outcome the pump decided.

        Matches records by object identity (the pump processes the same
        ``TrialIntervention`` instance that was submitted, so ``is`` is
        the correct comparison). No-op when the intervention isn't found —
        the trace-side write path must never mask a live-trial result.
        """
        with self._lock:
            for record in self._intervention_history:
                if record.intervention is intervention:
                    record.ack_outcome = outcome
                    record.ack_reason = reason
                    return

    def close(self) -> None:
        """Force-close the session even without a :class:`TerminalReached`.

        Provided for error paths — a conductor that crashes or times
        out without publishing a proper terminal event must call this to
        release any blocked :meth:`iter_events` iterators. Idempotent.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for slot in self._participants.values():
                slot.event_queue.put(_TERMINAL_SENTINEL)

    @property
    def is_closed(self) -> bool:
        return self._closed

    def snapshot(self) -> dict[str, Any]:
        """Serialisable snapshot of everything published + submitted for this
        trial, in the shape the ``open_agent_loop`` trajectory-trace section
        persists to disk.

        Returns a plain ``dict`` (not a Pydantic model) so callers can drop
        it straight into a YAML dump without wrestling with model shapes.
        Every event and intervention round-trips through Pydantic v2's
        ``model_dump(mode="json")`` so datetime / enum / discriminator
        handling matches the wire format the session module already uses
        elsewhere.
        """
        with self._lock:
            events = [event.model_dump(mode="json") for event in self._event_history]
            interventions = [
                {
                    "trial_id": record.trial_id,
                    "participant_id": record.participant_id,
                    "submitted_at": record.submitted_at.isoformat(),
                    "ack_outcome": record.ack_outcome,
                    "ack_reason": record.ack_reason,
                    "intervention": record.intervention.model_dump(mode="json"),
                }
                for record in self._intervention_history
            ]
        return {
            "trial_id": self._trial_id,
            "closed": self._closed,
            "events": events,
            "interventions": interventions,
        }

    @property
    def attached_participant_ids(self) -> list[str]:
        """Snapshot of currently attached participant ids. For diagnostics
        and tests; the underlying set can change between the read and any
        subsequent call.
        """
        with self._lock:
            return list(self._participants.keys())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_slot(self, handle: ParticipantHandle) -> _ParticipantSlot:
        with self._lock:
            slot = self._participants.get(handle.participant_id)
        if slot is None:
            raise ValueError(
                f"Participant {handle.participant_id!r} is not attached to this session "
                f"(trial_id={self._trial_id!r})."
            )
        if slot.handle.trial_id != handle.trial_id:
            raise ValueError(
                f"Handle trial_id={handle.trial_id!r} does not match session "
                f"trial_id={self._trial_id!r}."
            )
        return slot
