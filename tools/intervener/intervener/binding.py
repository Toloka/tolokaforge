"""``SessionBinding`` — thin per-participant façade over :class:`TrialSession`.

One binding == one ``session.attach()``. Owns the ``ParticipantHandle`` and
exposes just what a controller needs: ``submit()``, ``handle``, ``trial_id``.
Sinks receive events from the outer drain loop, not through the binding.
"""

from __future__ import annotations

from tolokaforge.session import (
    InterventionAck,
    ParticipantHandle,
    ParticipantRole,
    TrialIntervention,
    TrialSession,
)

__all__ = ["SessionBinding"]


class SessionBinding:
    """Attaches to a :class:`TrialSession` on construction; ``detach`` is idempotent."""

    def __init__(
        self,
        session: TrialSession,
        participant_id: str,
        role: ParticipantRole,
    ) -> None:
        self._session = session
        self._handle = session.attach(participant_id, role)
        self._detached = False

    @property
    def session(self) -> TrialSession:
        return self._session

    @property
    def handle(self) -> ParticipantHandle:
        return self._handle

    @property
    def trial_id(self) -> str:
        return self._handle.trial_id

    @property
    def participant_id(self) -> str:
        return self._handle.participant_id

    @property
    def role(self) -> ParticipantRole:
        return self._handle.role

    def submit(self, intervention: TrialIntervention) -> InterventionAck:
        """Submit an intervention through the session bus. Returns the ack."""
        return self._session.interventions().submit(self._handle, intervention)

    def detach(self) -> None:
        """Idempotent — safe to call multiple times."""
        if self._detached:
            return
        self._detached = True
        try:
            self._session.detach(self._handle)
        except Exception:
            pass
