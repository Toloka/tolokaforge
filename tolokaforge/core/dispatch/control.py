"""Control-plane value types for the dispatch seam: job identity, state, status.

These are the vocabulary the orchestrator uses to operate a dispatched job after
it is submitted — identify it, read its state, and name an outcome — independent
of any backend. :class:`JobState` reuses the run-queue status words so one set of
state names runs end to end; this module names no queue and no orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "JobHandle",
    "JobState",
    "JobStatus",
]


class JobState(StrEnum):
    """The state of a dispatched job, in the run-queue's own status vocabulary."""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JobHandle:
    """A backend-agnostic reference to one dispatched job.

    ``trial_id`` is the canonical ``"{task_id}:{trial_index}"`` identifier and
    ``attempt_id`` the 0-based attempt it names, so two attempts of the same
    trial produce distinct handles. ``backend_ref`` carries an opaque
    backend-side id when the adapter has one.
    """

    trial_id: str
    attempt_id: int = 0
    backend_ref: str | None = None

    @property
    def job_key(self) -> tuple[str, int]:
        """The per-attempt identity ``(trial_id, attempt_id)``, distinct for two
        attempts of one trial. Adapters key their per-job tables on it."""
        return (self.trial_id, self.attempt_id)


@dataclass(frozen=True)
class JobStatus:
    """A dispatched job's state plus an optional human-readable detail."""

    state: JobState
    detail: str = ""
