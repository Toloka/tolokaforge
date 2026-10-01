"""The builtin ``db_query`` / ``db_update`` read and write only their own trial's seeded store.

Two ``tool_use`` example trials are seeded on one real db-service (served over
loopback HTTP) and each gets its agent tools from the production
``ToolFactory``. Each trial's ``db_query("$")`` must answer its own seed, and a
``db_update`` in one trial must land in that trial's graded state and nowhere
else.
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
from tolokaforge.runner.tool_factory import ReconstructedTools, ToolFactory

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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "db_query/db_update post to the flat /query and /update routes, which resolve the "
        "unseeded shared __default__ store: db_query('$') answers [{}], not the trial's seed"
    ),
)
async def test_json_db_builtins_read_and_write_only_their_own_trials_store(
    db_service_loopback_url, monkeypatch
):
    monkeypatch.setenv("DB_SERVICE_URL", db_service_loopback_url)
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
