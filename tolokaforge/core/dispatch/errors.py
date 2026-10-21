"""Dispatch-neutral exceptions: failures a dispatch adapter reports, naming no transport."""

from __future__ import annotations

__all__ = [
    "DispatchProvisionError",
    "DispatchRefused",
    "DispatchTransient",
    "DispatchUnsupported",
]


class _DispatchFailure(RuntimeError):
    """A dispatch failure carrying a classified ``error_type`` / ``stage`` / ``reason``.

    Fields:
        error_type: The adapter's classification of the failure.
        stage: The step that failed (``preflight``, ``protocol``, ...), or empty.
        reason: What went wrong, naming what the operator must change.
    """

    def __init__(self, error_type: str, stage: str, reason: str) -> None:
        super().__init__(f"[{error_type}{f'/{stage}' if stage else ''}] {reason}")
        self.error_type = error_type
        self.stage = stage
        self.reason = reason


class DispatchRefused(_DispatchFailure):
    """The adapter refused the trial for a reason a retry cannot change."""


class DispatchTransient(_DispatchFailure):
    """The adapter could not serve the trial for an infrastructure reason a retry may clear."""


class DispatchProvisionError(_DispatchFailure):
    """A deterministic placement/start fault a retry cannot change.

    The sandbox could not be placed or started for a reason that stays broken
    across attempts — a bad compose file, a missing image, a malformed manifest.
    It maps to a non-retryable ``PROVISION_ERROR`` result. A backend that has no
    capacity *right now* (a transient "no capacity" condition) must raise
    ``DispatchTransient`` instead, so the orchestrator retries it.
    """


class DispatchUnsupported(DispatchRefused):
    """The backend cannot perform a requested control operation at all.

    A ``DispatchRefused`` subclass, so the orchestrator's existing non-retryable
    path handles it unchanged: a backend that cannot cancel, report status or
    collect a result says so honestly rather than pretending to.
    """
