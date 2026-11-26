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
:class:`RunnerUnreachableError` is the same ending for a call the runner never
answered, and both are :class:`TrialLostError`.
"""

from tolokaforge.core.models import TerminationReason
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.tools.registry import ENVIRONMENT_ERROR_STATUSES, ToolExecutionStatus

ENGINE_PROTOCOL_VERSION = 10

# EXECUTION_STATUS_TRIAL_NOT_FOUND is absent because the client raises
# TrialNotRegisteredError on it, and EXECUTION_STATUS_UNSPECIFIED because it names
# no outcome. Neither ever reaches a recorder.
RECORDED_STATUS_BY_PROTO: dict[int, ToolExecutionStatus] = {
    pb2.EXECUTION_STATUS_SUCCESS: ToolExecutionStatus.SUCCESS,
    pb2.EXECUTION_STATUS_ENVIRONMENT_ERROR: ToolExecutionStatus.ENVIRONMENT_ERROR,
    pb2.EXECUTION_STATUS_TOOL_ERROR: ToolExecutionStatus.TOOL_ERROR,
    pb2.EXECUTION_STATUS_ERROR: ToolExecutionStatus.ERROR,
    pb2.EXECUTION_STATUS_TIMEOUT: ToolExecutionStatus.TIMEOUT,
    pb2.EXECUTION_STATUS_TOOL_NOT_FOUND: ToolExecutionStatus.TOOL_NOT_FOUND,
    pb2.EXECUTION_STATUS_INVALID_ARGUMENTS: ToolExecutionStatus.INVALID_ARGUMENTS,
}


#: Statuses of a call the tool completed — a success, or an ``isError`` reply,
#: untyped or the tool's declared refusal (:data:`ENVIRONMENT_ERROR_STATUSES`). The
#: agent reads such a call's output verbatim, and its record keeps that output
#: rather than an error message.
COMPLETED_EXECUTION_STATUSES: frozenset[int] = frozenset(
    proto
    for proto, status in RECORDED_STATUS_BY_PROTO.items()
    if status is ToolExecutionStatus.SUCCESS or status in ENVIRONMENT_ERROR_STATUSES
)


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


class TrialLostError(RuntimeError):
    """A tool call could not reach the trial's tools through the runner.

    There is no tool outcome for either substrate to record, and nothing in the
    call the agent could have done differently, so the engine ends the trial on
    it (``termination_reason: trial_lost``) rather than handing it back as a tool
    failure the agent would read as its own and retry against. Each subclass
    names one way the trial was lost.
    """

    def __init__(self, message: str, *, trial_id: str, tool_name: str) -> None:
        super().__init__(message)
        self.trial_id = trial_id
        self.tool_name = tool_name


class TrialNotRegisteredError(TrialLostError):
    """The runner holds no registration for the trial a tool call named.

    The call reached no tool, so there is nothing about it for either substrate
    to record and nothing in it the agent could have done differently.
    """

    def __init__(self, trial_id: str, tool_name: str) -> None:
        super().__init__(
            f"Trial {trial_id!r} is not registered on the runner, so its call to "
            f"{tool_name!r} reached no tool.",
            trial_id=trial_id,
            tool_name=tool_name,
        )


class RunnerUnreachableError(TrialLostError):
    """The runner could not be reached for a tool call (gRPC ``UNAVAILABLE``).

    Every later call of the trial would fail the same way: the agent's tools all
    run through the one runner. Handed back as a tool failure, each would read as
    the agent's own error, and the agent spends the rest of its episode retrying
    commands that never reach its container. Seen on a trial whose agent starved
    the machine: from one call on, ``echo hi`` and ``true`` each came back as
    ``UNAVAILABLE`` after minutes, until the episode's time ran out.
    """

    def __init__(self, trial_id: str, tool_name: str, detail: str) -> None:
        super().__init__(
            f"The runner could not be reached for trial {trial_id!r}'s call to "
            f"{tool_name!r}: {detail}",
            trial_id=trial_id,
            tool_name=tool_name,
        )


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
