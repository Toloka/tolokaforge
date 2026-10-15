"""The runner container's environment for the run's runner limits.

Two run-config settings reach a runner container only as environment
variables: ``compute.grpc_max_message_mb`` (:data:`MAX_MESSAGE_ENV_VAR`) and
``compute.state_read_timeout_s`` (:data:`STATE_READ_TIMEOUT_ENV_VAR`). The
built-in stack sets them on the engine-owned runner and materialisation sets
them on a task-declared one, both from :func:`runner_limits_env`, so the two
paths cannot drift in which variables they set or how they spell a value.

This module imports nothing beyond the standard library and the runner's two
limit modules, so the stack factories can share it without the materialisation
machinery.
"""

from __future__ import annotations

from tolokaforge.runner.grpc_limits import MAX_MESSAGE_ENV_VAR, validate_max_message_mb
from tolokaforge.runner.state_read_limits import (
    STATE_READ_TIMEOUT_ENV_VAR,
    validate_state_read_timeout_s,
)

__all__ = ["runner_limits_env"]


def runner_limits_env(*, grpc_max_message_mb: int, state_read_timeout_s: float) -> dict[str, str]:
    """Each runner limit variable, validated and spelled as the runner parses it.

    Raises:
        ValueError: a value is outside its setting's accepted range.
    """
    return {
        MAX_MESSAGE_ENV_VAR: str(validate_max_message_mb(grpc_max_message_mb)),
        STATE_READ_TIMEOUT_ENV_VAR: repr(validate_state_read_timeout_s(state_read_timeout_s)),
    }
