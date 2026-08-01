"""Driving a real ``GradeTrial`` against the in-process runner servicer.

:class:`ServicerBackend` runs the real ``GrpcRunnerClient.grade_trial``
proto→dict mapping against a real servicer, so the host grader sees the dict
production builds — no Docker, no gRPC channel.
:func:`register_collided_trial` stages the cheapest deterministic refusal that
servicer can be made to produce.
"""

from __future__ import annotations

import json
from typing import Any

from tests.canonical._factories import make_trajectory
from tests.utils.runner_requests import execute_request, register_request, trial_spec_json
from tolokaforge.core.models import (
    Message,
    MessageRole,
    TerminationReason,
    ToolCall,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.runner import runner_pb2 as pb2

DUPLICATE_CALL_ID = "toolu_dup"
"""The ``call_id`` :func:`register_collided_trial` records twice."""


class ServicerStub:
    """A gRPC stub over the in-process servicer."""

    def __init__(self, service: Any, context: Any) -> None:
        self._service = service
        self._context = context

    def GradeTrial(self, request):  # noqa: N802 — matches the gRPC stub method name
        return self._service.GradeTrial(request, self._context)


class ServicerBackend:
    """``RuntimeBackend.grade_trial`` backed by the in-process servicer."""

    def __init__(self, service: Any, context: Any) -> None:
        self._client = GrpcRunnerClient(runner_address="unused:0")
        self._client.stub = ServicerStub(service, context)

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._client.grade_trial(
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
            termination_reason=termination_reason,
        )


def register_collided_trial(
    service: Any,
    context: Any,
    task_description: dict[str, Any],
    *,
    trial_id: str,
) -> str:
    """Register *trial_id* and record two tool calls sharing one ``call_id``.

    Nothing rejects a duplicate at record time, so the collision reaches grade
    time and the timeline cannot join a call to its result — ``GradeTrial``
    then refuses without a live provider or a hand-written error string.
    Returns *trial_id*.
    """
    registered = service.RegisterTrial(
        register_request(trial_spec_json(task_description, trial_id=trial_id), trial_id=trial_id),
        context,
    )
    assert registered.success is True, registered.error

    async def echo(args):
        return json.dumps(args)

    service.trials[trial_id].agent_tools["echo"] = echo
    for _ in range(2):
        executed = service.ExecuteTool(
            execute_request(trial_id, "echo", json.dumps({"x": 1}), call_id=DUPLICATE_CALL_ID),
            context,
        )
        assert executed.status == pb2.EXECUTION_STATUS_SUCCESS

    history = service.trials[trial_id].tool_call_history
    assert [record.call_id for record in history] == [DUPLICATE_CALL_ID, DUPLICATE_CALL_ID]
    return trial_id


def collided_trajectory(*, task_id: str, trial_index: int = 0) -> Trajectory:
    """The transcript half of the collision staged by
    :func:`register_collided_trial` — one assistant turn whose tool call carries
    :data:`DUPLICATE_CALL_ID`, against two recorded results."""
    return make_trajectory(
        task_id=task_id,
        trial_index=trial_index,
        status=TrialStatus.COMPLETED,
        termination_reason=TerminationReason.AGENT_DONE,
        messages=[
            Message(role=MessageRole.USER, content="echo x"),
            Message(
                role=MessageRole.ASSISTANT,
                content="Echoing.",
                tool_calls=[ToolCall(id=DUPLICATE_CALL_ID, name="echo", arguments={"x": 1})],
            ),
        ],
    )
