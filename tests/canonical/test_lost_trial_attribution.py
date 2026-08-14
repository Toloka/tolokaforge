"""A trial the runner no longer holds ends as our fault, not as the agent's.

``TRIAL_NOT_FOUND`` is the runner saying it has no registration for the trial the
engine is running: a restarted shared-stack runner, a shutdown sweep, or the
deliberate deregistration before a retry. None of them is the agent calling a bad
tool, and every one of them reaches the agent as an ordinary tool failure unless
the engine ends the trial where the fault happened.

Both routes into that fault are driven here with nothing mocked but the two LLMs:
the real ``RunnerServiceImpl`` answering ``ExecuteTool`` for a trial it was never
asked to register, the real ``GrpcRunnerClient`` translating the response, the
real ``TrialRunner`` and its recorder around it, and ``build_trial_timeline`` over
the result. The agent's own tool call runs inside the turn loop; the simulator's
opening call runs before the loop, under a different ``except``, so a fix applied
to one route leaves the other visibly open.
"""

from __future__ import annotations

from itertools import count
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.utils.mock_clients import MockAsyncClient
from tests.utils.runner_requests import execute_request
from tests.utils.servicer_runtime import ServicerBackend
from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.core.failure_attribution import TrialOutcomeClass, classify_trial_outcome
from tolokaforge.core.grading.trace_timeline import (
    TraceEventKind,
    TrialTimeline,
    build_trial_timeline,
)
from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import TerminationDecision, classify_loop_error
from tolokaforge.core.models import (
    RecordedToolCall,
    TerminationReason,
    ToolCall,
    ToolExecutionStatus,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.runner import TrialRunner
from tolokaforge.env.json_db_service.app import app as db_app
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.canonical

LOST_TOOL = "calculator"
"""The tool every driven call names. The runner refuses before it looks one up,
so the name only has to be the same on both views."""

_TASK_IDS = count()


def _servicer() -> tuple[RunnerServiceImpl, Any]:
    """A real runner servicer over the in-process DB service, and a gRPC context."""
    db_client = DBServiceClient("http://testserver")
    db_client.set_test_client(MockAsyncClient(TestClient(db_app), "http://testserver"))
    return RunnerServiceImpl(db_client), MagicMock()


class _CallingAgent:
    """The agent's LLM, replaced by a turn that calls a tool and nothing else."""

    def __init__(self) -> None:
        self._turns = count(1)

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        return GenerationResult(
            text="",
            tool_calls=[
                ToolCall(id=f"toolu_{next(self._turns)}", name=LOST_TOOL, arguments={"x": 1})
            ],
            usage=Usage(prompt_tokens=1),
        )

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())


def _calling_simulator() -> MagicMock:
    """The user's LLM, replaced by an opening turn that calls a tool."""
    simulator = MagicMock()
    simulator.last_system_prompt = None
    simulator.reply.side_effect = [
        GenerationResult(
            text="Let me look that up myself.",
            tool_calls=[ToolCall(id="call_u", name=LOST_TOOL, arguments={"x": 1})],
        ),
        GenerationResult(text="###STOP###"),
    ]
    return simulator


def drive_lost_trial(
    *, bootstrap: bool = False
) -> tuple[Trajectory, tuple[RecordedToolCall, ...], TrialTimeline]:
    """Run one whole trial against a runner that never registered it.

    ``bootstrap`` routes the fault through the simulator's opening turn, which
    runs before the turn loop; otherwise the agent hits it inside the loop.

    A plain function rather than a fixture: the reachability census in
    ``tests/canonical/test_termination_reason_reachability.py`` calls it from a
    module-scoped fixture, which cannot request the function-scoped servicer
    fixtures this builds for itself.
    """
    service, context = _servicer()
    runtime = ServicerBackend(service, context)
    task_id = f"lost_trial_{next(_TASK_IDS)}"
    trial_id = f"{task_id}:0"

    trajectory = TrialRunner(
        task_id=task_id,
        trial_index=0,
        agent_client=_CallingAgent(),
        user_simulator=_calling_simulator(),
        tool_executor=DockerRunnerAdapter(runtime, trial_id, executor="agent"),
        tool_schemas=[],
        user_tool_executor=DockerRunnerAdapter(runtime, trial_id, executor="user"),
        max_turns=3,
    ).run("You are an agent.", initial_user_message="" if bootstrap else "Do the thing.")

    records = tuple(trajectory.tool_log)
    timeline = build_trial_timeline(trajectory.messages, records, trajectory.termination_reason)
    return trajectory, records, timeline


@pytest.fixture
def loop_route() -> tuple[Trajectory, tuple[RecordedToolCall, ...], TrialTimeline]:
    return drive_lost_trial()


@pytest.fixture
def bootstrap_route() -> tuple[Trajectory, tuple[RecordedToolCall, ...], TrialTimeline]:
    return drive_lost_trial(bootstrap=True)


def test_the_runner_refuses_a_call_for_a_trial_it_never_registered() -> None:
    """The wire half of the premise: the fault this module drives is the one the
    servicer actually produces, not a response shape written here."""
    service, context = _servicer()

    response = service.ExecuteTool(
        execute_request("never_registered:0", LOST_TOOL, call_id="toolu_A"), context
    )

    assert response.status == pb2.EXECUTION_STATUS_TRIAL_NOT_FOUND


def test_the_runner_records_nothing_for_a_trial_it_never_registered() -> None:
    """The other half: the runner substrate has no trial context to record into,
    so whatever the host records for this call, it records alone."""
    service, context = _servicer()

    service.ExecuteTool(
        execute_request("never_registered:0", LOST_TOOL, call_id="toolu_A"), context
    )

    assert "never_registered:0" not in service.trials


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, AttributeError),
    reason="a lost trial burns its turn budget and reports every call as an agent tool error",
)
def test_a_trial_lost_inside_the_loop_ends_as_a_named_harness_fault(
    loop_route: tuple[Trajectory, tuple[RecordedToolCall, ...], TrialTimeline],
) -> None:
    """The whole target contract as one test.

    ``TRIAL_LOST`` is asserted first because it is the last piece to land: the
    client refusing the status ends the trial on its own, under the generic
    ``ERROR``, and this must stay red until the fault has its own name.
    """
    trajectory, records, timeline = loop_route

    assert trajectory.termination_reason is TerminationReason.TRIAL_LOST
    assert trajectory.status is TrialStatus.ERROR
    assert classify_trial_outcome(trajectory) is TrialOutcomeClass.HARNESS_ERROR
    assert [record.status for record in records if record.status is ToolExecutionStatus.ERROR] == []

    calls = [event.call_id for event in timeline.events if event.kind is TraceEventKind.TOOL_CALL]
    results = [
        event.call_id for event in timeline.events if event.kind is TraceEventKind.TOOL_RESULT
    ]
    assert calls == ["toolu_1"]
    assert results == []


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, AttributeError),
    reason="a trial lost before the loop ends under the bootstrap handler's generic ERROR",
)
def test_a_trial_lost_before_the_loop_ends_under_the_same_name(
    bootstrap_route: tuple[Trajectory, tuple[RecordedToolCall, ...], TrialTimeline],
) -> None:
    """The simulator's opening call runs outside the loop, under the trial
    initialisation handler, which names every failure ``ERROR``. The fault is the
    same one, so the trial must end under the same name."""
    trajectory, records, _ = bootstrap_route

    assert trajectory.termination_reason is TerminationReason.TRIAL_LOST
    assert trajectory.status is TrialStatus.ERROR
    assert classify_trial_outcome(trajectory) is TrialOutcomeClass.HARNESS_ERROR
    assert records == ()
