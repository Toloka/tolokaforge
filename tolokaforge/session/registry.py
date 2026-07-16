"""``SessionRegistry`` — per-run store of live :class:`InProcessTrialSession`
handles keyed by ``trial_id``.

Held by the orchestrator when a run is in open mode. Every trial that
enters the run body claims (or creates on first access) its session
through :meth:`get_or_create`; external participants — a CLI attach
subcommand, a web UI, a cross-trial orchestrator — look sessions up by
``trial_id`` through :meth:`get` to attach.

Thread-safe under the same threading model as
:class:`InProcessTrialSession`: many worker threads may call
:meth:`get_or_create` concurrently; each ``trial_id`` maps to exactly
one session for the lifetime of the run.
"""

from __future__ import annotations

import threading

from tolokaforge.session.in_process import InProcessTrialSession

__all__ = ["SessionRegistry"]


class SessionRegistry:
    """Thread-safe map of ``trial_id`` → :class:`InProcessTrialSession`.

    A single registry lives on the orchestrator for the duration of a
    run. Sessions are created lazily on first ``get_or_create`` — the
    orchestrator does not know its full trial list up front (retries and
    resumes may add new ``trial_id``s at any time).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, InProcessTrialSession] = {}
        self._lock = threading.RLock()

    def get_or_create(self, trial_id: str) -> InProcessTrialSession:
        """Return the session for ``trial_id``, creating one on first access.

        Idempotent — repeated calls with the same ``trial_id`` return the
        same instance for the registry's lifetime.
        """
        with self._lock:
            session = self._sessions.get(trial_id)
            if session is None:
                session = InProcessTrialSession(trial_id=trial_id)
                self._sessions[trial_id] = session
            return session

    def get(self, trial_id: str) -> InProcessTrialSession | None:
        """Return the session for ``trial_id`` if one has been created, else
        ``None``. External participants use this to find the session they
        want to attach to without accidentally creating a fresh empty one.
        """
        with self._lock:
            return self._sessions.get(trial_id)

    def all_trial_ids(self) -> list[str]:
        """Snapshot of every ``trial_id`` currently registered.

        For diagnostics and CLI listing. The underlying set may change
        between the read and any subsequent operation.
        """
        with self._lock:
            return list(self._sessions.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __contains__(self, trial_id: object) -> bool:
        if not isinstance(trial_id, str):
            return False
        with self._lock:
            return trial_id in self._sessions
