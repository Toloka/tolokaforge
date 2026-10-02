"""A trial enabling ``db_query`` / ``db_update`` with no seeded table is refused at registration.

The JSON-DB builtins read and write the trial's own store, which ``RegisterTrial``
seeds from ``initial_state.tables``. With no table the agent would be handed tools
over an empty store, so registration refuses by name instead. The predicate is the
tables, not whether the task provisions a database at all: a task provisioning one
from unstable fields alone still seeds no table. An intentionally empty store is
declared as an empty table and registers.

``RunnerServiceImpl`` is real and drives its real ``RegisterTrial`` against the
in-process db-service.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json

pytestmark = pytest.mark.unit

_TASK_ID = "json_db_seed_task"
_UNSTABLE_ONLY = {"unstable_fields": [{"table_name": "tickets", "field_name": "updated_at"}]}


def _tool(name: str) -> dict[str, Any]:
    return {"name": name, "description": name, "parameters": {"type": "object"}}


def _task(initial_state: dict[str, Any], agent: list[str], user: list[str]) -> dict[str, Any]:
    return {
        "task_id": _TASK_ID,
        "name": "JSON DB seed task",
        "category": "test",
        "description": "Enables the JSON-DB builtins",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": initial_state,
        "agent_tools": [_tool(name) for name in agent],
        "user_tools": [_tool(name) for name in user],
    }


def _register(runner_service, context, trial_id: str, task: dict[str, Any]):
    return runner_service.RegisterTrial(
        register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id), context
    )


@pytest.mark.parametrize(
    ("initial_state", "agent", "user", "named"),
    [
        pytest.param({}, ["db_query"], [], "['db_query']", id="agent-no-initial-state"),
        pytest.param({}, [], ["db_update"], "['db_update']", id="user-tool"),
        pytest.param(
            _UNSTABLE_ONLY,
            ["db_query", "db_update"],
            [],
            "['db_query', 'db_update']",
            id="provisions-a-db-but-seeds-no-table",
        ),
    ],
)
def test_json_db_builtins_with_no_seeded_table_are_refused_by_name(
    runner_service, mock_grpc_context, initial_state, agent, user, named
):
    trial_id = f"{_TASK_ID}:0"

    response = _register(
        runner_service, mock_grpc_context, trial_id, _task(initial_state, agent, user)
    )

    assert response.success is False
    assert f"task '{_TASK_ID}'" in response.error
    assert f"JSON-DB tools {named}" in response.error
    assert "initial_state.json_db" in response.error
    assert trial_id not in runner_service.trials


def test_an_intentionally_empty_table_registers(runner_service, mock_grpc_context):
    trial_id = f"{_TASK_ID}:1"

    response = _register(
        runner_service,
        mock_grpc_context,
        trial_id,
        _task({"tables": {"tickets": []}}, ["db_query", "db_update"], []),
    )

    assert response.success is True, response.error
    assert set(runner_service.trials[trial_id].agent_tools) == {"db_query", "db_update"}
