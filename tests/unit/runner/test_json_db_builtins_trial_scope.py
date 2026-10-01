"""The builtin ``db_query`` / ``db_update`` read and write only their own trial's seeded store.

Two ``tool_use`` example trials are seeded on one real db-service (served over
loopback HTTP) and each gets its agent tools from the production
``ToolFactory``. Each trial's ``db_query("$")`` must answer its own seed, and a
``db_update`` in one trial must land in that trial's graded state and nowhere
else. A refused ``db_update`` reaches the agent as a tool error it can correct
from, carrying the service's own reason and never the service's address.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from tests.utils.example_packs import EXAMPLES_ROOT
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.tool_factory import ReconstructedTools, ToolExecutionError, ToolFactory

pytestmark = pytest.mark.unit

_TOOL_USE_DATASET = EXAMPLES_ROOT / "native" / "tool_use" / "dataset"
_TICKETS_TASK = "tool_use_public_example_01"
_ACCOUNTS_TASK = "tool_use_public_example_02"


@dataclass(frozen=True)
class _SeededTrial:
    trial_id: str
    seed: dict[str, list[dict[str, Any]]]
    tools: ReconstructedTools


async def _seed_trial(
    client: DBServiceClient, adapter: NativeAdapter, task_id: str
) -> _SeededTrial:
    task = adapter.to_task_description(task_id)
    trial_id = f"{task_id}:{uuid4().hex[:8]}"
    await client.init_trial(trial_id, task.initial_state.tables)
    tools = ToolFactory(client, trial_id).reconstruct_tools(
        [tool.model_dump(mode="json") for tool in task.agent_tools]
    )
    return _SeededTrial(trial_id=trial_id, seed=task.initial_state.tables, tools=tools)


async def _query_root(trial: _SeededTrial) -> Any:
    return json.loads(await trial.tools.agent_tools["db_query"]({"jsonpath": "$"}))


async def _require_seeded(client: DBServiceClient, trial: _SeededTrial) -> None:
    # pytest.fail, not assert: a broken fixture must not pass as the expected failure.
    state = (await client.get_state(trial.trial_id)).data
    if state != trial.seed:
        pytest.fail(f"fixture did not seed {trial.trial_id}: {state!r} != {trial.seed!r}")


async def test_json_db_builtins_read_and_write_only_their_own_trials_store(
    db_service_loopback_url,
):
    client = DBServiceClient(base_url=db_service_loopback_url)
    adapter = NativeAdapter({"tasks_glob": "**/task.yaml", "task_packs": [str(_TOOL_USE_DATASET)]})
    tickets = await _seed_trial(client, adapter, _TICKETS_TASK)
    accounts = await _seed_trial(client, adapter, _ACCOUNTS_TASK)
    try:
        for trial in (tickets, accounts):
            await _require_seeded(client, trial)

        assert await _query_root(tickets) == [tickets.seed]
        assert await _query_root(accounts) == [accounts.seed]

        await accounts.tools.agent_tools["db_update"](
            {"ops": [{"op": "replace", "path": "$.accounts[0].state", "value": "suspended"}]}
        )

        assert await _query_root(tickets) == [tickets.seed]
        accounts_state = (await client.get_state(accounts.trial_id)).data
        assert accounts_state["accounts"][0]["state"] == "suspended"
    finally:
        for trial in (tickets, accounts):
            await client.delete_trial(trial.trial_id)


@pytest.mark.parametrize(
    ("ops", "names"),
    [
        pytest.param(
            [{"op": "replace", "path": "/accounts/0/state", "value": "suspended"}],
            "JSONPath",
            id="json-pointer-path",
        ),
        pytest.param([{"path": "$.accounts"}], "ops.0.op", id="op-missing"),
    ],
)
async def test_a_refused_db_update_is_an_agent_correctable_error_without_the_service_url(
    db_service_loopback_url, ops, names
):
    client = DBServiceClient(base_url=db_service_loopback_url)
    adapter = NativeAdapter({"tasks_glob": "**/task.yaml", "task_packs": [str(_TOOL_USE_DATASET)]})
    accounts = await _seed_trial(client, adapter, _ACCOUNTS_TASK)
    try:
        with pytest.raises(ToolExecutionError) as refused:
            await accounts.tools.agent_tools["db_update"]({"ops": ops})

        assert names in str(refused.value)
        assert "http://" not in str(refused.value)
        assert (await client.get_state(accounts.trial_id)).data == accounts.seed
    finally:
        await client.delete_trial(accounts.trial_id)
