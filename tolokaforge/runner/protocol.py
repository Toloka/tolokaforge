"""Engine/runner wire-protocol version and the vocabularies both ends translate.

The engine declares :data:`ENGINE_PROTOCOL_VERSION` on every
``RegisterTrialRequest``; the runner refuses to register a trial from an engine
that declares less than its own value. That places the version gate at
registration, before any tokens are spent — a per-call rejection would reach the
agent as an ordinary tool failure, and it would retry until the turn budget was
gone while the trial still reported ``status=completed``.

What each version is the first to change is the ledger in
``docs/GRPC_PROTOCOL.md`` § Version lock, which a canonical test holds level
with this constant.

:data:`RECORDED_STATUS_BY_PROTO` translates the wire's ``ExecutionStatus`` into
the recorded vocabulary. It lives here because both ends of the wire read it:
the runner stamps a status onto the call it just executed, and the engine
carries the response's status onto the :class:`ToolResult` it returns so the
production path records the full vocabulary instead of a bool.

:func:`parse_termination_reason` is the same kind of translation for
``GradeTrialRequest.termination_reason``: the engine writes
:attr:`TerminationReason.value`, the runner reads it back as the enum.

:class:`TrialNotRegisteredError` is the third: the one ``ExecutionStatus`` that
describes the trial rather than the call, translated into the exception the
engine terminates on instead of a result the agent would read as a tool failure.
"""

from tolokaforge.core.models import TerminationReason
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.tools.registry import ToolExecutionStatus

ENGINE_PROTOCOL_VERSION = 2

# EXECUTION_STATUS_TRIAL_NOT_FOUND is absent because the client raises
# TrialNotRegisteredError on it, and EXECUTION_STATUS_UNSPECIFIED because it names
# no outcome. Neither ever reaches a recorder.
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


class TrialNotRegisteredError(RuntimeError):
    """The runner holds no registration for the trial a tool call named.

    The call reached no tool, so there is nothing about it for either substrate
    to record and nothing in it the agent could have done differently. The
    engine ends the trial on it rather than handing it back as a tool failure.
    """

    def __init__(self, trial_id: str, tool_name: str) -> None:
        super().__init__(
            f"Trial {trial_id!r} is not registered on the runner, so its call to "
            f"{tool_name!r} reached no tool."
        )
        self.trial_id = trial_id
        self.tool_name = tool_name


def parse_termination_reason(raw: str) -> TerminationReason | None:
    """How the trial ended, as carried on ``GradeTrialRequest``.

    Empty means the engine reported no reason. Raises on an unrecognised
    non-empty value: coercing it would record the trial as having ended for no
    stated reason, which reads as a healthy trial rather than an unknown one.
    """
    if not raw:
        return None
    try:
        return TerminationReason(raw)
    except ValueError:
        accepted = sorted(reason.value for reason in TerminationReason)
        raise ValueError(
            f"termination_reason {raw!r} is not a known termination reason; "
            f"accepted values are {accepted}"
        ) from None
