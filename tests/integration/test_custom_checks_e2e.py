"""End-to-end lock for runner-side custom checks over real gRPC.

Drives the `examples/native/custom_checks` reference pack through the
Docker runner: `NativeAdapter` builds the `TaskDescription` (bundling
`checks.py` into `tool_artifacts`), the runner registers the trial (which
extracts the artifacts and validates `interface_version`), and
`GradeTrial` runs the executor against synthetic transcript evidence +
the DB state the executed tool calls leave behind.

The pack's `initial_state.json` seeds `customers[0].balance = 500` (the
unreconciled opening balance). Only a run that raises the balance to
`700` (= `500 + 260 - 60`) in the trial's own state passes the state and
arithmetic dimensions. This makes every graded dimension gate on real
work — no dimension trivially passes on the initial state.

Reconciliation is driven through the trial-scoped db-service mutate
endpoint (`PATCH /trials/{trial_id}/state/customers`), the same surface
`test_docker_grading_jsonpath.py` uses to move a trial's DB state
between registration and grading. It deliberately does *not* go through
the runner's builtin `db_update` tool: that tool posts to the flat,
non-trial-scoped legacy `/update` endpoint (the `__default__` store),
which `RegisterTrial` never seeds and `GradeTrial` never reads, so a
`db_update` mutation is invisible to trial-scoped grading. Driving the
mutate endpoint directly reconciles the store grading actually reads —
`customers[0].balance` under `$.db.*` for the state check and
`final_state.data.customers` for the arithmetic custom check.

Two cases lock the seam:

- **Positive** — the test reconciles `balance` to `700` on the trial's
  state, then grades with a transcript enumerating every credit
  transaction id and a `db_query` call. State-check `equals 700` passes,
  both custom checks pass: `custom_checks == 1.0`. Combined with
  `state_checks: 0.4` * 1.0 + `custom_checks: 0.6` * 1.0 == 1.0
  (`binary_pass`).

- **Negative** — no `db_update`, no transcript enumeration. The DB
  balance stays at `500`, so the JSONPath `equals 700` fails
  (`state_checks == 0.0`), `balance_matches_transaction_net` fails
  (500 != 500 + 260 - 60 = 700), and
  `transcript_enumerates_credit_transactions` fails
  (`custom_checks == 0.0`). The weighted final is 0.0 — well below
  `pass_threshold: 0.8` — and `binary_pass` is False. This pins that
  the declared `custom_checks` weight is applied to the final score,
  not silently dropped.

Deterministic and network-free: the test drives `register_trial` + a
trial-scoped state mutation + `grade_trial` directly (like
`test_docker_grading_jsonpath.py`) rather than running an agent loop,
so no LLM provider key is required. The runner's `db_client.get_state`
output feeds the shared `build_check_context` helper — this is the
sole real-shape check on that path, and any regression in it fails
`balance_matches_transaction_net`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.trial import EnvEndpoints, TrialSpec

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "examples" / "native" / "custom_checks"
_DATASET = _PACK_ROOT / "dataset"
_TASK_ID = "reconcile_ledger"


def _docker_running() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@pytest.fixture(scope="module")
def task_description():
    """Build the `TaskDescription` from the reference pack's `NativeAdapter`.

    Exercises the same adapter path the orchestrator drives, so this test
    catches a `custom_checks`-related regression in `NativeAdapter` (bundling
    `checks.py` into `tool_artifacts`, propagating `custom_checks` into
    `GradingConfig`) as well as in the runner.
    """
    adapter = NativeAdapter(
        {
            "tasks_glob": "tasks/**/task.yaml",
            "task_packs": [str(_DATASET)],
        }
    )
    return adapter.to_task_description(_TASK_ID)


def _trial_spec_json(task_description, trial_id: str) -> str:
    return TrialSpec(
        trial_id=trial_id,
        run_id="custom_checks_e2e_run",
        task=task_description,
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


@pytest.fixture
def runner_client(runner_container) -> GrpcRunnerClient:
    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = GrpcRunnerClient(runner_address=f"{host}:{port}")
    client.connect()
    yield client
    client.close()


@pytest.fixture
def db_service_url(json_db_container) -> str:
    """Host-reachable base URL of the containerized db-service HTTP API.

    The runner reaches the same service over the ``runner-net`` alias
    (``http://db-service:8000``); the test reaches it on the published
    port to drive trial-scoped state directly.
    """
    host = json_db_container.get_container_host_ip()
    port = json_db_container.get_exposed_port(8000)
    return f"http://{host}:{port}"


_ASSISTANT_ENUMERATION_TEXT = (
    "Transactions for C-1: credits T-1 (+120), T-3 (+60), T-5 (+80) sum to 260. "
    "Debits T-2 (-45), T-4 (-15) sum to 60. Net 200. Reconciled balance = 500 + 200 = 700."
)


def _synthetic_llm_messages_positive() -> list[dict[str, Any]]:
    """Wire-shaped `llm_messages` enumerating every credit transaction id."""
    return [
        {"role": "user", "content": "Reconcile customer C-1's balance."},
        {
            "role": "assistant",
            "content": "Fetching every transaction for C-1.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "db_query",
                        "arguments": '{"jsonpath": "$.transactions"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "db_query",
            "content": "[…transaction rows…]",
        },
        {"role": "assistant", "content": _ASSISTANT_ENUMERATION_TEXT},
    ]


def _synthetic_llm_messages_negative() -> list[dict[str, Any]]:
    """Empty transcript — no db_query call, no credit-txn ids anywhere."""
    return [
        {"role": "user", "content": "Reconcile customer C-1's balance."},
        {"role": "assistant", "content": "Done."},
    ]


def _reconcile_balance(db_service_url: str, trial_id: str) -> None:
    """Raise ``customers[0].balance`` from the seeded 500 to the reconciled 700.

    Applied through the trial-scoped mutate endpoint so the write lands in
    the store ``GradeTrial`` reads (``db_client.get_state(trial_id)``),
    which the flat legacy ``/update`` the ``db_update`` tool posts to does
    not — see the module docstring.
    """
    with httpx.Client(base_url=db_service_url, timeout=10.0) as db:
        resp = db.patch(
            f"/trials/{trial_id}/state/customers",
            json={
                "operations": [
                    {"op": "update", "filter": {"id": "C-1"}, "set": {"balance": 700}},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["affected_rows"] == 1, resp.text


def _register_and_grade(
    runner_client: GrpcRunnerClient,
    db_service_url: str,
    trial_id: str,
    trial_spec_json: str,
    llm_messages: list[dict[str, Any]],
    *,
    reconcile_balance: bool,
) -> dict[str, Any]:
    """Register the trial, optionally reconcile the DB, then grade.

    ``reconcile_balance=True`` raises ``customers[0].balance`` from the
    seeded 500 to the reconciled 700 — the outcome an agent's reconciliation
    would leave in the trial's state. Skipping it leaves the DB at the
    seeded value so the grade dimensions reflect a no-op.
    """
    registered = runner_client.register_trial(trial_id=trial_id, trial_spec_json=trial_spec_json)
    assert registered["success"] is True, registered["error"]
    try:
        if reconcile_balance:
            _reconcile_balance(db_service_url, trial_id)
        result = runner_client.grade_trial(
            trial_id=trial_id, llm_messages_json=json.dumps(llm_messages)
        )
    finally:
        runner_client.cleanup_trial(trial_id=trial_id)
    assert result["success"] is True, result["error"]
    assert result["grade"] is not None, result
    return result["grade"]


@pytest.mark.skipif(not _docker_running(), reason="Docker daemon not available")
class TestCustomChecksE2E:
    def test_positive_case_scores_green_with_custom_checks_weight_applied(
        self,
        runner_client: GrpcRunnerClient,
        db_service_url: str,
        task_description,
    ) -> None:
        """Reconcile the DB + enumerate credits → both dimensions pass."""
        trial_id = f"{_TASK_ID}_pos:0"
        grade = _register_and_grade(
            runner_client,
            db_service_url,
            trial_id,
            _trial_spec_json(task_description, trial_id),
            _synthetic_llm_messages_positive(),
            reconcile_balance=True,
        )

        assert grade["components"]["custom_checks"] == pytest.approx(1.0)
        assert grade["components"]["state_checks"] == pytest.approx(1.0)
        assert grade["score"] == pytest.approx(1.0)
        assert grade["binary_pass"] is True

        custom_checks = grade["custom_checks"]
        by_name = {c["check_name"]: c for c in custom_checks}
        assert set(by_name) == {
            "balance_matches_transaction_net",
            "transcript_enumerates_credit_transactions",
        }, custom_checks
        for name, entry in by_name.items():
            assert entry["status"] == "passed", f"{name}: {entry}"
            assert entry["score"] == pytest.approx(1.0), f"{name}: {entry}"

    def test_negative_case_failing_checks_drag_weighted_score_below_threshold(
        self,
        runner_client: GrpcRunnerClient,
        db_service_url: str,
        task_description,
    ) -> None:
        """No reconciliation, no transcript enumeration — every dimension fails."""
        trial_id = f"{_TASK_ID}_neg:0"
        grade = _register_and_grade(
            runner_client,
            db_service_url,
            trial_id,
            _trial_spec_json(task_description, trial_id),
            _synthetic_llm_messages_negative(),
            reconcile_balance=False,
        )

        # DB stayed at balance=500 → JSONPath `equals 700` fails.
        assert grade["components"]["state_checks"] == pytest.approx(0.0)
        # Both custom checks fail: balance != reconciled total AND transcript
        # lacks a db_query call / credit-txn enumeration.
        assert grade["components"]["custom_checks"] == pytest.approx(0.0)
        # Weighted final: 0.4*0.0 + 0.6*0.0 = 0.0, below pass_threshold 0.8.
        assert grade["score"] == pytest.approx(0.0)
        assert grade["binary_pass"] is False

        by_name = {c["check_name"]: c for c in grade["custom_checks"]}
        assert by_name["balance_matches_transaction_net"]["status"] == "failed"
        assert by_name["transcript_enumerates_credit_transactions"]["status"] == "failed"
