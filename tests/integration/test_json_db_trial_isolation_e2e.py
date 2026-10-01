"""Concurrent trials of different tasks each read and write only their own JSON DB.

Three tasks from two packs (`tool_use_public_example_01`, `_02` and
`reconcile_ledger`) are registered on one Docker runner backed by one
db-service. Their builtin `db_query` / `db_update` calls go over real gRPC
`ExecuteTool`, so the trial binding under test is the runner's own, not a
fixture's.

Two tests lock per-trial JSON-DB isolation:

- **No LLM** (`test_concurrent_trials_*`): every trial's `db_query("$")`, issued
  concurrently, returns exactly its own seed; an agent-side `db_update` on one
  trial moves no other trial's store and lands in the state `GradeTrial` grades.
- **One cheap live run** (`test_live_run_*`, `requires_api`): `tolokaforge run`
  with `workers: 3` over both packs, asserting on recorded traces only — no
  successful `db_query` output a trial saw carries another task's tables or
  seed rows. Agent correctness (`binary_pass`) is out of scope.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
import yaml

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.trial import EnvEndpoints, TrialSpec

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_docker,
    pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker daemon not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_USE_DATASET = _REPO_ROOT / "examples" / "native" / "tool_use" / "dataset"
_CUSTOM_CHECKS_DATASET = _REPO_ROOT / "examples" / "native" / "custom_checks" / "dataset"

_TICKETS_TASK = "tool_use_public_example_01"
_ACCOUNTS_TASK = "tool_use_public_example_02"
_LEDGER_TASK = "reconcile_ledger"

_DATASET_BY_TASK = {
    _TICKETS_TASK: _TOOL_USE_DATASET,
    _ACCOUNTS_TASK: _TOOL_USE_DATASET,
    _LEDGER_TASK: _CUSTOM_CHECKS_DATASET,
}

# The tables only one task's seed declares, and a row id only that seed holds.
_OWN_TABLES = {
    _TICKETS_TASK: frozenset({"tickets"}),
    _ACCOUNTS_TASK: frozenset({"accounts"}),
    _LEDGER_TASK: frozenset({"customers", "transactions"}),
}
_OWN_SEED_ID = {_TICKETS_TASK: "T-100", _ACCOUNTS_TASK: "A-77", _LEDGER_TASK: "C-1"}

_ROOT_QUERY = {"jsonpath": "$"}
_RECONCILE_OPS = {"ops": [{"op": "replace", "path": "$.customers[0].balance", "value": 700}]}


def _task_description(task_id: str):
    adapter = NativeAdapter(
        {"tasks_glob": "tasks/**/task.yaml", "task_packs": [str(_DATASET_BY_TASK[task_id])]}
    )
    return adapter.to_task_description(task_id)


def _trial_spec_json(task_description, trial_id: str) -> str:
    return TrialSpec(
        trial_id=trial_id,
        run_id="json_db_trial_isolation_run",
        task=task_description,
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


@pytest.fixture
def runner_client(runner_container) -> Iterator[GrpcRunnerClient]:
    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = GrpcRunnerClient(runner_address=f"{host}:{port}")
    client.connect()
    yield client
    client.close()


@pytest.fixture
def registered_trials(runner_client: GrpcRunnerClient) -> Iterator[dict[str, dict[str, Any]]]:
    """Register one trial per task on the shared runner; yield task id → {trial_id, seed}."""
    trials: dict[str, dict[str, Any]] = {}
    try:
        for task_id in _DATASET_BY_TASK:
            task = _task_description(task_id)
            trial_id = f"{task_id}_isolation:0"
            registered = runner_client.register_trial(
                trial_id=trial_id, trial_spec_json=_trial_spec_json(task, trial_id)
            )
            assert registered["success"] is True, registered["error"]
            trials[task_id] = {"trial_id": trial_id, "seed": task.initial_state.tables}
        yield trials
    finally:
        for trial in trials.values():
            runner_client.cleanup_trial(trial_id=trial["trial_id"])


def _db_query_root(runner_client: GrpcRunnerClient, trial_id: str, call_id: str) -> Any:
    result = runner_client.execute_tool(
        trial_id=trial_id, tool_name="db_query", arguments=_ROOT_QUERY, call_id=call_id
    )
    assert result.success, f"{trial_id}: {result.error}"
    return json.loads(result.output)


def _declared_calls_transcript(calls: list[tuple[str, str, dict[str, Any]]]) -> str:
    """Wire `llm_messages` declaring each executed `(call_id, tool, arguments)`, in order."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Reconcile C-1."}]
    for call_id, tool_name, arguments in calls:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": ""})
    return json.dumps(messages)


def test_concurrent_trials_each_read_their_own_seed_and_write_only_their_own_store(
    runner_client: GrpcRunnerClient,
    registered_trials: dict[str, dict[str, Any]],
) -> None:
    all_in_flight = Barrier(len(registered_trials))

    def query_once_all_are_in_flight(task_id: str) -> tuple[str, Any]:
        all_in_flight.wait(timeout=30)
        return task_id, _db_query_root(
            runner_client, registered_trials[task_id]["trial_id"], "call_concurrent"
        )

    with ThreadPoolExecutor(max_workers=len(registered_trials)) as pool:
        seen = dict(pool.map(query_once_all_are_in_flight, registered_trials))

    for task_id, trial in registered_trials.items():
        assert seen[task_id] == [trial["seed"]], task_id

    ledger_trial_id = registered_trials[_LEDGER_TASK]["trial_id"]
    update = runner_client.execute_tool(
        trial_id=ledger_trial_id,
        tool_name="db_update",
        arguments=_RECONCILE_OPS,
        call_id="call_reconcile",
    )
    assert update.success, update.error

    for task_id in (_TICKETS_TASK, _ACCOUNTS_TASK):
        trial = registered_trials[task_id]
        after = _db_query_root(runner_client, trial["trial_id"], "call_after_update")
        assert after == [trial["seed"]], task_id

    transcript = _declared_calls_transcript(
        [
            ("call_concurrent", "db_query", _ROOT_QUERY),
            ("call_reconcile", "db_update", _RECONCILE_OPS),
        ]
    )
    grade = runner_client.grade_trial(trial_id=ledger_trial_id, llm_messages_json=transcript)
    assert grade["success"] is True, grade["error"]
    assert grade["grade"]["components"]["state_checks"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# One live run: tolokaforge run, workers 3, over both packs
# ---------------------------------------------------------------------------

_LIVE_MODEL = {"provider": "openrouter", "name": "anthropic/claude-haiku-4.5"}


def _live_run_config(output_dir: Path) -> dict[str, Any]:
    return {
        "models": {
            "agent": {**_LIVE_MODEL, "temperature": 0.0, "max_tokens": 4096},
            "user": dict(_LIVE_MODEL),
        },
        "orchestrator": {"workers": 3, "repeats": 1, "max_turns": 8, "queue_backend": "sqlite"},
        "evaluation": {
            "projects": [str(_TOOL_USE_DATASET), str(_CUSTOM_CHECKS_DATASET)],
            "tasks_glob": "**/task.yaml",
            "output_dir": str(output_dir),
        },
    }


def _successful_db_query_outputs(trial_dir: Path) -> list[str]:
    """The output of every `db_query` call the trial's tool log records as successful."""
    tool_log = yaml.safe_load((trial_dir / "tool_log.yaml").read_text()) or []
    return [
        entry["output"]
        for entry in tool_log
        if entry["tool_name"] == "db_query" and entry["status"] == "success"
    ]


def _foreign_markers(task_id: str) -> tuple[frozenset[str], frozenset[str]]:
    """Another task's seed-only table names, and its seed row ids as quoted JSON strings."""
    others = [other for other in _OWN_TABLES if other != task_id]
    tables = frozenset().union(*(_OWN_TABLES[other] for other in others))
    row_ids = frozenset(f'"{_OWN_SEED_ID[other]}"' for other in others)
    return tables, row_ids


def _top_level_keys(parsed: Any) -> set[str]:
    rows = parsed if isinstance(parsed, list) else [parsed]
    return {key for row in rows if isinstance(row, dict) for key in row}


@pytest.mark.requires_api
@pytest.mark.llm
@pytest.mark.slow
def test_live_run_with_three_workers_shows_each_trial_only_its_own_db(tmp_path: Path) -> None:
    if not os.getenv("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set — the live run's models route through OpenRouter")
    config_path = tmp_path / "run_config.yaml"
    config_path.write_text(yaml.safe_dump(_live_run_config(tmp_path / "out")))

    proc = subprocess.run(
        ["uv", "run", "tolokaforge", "run", "--config", str(config_path)],
        cwd=str(_REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, (
        f"tolokaforge run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
    )

    run_dirs = [p for p in tmp_path.iterdir() if p.is_dir() and p.name.startswith("out_")]
    assert len(run_dirs) == 1, sorted(p.name for p in tmp_path.iterdir())
    trial_dirs = sorted(p.parent for p in (run_dirs[0] / "trials").glob("*/*/trajectory.yaml"))
    assert len(trial_dirs) == 3, trial_dirs
    assert {d.parent.name for d in trial_dirs} == set(_DATASET_BY_TASK), trial_dirs

    tasks_seeing_own_seed = set()
    for trial_dir in trial_dirs:
        task_id = trial_dir.parent.name
        foreign_tables, foreign_row_ids = _foreign_markers(task_id)
        for output in _successful_db_query_outputs(trial_dir):
            leaked_tables = _top_level_keys(json.loads(output)) & foreign_tables
            leaked_rows = {row_id for row_id in foreign_row_ids if row_id in output}
            assert not leaked_tables and not leaked_rows, (
                f"{trial_dir}: db_query returned another task's data: "
                f"tables {sorted(leaked_tables)}, rows {sorted(leaked_rows)}\n{output}"
            )
            if f'"{_OWN_SEED_ID[task_id]}"' in output:
                tasks_seeing_own_seed.add(task_id)

    assert tasks_seeing_own_seed, "no trial's db_query output carried its own seed's row id"
