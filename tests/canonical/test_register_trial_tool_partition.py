"""Pin ``RegisterTrialResponse``'s tool-surface ordering contract.

One repeated field carries both actors' tools. The host reads it as two
slices — ``tool_schemas[:num_agent_tools]`` is offered to the agent and
``tool_schemas[num_agent_tools:]`` to the user actor — so the response's
*order* is load-bearing, not incidental. A response that interleaved the
actors, or one whose count did not partition the list, would hand each actor
tools the runner refuses to execute for it.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.runner_requests import (
    register_request,
    simple_task_description,
    trial_spec_json,
)

pytestmark = pytest.mark.canonical


def _tool(name: str) -> dict[str, Any]:
    """A source-less schema, which the runner reconstructs by dispatching
    *name* through the builtin registry — so every name here is a real
    builtin, or registration refuses before the response is built."""
    return {
        "name": name,
        "description": f"tool {name}",
        "parameters": {"type": "object", "properties": {}},
        "category": "read",
    }


def _task_with(agent: list[str], user: list[str]) -> dict[str, Any]:
    task = simple_task_description()
    task["agent_tools"] = [_tool(name) for name in agent]
    task["user_tools"] = [_tool(name) for name in user]
    return task


def _register(runner_service, context, trial_id: str, task: dict[str, Any]):
    response = runner_service.RegisterTrial(
        register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id),
        context,
    )
    assert response.success is True, response.error
    return response


def test_agent_tools_come_first_and_num_agent_tools_partitions_the_list(
    runner_service, mock_grpc_context
) -> None:
    response = _register(
        runner_service,
        mock_grpc_context,
        "partition_order:0",
        _task_with(agent=["calculator", "list_dir"], user=["read_file"]),
    )

    assert [schema.name for schema in response.tool_schemas] == [
        "calculator",
        "list_dir",
        "read_file",
    ]
    assert response.num_agent_tools == 2
    assert response.num_user_tools == 1
    assert response.num_agent_tools + response.num_user_tools == len(response.tool_schemas)

    partition = response.num_agent_tools
    assert [s.name for s in response.tool_schemas[:partition]] == ["calculator", "list_dir"]
    assert [s.name for s in response.tool_schemas[partition:]] == ["read_file"]


@pytest.mark.parametrize(
    ("agent", "user"),
    [
        pytest.param(["calculator", "list_dir"], [], id="agent_only"),
        pytest.param([], ["calculator"], id="user_only"),
        pytest.param(["calculator"], ["read_file", "list_dir"], id="both"),
        pytest.param([], [], id="neither"),
    ],
)
def test_the_partition_holds_for_every_declared_mix(
    runner_service, mock_grpc_context, agent: list[str], user: list[str]
) -> None:
    """The slice boundary is the count, not a guess from the names — a task
    declaring only user tools must report ``num_agent_tools == 0`` so the
    agent's slice comes out empty rather than swallowing the user's."""
    trial_id = f"partition_mix_{len(agent)}_{len(user)}:0"
    response = _register(runner_service, mock_grpc_context, trial_id, _task_with(agent, user))

    names = [schema.name for schema in response.tool_schemas]
    assert names == agent + user
    assert names[: response.num_agent_tools] == agent
    assert names[response.num_agent_tools :] == user
    assert response.num_user_tools == len(user)
