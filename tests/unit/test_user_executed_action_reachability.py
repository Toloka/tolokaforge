"""A ``requestor: user`` required action is satisfiable by a trial that ran.

Every other lock on this vocabulary grades an authored record. This one grades
one the harness produced: a real :class:`TrialRunner` drives a stubbed agent and
a stubbed simulator against a real ``RunnerServiceImpl`` registered from the
pack's own ``TaskDescription``, and the pack's ``requestor: user`` action is
scored against whatever came out. Every seam between the declaration and the
verdict is production code — the adapter resolving ``tools.user.enabled``, the
conductor's ``DockerRunnerAdapter`` bound to ``executor="user"``, the runner's
user registry, the trial's own recorder and the transcript evaluator.

The two stubs are the two LLMs, and nothing else: the tool is the pack's own
builtin, executed by the runner. What stands in for the gRPC channel is
:class:`tests.utils.servicer_runtime.ServicerBackend`, which drives the real client
mapping against the servicer in this process; that the same drive survives a real
channel and the shipped image is
``tests/integration/test_docker_grading_user_executed_action.py``.

Both trials are driven from one script differing only in the expression the
simulator asks for, so the verdicts discriminate on the arguments the action
compares rather than on two differently shaped runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tests.utils.servicer_runtime import ServicerBackend
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.loop import TerminationDecision, classify_loop_error
from tolokaforge.core.models import (
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
)
from tolokaforge.core.runner import TrialRunner
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit

_TASK_ID = "user_executed_action"
_PARITY_GLOB = "grading_parity/**/task.yaml"
_USER_TOOL = "calculator"

#: The expression the pack's required action compares against, and one it does not.
_REQUIRED_EXPRESSION = "2 + 2"
_OTHER_EXPRESSION = "2 + 3"


class _ScriptedAgent:
    """The agent's LLM, replaced by the turns it would have generated."""

    def __init__(self, texts: tuple[str, ...]) -> None:
        self._texts = list(texts)
        self.capabilities = ModelCapabilities()

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        return GenerationResult(text=self._texts.pop(0), usage=Usage(prompt_tokens=1))

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


def _simulator_calling(expression: str) -> MagicMock:
    """The user's LLM, replaced by one tool-calling turn and a stop."""
    simulator = MagicMock()
    simulator.last_system_prompt = None
    simulator.reply.side_effect = [
        GenerationResult(
            text="Let me add it up myself.",
            tool_calls=[
                ToolCall(id="call_1", name=_USER_TOOL, arguments={"expression": expression})
            ],
        ),
        GenerationResult(text="###STOP###"),
    ]
    return simulator


@pytest.fixture
def adapter(test_data_dir: Path) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(test_data_dir), "tasks_glob": _PARITY_GLOB})


@pytest.fixture
def pack_dir(test_data_dir: Path) -> Path:
    return test_data_dir / "grading_parity" / _TASK_ID


def _run_trial(
    expression: str,
    adapter: NativeAdapter,
    servicer: RunnerServiceImpl,
    context: Any,
    *,
    trial_id: str,
) -> Trajectory:
    """One trial of the pack, with the simulator asking for ``expression``."""
    registered = servicer.RegisterTrial(
        register_request(
            trial_spec_json(
                adapter.to_task_description(_TASK_ID).model_dump(mode="json"), trial_id=trial_id
            ),
            trial_id=trial_id,
        ),
        context,
    )
    assert registered.success is True, registered.error

    runtime = ServicerBackend(servicer, context)
    return TrialRunner(
        task_id=_TASK_ID,
        trial_index=0,
        agent_client=_ScriptedAgent(("Work it out and tell me the total.", "Then the total is 4.")),
        user_simulator=_simulator_calling(expression),
        tool_executor=DockerRunnerAdapter(runtime, trial_id, executor="agent"),
        tool_schemas=[],
        user_tool_executor=DockerRunnerAdapter(runtime, trial_id, executor="user"),
        max_turns=4,
        # The trial's own catch-all would otherwise turn a broken drive into an
        # empty trajectory, which reads here as the user having made no call.
        strict=True,
    ).run("You are a helpful assistant.", initial_user_message="What is 2 + 2?")


def _transcript_component(adapter: NativeAdapter, pack_dir: Path, trajectory: Trajectory) -> float:
    grade = GradingEngine(adapter.get_grading_config(_TASK_ID), task_dir=pack_dir).grade_trajectory(
        trajectory, {}
    )
    assert (
        grade.components.transcript_rules is not None
    ), f"the trial produced no transcript_rules component: {grade.reasons!r}"
    return grade.components.transcript_rules


def test_the_users_own_call_satisfies_the_action_the_pack_requires(
    adapter: NativeAdapter, pack_dir: Path, runner_service, mock_grpc_context
) -> None:
    """The trial the harness ran carries the call, and the action passes on it.

    The record is asserted beside the score because the score says only that the
    action was satisfied. Which actor satisfied it is on the record's
    ``executor`` — the field ``requestor`` is matched against — so a build that
    ran the user's call as the agent scores the same and fails here.
    """
    trajectory = _run_trial(
        _REQUIRED_EXPRESSION,
        adapter,
        runner_service,
        mock_grpc_context,
        trial_id=f"{_TASK_ID}_reachability:0",
    )

    assert [
        (call.tool_name, call.executor, call.arguments, call.output) for call in trajectory.tool_log
    ] == [(_USER_TOOL, ToolExecutorIdentity.USER, {"expression": _REQUIRED_EXPRESSION}, "4.0")]
    assert _transcript_component(adapter, pack_dir, trajectory) == pytest.approx(1.0)


def test_a_user_call_the_action_does_not_name_leaves_it_unsatisfied(
    adapter: NativeAdapter, pack_dir: Path, runner_service, mock_grpc_context
) -> None:
    """The same drive, one argument apart, fails the action.

    Without it the lock above would hold for a build that passed every required
    action whatever the trial did. The record is asserted to be a successful
    user-side call, so this half fails too where the call never ran — a trial that
    scores 0.0 because nothing happened measures nothing about the arguments.
    """
    trajectory = _run_trial(
        _OTHER_EXPRESSION,
        adapter,
        runner_service,
        mock_grpc_context,
        trial_id=f"{_TASK_ID}_reachability_other:0",
    )

    assert [(call.executor, call.status, call.arguments) for call in trajectory.tool_log] == [
        (
            ToolExecutorIdentity.USER,
            ToolExecutionStatus.SUCCESS,
            {"expression": _OTHER_EXPRESSION},
        )
    ]
    assert _transcript_component(adapter, pack_dir, trajectory) == pytest.approx(0.0)
