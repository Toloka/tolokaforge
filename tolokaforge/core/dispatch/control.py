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

    ``trial_id`` is the canonical ``"{task_id}:{trial_index}"`` identifier.
    ``backend_ref`` carries an opaque backend-side id when the adapter has one.
    """

    trial_id: str
    backend_ref: str | None = None


@dataclass(frozen=True)
class JobStatus:
    """A dispatched job's state plus an optional human-readable detail."""

    state: JobState
    detail: str = ""
