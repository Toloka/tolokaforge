""":class:`TrialStatus` and :class:`TerminationReason` live here rather than in
:mod:`tolokaforge.core.models.trajectory` because :class:`Grade` references
:class:`TerminationReason` and :class:`Trajectory` references :class:`Grade` —
housing both enums in ``trajectory`` would close an import cycle.
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
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"  # Wire history exceeded the provider's max input tokens and no summarize recovery was possible
    PROVISION_ERROR = "provision_error"  # Substrate provisioning failed before the trial body ran
    TRIAL_LOST = "trial_lost"  # The substrate no longer holds the trial the engine was running
