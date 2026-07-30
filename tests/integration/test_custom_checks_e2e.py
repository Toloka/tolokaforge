"""End-to-end lock for runner-side custom checks over real gRPC.

Drives the `examples/native/custom_checks` reference pack through the
Docker runner: `NativeAdapter` builds the `TaskDescription` (bundling
`checks.py` into `tool_artifacts`), the runner registers the trial (which
extracts the artifacts and validates `interface_version`), and
`GradeTrial` runs the executor against synthetic transcript evidence + the
initial DB state.

Two cases lock the seam:

- **Positive** — transcript enumerates every credit transaction id and
  contains a `db_query` call; both checks pass, `custom_checks == 1.0`,
  and the declared `custom_checks: 0.6` weight combined with
  `state_checks: 0.4` yields `final_score == 1.0` (`binary_pass`).

- **Negative** — same registered trial pack with an empty transcript
  (no tool calls, no content). The arithmetic balance check still passes
  because the DB balance equals the reconciled total, but the
  transcript-enumeration check fails: `custom_checks == 0.5`. With
  `state_checks: 0.4` * 1.0 + `custom_checks: 0.6` * 0.5 == 0.7, the
  weighted score drops below `pass_threshold: 0.8` and `binary_pass` is
  False. This pins that the declared `custom_checks` weight is applied
  to the final score, not silently dropped.

Deterministic and network-free: the test drives `register_trial` +
`grade_trial` directly (like `test_docker_grading_jsonpath.py`) rather
than running an agent loop, so no LLM provider key is required. The
runner's `db_client.get_state` output feeds the shared
`build_check_context` helper — this is the sole real-shape check on
that path, and any regression in it fails
`balance_matches_transaction_net`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

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
        {"role": "tool", "name": "db_query", "content": "[…transaction rows…]"},
        {"role": "assistant", "content": _ASSISTANT_ENUMERATION_TEXT},
    ]


def _synthetic_llm_messages_negative() -> list[dict[str, Any]]:
    """Empty transcript — no db_query call, no credit-txn ids anywhere."""
    return [
        {"role": "user", "content": "Reconcile customer C-1's balance."},
        {"role": "assistant", "content": "Done."},
    ]


def _register_and_grade(
    runner_client: GrpcRunnerClient,
    trial_id: str,
    trial_spec_json: str,
    llm_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    registered = runner_client.register_trial(trial_id=trial_id, trial_spec_json=trial_spec_json)
    assert registered["success"] is True, registered["error"]
    try:
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
        task_description,
    ) -> None:
        """Both checks pass — custom_checks = 1.0; weighted final = 1.0; pass."""
        trial_id = f"{_TASK_ID}_pos:0"
        grade = _register_and_grade(
            runner_client,
            trial_id,
            _trial_spec_json(task_description, trial_id),
            _synthetic_llm_messages_positive(),
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

    def test_negative_case_failing_check_drags_weighted_score_below_threshold(
        self,
        runner_client: GrpcRunnerClient,
        task_description,
    ) -> None:
        """Transcript-enumeration fails; weighted score drops below pass_threshold."""
        trial_id = f"{_TASK_ID}_neg:0"
        grade = _register_and_grade(
            runner_client,
            trial_id,
            _trial_spec_json(task_description, trial_id),
            _synthetic_llm_messages_negative(),
        )

        # state_checks JSONPath still passes (initial DB balance == 700).
        assert grade["components"]["state_checks"] == pytest.approx(1.0)
        # custom_checks aggregate: 1 pass + 1 fail = 0.5.
        assert grade["components"]["custom_checks"] == pytest.approx(0.5)
        # Weighted final: 0.4*1.0 + 0.6*0.5 = 0.7, below pass_threshold 0.8.
        assert grade["score"] == pytest.approx(0.7)
        assert grade["binary_pass"] is False

        by_name = {c["check_name"]: c for c in grade["custom_checks"]}
        assert by_name["balance_matches_transaction_net"]["status"] == "passed"
        assert by_name["transcript_enumerates_credit_transactions"]["status"] == "failed"
