"""Engine/runner wire-protocol version and its recorded-status vocabulary.

The engine declares :data:`ENGINE_PROTOCOL_VERSION` on every
``RegisterTrialRequest``; the runner refuses to register a trial from an engine
that declares less than its own value. That places the version gate at
registration, before any tokens are spent — a per-call rejection would reach the
agent as an ordinary tool failure, and it would retry until the turn budget was
gone while the trial still reported ``status=completed``.

Version 1 is the first that sends ``ExecuteToolRequest.call_id``. Without it a
tool call cannot be joined to the result it produced, so a runner built from
this tree cannot grade a trial driven by an older engine.

:data:`RECORDED_STATUS_BY_PROTO` translates the wire's ``ExecutionStatus`` into
the recorded vocabulary. It lives here because both ends of the wire read it:
the runner stamps a status onto the call it just executed, and the engine
carries the response's status onto the :class:`ToolResult` it returns so the
production path records the full vocabulary instead of a bool.
"""

from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.tools.registry import ToolExecutionStatus

ENGINE_PROTOCOL_VERSION = 1

# EXECUTION_STATUS_TRIAL_NOT_FOUND is absent because there is no trial context to
# record into, and EXECUTION_STATUS_UNSPECIFIED because it names no outcome.
RECORDED_STATUS_BY_PROTO: dict[int, ToolExecutionStatus] = {
    pb2.EXECUTION_STATUS_SUCCESS: ToolExecutionStatus.SUCCESS,
    pb2.EXECUTION_STATUS_ERROR: ToolExecutionStatus.ERROR,
    pb2.EXECUTION_STATUS_TIMEOUT: ToolExecutionStatus.TIMEOUT,
    pb2.EXECUTION_STATUS_TOOL_NOT_FOUND: ToolExecutionStatus.TOOL_NOT_FOUND,
    pb2.EXECUTION_STATUS_INVALID_ARGUMENTS: ToolExecutionStatus.INVALID_ARGUMENTS,
}


def recorded_status(status: int) -> ToolExecutionStatus:
    """The status a trial records for a call that reached a tool.

    Raises for a status outside :data:`RECORDED_STATUS_BY_PROTO`: a call the
    runner routed to a tool must record how it ended, and a status with no
    record would read as a call that never happened.
    """
    recorded = RECORDED_STATUS_BY_PROTO.get(status)
    if recorded is None:
        raise ValueError(
            f"ExecutionStatus {status} has no recordable ToolExecutionStatus, so the tool "
            "call it describes could not be recorded."
        )
    return recorded
