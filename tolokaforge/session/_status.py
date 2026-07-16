"""Enum mirrors for :mod:`tolokaforge.session`.

The session module deliberately does **not** import from
:mod:`tolokaforge.core.models`. Doing so would drag the full gRPC runner
stack (``tolokaforge.runner`` transitives) into every process that consumes
the session gate — including lightweight external participants like a web
UI or a cross-trial orchestrator that only care about typed session events.

The values here are the canonical trial status vocabulary; they must stay
identical to the ``TrialStatus`` / ``TerminationReason`` enums in
``tolokaforge/core/models.py``. A single canonical home for these enums
(likely a new ``tolokaforge/core/status.py`` with re-exports from
``core.models``) is the correct long-term shape and can land as a follow-up.
This module is the deliberate seam that decouples the session gate today.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["TerminationReason", "TrialStatus"]


class TrialStatus(str, Enum):
    """Terminal status of a trial. Values mirror
    :class:`tolokaforge.core.models.TrialStatus`.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class TerminationReason(str, Enum):
    """Reason a trial terminated. Values mirror
    :class:`tolokaforge.core.models.TerminationReason`.
    """

    AGENT_DONE = "agent_done"
    USER_STOP = "user_stop"
    STUCK_DETECTED = "stuck_detected"
    TIMEOUT = "timeout"
    MAX_TURNS = "max_turns"
    ERROR = "error"
    RATE_LIMIT = "rate_limit"
    API_TIMEOUT = "api_timeout"
    API_ERROR = "api_error"
    PROVISION_ERROR = "provision_error"
