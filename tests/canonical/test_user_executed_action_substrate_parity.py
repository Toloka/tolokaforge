"""A required action the *user* made, graded alike by both substrates.

``requestor: user`` has always been authorable and, until the user-side executor
was wired, nothing could satisfy it: no run produced a record carrying
``executor: user``, so every trial failed such an action however the actors
behaved. This is where that stops being true of the tree.

One pack — ``tests/data/grading_parity/user_executed_action``, whose only author
key is a ``requestor: user`` required action over a tool declared for the user
actor alone — is graded through each substrate's own path:

- core: ``NativeAdapter.get_grading_config`` → ``GradingEngine.grade_trajectory``
  over the authored trial;
- runner: ``NativeAdapter.to_task_description`` → ``RegisterTrial`` →
  ``ExecuteTool(executor="user")`` → ``GradeTrial``.

The runner arm executes the authored call rather than being handed a record of
it, so what ``GradeTrial`` reads is a record the runner wrote under the user
identity. Nothing stands in for the tool: the pack declares the ``calculator``
builtin, which the runner reconstructs from the registered ``TaskDescription``
with no source to fetch — the same property that lets
``tests/integration/test_docker_grading_user_executed_action.py`` reach this
inside the shipped image.

Both trials are driven, because a pack that failed everything would satisfy an
agreement check on its own: the satisfying trial calls the tool the action names
with the arguments it compares, and the violating one calls it with different
arguments, so the two differ in nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.utils.grading_parity_packs import TrialCase, load_case
from tests.utils.runner_requests import execute_request, register_request, trial_spec_json
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import ToolExecutorIdentity
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import TaskDescription
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.canonical

_TASK_ID = "user_executed_action"
_PARITY_GLOB = "grading_parity/**/task.yaml"
_ACTION_ID = "the_user_did_the_arithmetic"
_USER_TOOL = "calculator"

_CASES = ("satisfying", "violating")

#: One recorded call as this module compares it: id, tool, executor, output.
_RecordedCall = tuple[str, str, ToolExecutorIdentity, str]


@dataclass(frozen=True)
class _Verdict:
    """One case's ``transcript_rules`` component from each substrate, and the
    record the runner wrote while producing its own."""

    core: float
    runner: float
    recorded: tuple[_RecordedCall, ...]


@pytest.fixture
def pack_dir(test_data_dir: Path) -> Path:
    return test_data_dir / "grading_parity" / _TASK_ID


@pytest.fixture
def adapter(test_data_dir: Path) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(test_data_dir), "tasks_glob": _PARITY_GLOB})


def _core_component(adapter: NativeAdapter, pack_dir: Path, case: TrialCase) -> float:
    grade = GradingEngine(adapter.get_grading_config(_TASK_ID), task_dir=pack_dir).grade_trajectory(
        case.core_trajectory, case.state
    )
    assert (
        grade.components.transcript_rules is not None
    ), f"the core engine scored no transcript_rules component: {grade.reasons!r}"
    return grade.components.transcript_rules


def _runner_component(
    task_description: TaskDescription,
    case: TrialCase,
    servicer: RunnerServiceImpl,
    context: Any,
    *,
    trial_id: str,
) -> tuple[float, tuple[_RecordedCall, ...]]:
    """``GradeTrial``'s verdict, over records this servicer executed itself."""
    registered = servicer.RegisterTrial(
        register_request(
            trial_spec_json(task_description.model_dump(mode="json"), trial_id=trial_id),
            trial_id=trial_id,
        ),
        context,
    )
    assert registered.success is True, registered.error
    assert registered.num_user_tools == 1, (
        "the registered trial carries no user tool, so the call below would be refused "
        "and the record the action needs could not exist"
    )

    for record in case.core_trajectory.tool_log:
        executed = servicer.ExecuteTool(
            execute_request(
                trial_id,
                record.tool_name,
                json.dumps(record.arguments),
                executor=record.executor.value,
                call_id=record.call_id,
            ),
            context,
        )
        assert executed.status == pb2.EXECUTION_STATUS_SUCCESS, executed.error_message

    response = servicer.GradeTrial(
        pb2.GradeTrialRequest(
            trial_id=trial_id, llm_messages_json=json.dumps(case.runner_messages)
        ),
        context,
    )
    assert response.success is True, response.error
    return response.grade.components.transcript_rules, tuple(
        (call.call_id, call.tool_name, call.executor, call.output)
        for call in servicer.trials[trial_id].recorded
    )


@pytest.fixture
def verdicts(
    adapter: NativeAdapter, pack_dir: Path, runner_service, mock_grpc_context
) -> dict[str, _Verdict]:
    task_description = adapter.to_task_description(_TASK_ID)
    graded: dict[str, _Verdict] = {}
    for name in _CASES:
        case = load_case(pack_dir, name)
        runner, recorded = _runner_component(
            task_description,
            case,
            runner_service,
            mock_grpc_context,
            trial_id=f"{_TASK_ID}_{name}:0",
        )
        graded[name] = _Verdict(
            core=_core_component(adapter, pack_dir, case), runner=runner, recorded=recorded
        )
    return graded


def test_both_substrates_score_the_user_executed_action_alike(
    verdicts: dict[str, _Verdict],
) -> None:
    """The action the user satisfied scores 1.0 and the one it missed 0.0, on both.

    The discrimination is what makes the agreement worth reading: two substrates
    that both scored everything 0.0 — which is what either of them does with a
    ``requestor: user`` action nothing can satisfy — agree just as well.
    """
    assert verdicts["satisfying"].core == verdicts["satisfying"].runner == pytest.approx(1.0)
    assert verdicts["violating"].core == verdicts["violating"].runner == pytest.approx(0.0)


def test_the_runner_scored_a_user_record_it_wrote_itself(verdicts: dict[str, _Verdict]) -> None:
    """The graded record came out of ``ExecuteTool``, under the user identity.

    Read as the whole recorded history rather than a membership test, so a build
    that also ran the call as the agent — the shape the registry split exists to
    refuse — fails here rather than passing on the user record beside it.
    """
    assert verdicts["satisfying"].recorded == (
        ("call_0", _USER_TOOL, ToolExecutorIdentity.USER, "4.0"),
    )
    assert verdicts["violating"].recorded == (
        ("call_0", _USER_TOOL, ToolExecutorIdentity.USER, "5.0"),
    )


def test_the_pack_declares_the_action_against_the_user_actor_alone(
    adapter: NativeAdapter, pack_dir: Path
) -> None:
    """The tool is the user's and nobody else's, and the action names the user.

    Without this the two verdicts above would hold for a pack whose agent also
    declared the tool, and then neither substrate need read ``requestor`` at all
    to tell the two trials apart — the arguments alone would do it.
    """
    task_description = adapter.to_task_description(_TASK_ID)
    assert [tool.name for tool in task_description.user_tools] == [_USER_TOOL]
    assert task_description.agent_tools == []

    (action,) = adapter.get_grading_config(_TASK_ID).transcript_rules.required_actions
    assert (action.action_id, action.name, action.requestor) == (_ACTION_ID, _USER_TOOL, "user")
