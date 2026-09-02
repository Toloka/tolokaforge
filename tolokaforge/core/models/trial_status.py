"""Trial status + termination reason enums, imported by both the trajectory
wire type and the grade wire type.

Split from :mod:`tolokaforge.core.models.trajectory` so that
:class:`~tolokaforge.core.models.grade.Grade` can reference
:class:`TerminationReason` without closing an import cycle:
``trajectory`` imports :class:`Grade` (for :attr:`Trajectory.grade`), and
:class:`Grade` types :attr:`Grade.synthesized_by_termination_reason` as a
:class:`TerminationReason`.
"""

from enum import Enum


class TrialStatus(str, Enum):
    """Trial execution status"""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class TerminationReason(str, Enum):
    """Reason why the dialogue was terminated"""

    AGENT_DONE = "agent_done"  # Agent had no further action and no party could ask for one
    USER_STOP = "user_stop"  # User signaled ###STOP###
    STUCK_DETECTED = "stuck_detected"  # Stuck condition detected
    TIMEOUT = "timeout"  # Episode timeout reached
    MAX_TURNS = "max_turns"  # Maximum turns limit reached
    ERROR = "error"  # Runtime error occurred
    RATE_LIMIT = "rate_limit"  # API rate limit error
    API_TIMEOUT = "api_timeout"  # API call timed out after retries
    API_ERROR = "api_error"  # Other API errors
    EMPTY_COMPLETION = "empty_completion"  # Provider returned no text and no tool calls
    PROVISION_ERROR = "provision_error"  # Substrate provisioning failed before the trial body ran
    TRIAL_LOST = "trial_lost"  # The substrate no longer holds the trial the engine was running
