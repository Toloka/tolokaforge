"""End-to-end integration tests for DB JSONPath state grading over real gRPC.

These tests exercise the path added in PR #80: the Docker runner's ``GradeTrial``
RPC now grades ``path:``-style JSONPath assertions against the trial's structured
DB state (``$.db.*``), not just ``path_glob:`` file checks. The flow under test is

    RegisterTrial (loads initial_state.tables into the DB service)
        -> GradeTrial RPC
            -> service fetches db_client.get_stable_state(trial_id)
            -> grading.evaluate_jsonpath_checks routes path: -> state checks
            -> grade.reasons carries "State: PASS/FAIL ..." segments

Unlike the unit tests in ``tests/unit/test_runner_jsonpath_grading.py`` (which call
the evaluators directly), these drive the same transport the orchestrator uses in
production and assert on the grade the runner container actually returns. They are
deterministic: the DB state is the registered ``initial_state`` (no mutations, no
LLM), so the verdicts are pinned.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tolokaforge.core.docker_runtime import GrpcRunnerClient

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def _task_description(jsonpath_checks: list[dict[str, Any]]) -> dict[str, Any]:
    """A trial whose DB holds one known ``orders`` row, graded by ``jsonpath_checks``.

    Only ``state_checks.jsonpath_checks`` is configured (hash grading disabled and
    no transcript rules), so the combined ``state_checks`` component equals the
    JSONPath score — making the expected score easy to pin.
    """
    return {
        "task_id": "jsonpath_state_e2e",
        "name": "JSONPath State Grading E2E",
        "category": "test",
        "description": "Grade $.db.* assertions through the runner GradeTrial RPC",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": {
                "orders": [
                    {
                        "id": "O-1",
                        "status": "confirmed",
                        "quantity": 3,
                        "note": "Priority RUSH order",
                    }
                ]
            },
            "schemas": [
                {
                    "table_name": "orders",
                    "fields": {
                        "id": "string",
                        "status": "string",
                        "quantity": "integer",
                        "note": "string",
                    },
                    "primary_key": "id",
                }
            ],
            "unstable_fields": [],
        },
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 0.99,
            "state_checks": {
                "hash_enabled": False,
                "golden_actions": [],
                "jsonpath_checks": jsonpath_checks,
            },
        },
    }


@pytest.fixture
def runner_client(runner_container) -> GrpcRunnerClient:
    """RunnerClient connected to the testcontainer Runner over gRPC."""
    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = GrpcRunnerClient(runner_address=f"{host}:{port}")
    client.connect()
    yield client
    client.close()


def _register_and_grade(
    runner_client: GrpcRunnerClient,
    trial_id: str,
    jsonpath_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Register a trial with the given checks, grade it, clean up, return the grade."""
    td_json = json.dumps(_task_description(jsonpath_checks))
    registered = runner_client.register_trial(trial_id=trial_id, task_description_json=td_json)
    assert registered["success"] is True, registered["error"]
    try:
        result = runner_client.grade_trial(trial_id=trial_id)
    finally:
        runner_client.cleanup_trial(trial_id=trial_id)
    assert result["success"] is True, result["error"]
    assert result["grade"] is not None, "grade missing from successful GradeTrial response"
    return result["grade"]


class TestDbJsonpathGradingOverGrpc:
    """Validate PR #80's DB JSONPath state grading against the real wire protocol."""

    def test_db_state_checks_graded_against_live_db(self, runner_client: GrpcRunnerClient) -> None:
        """``path:`` assertions are evaluated against the registered DB state, with
        PASS/FAIL verdicts and actionable mismatch reasons surfaced under ``State:``."""
        checks = [
            {
                "path": "$.db.orders[0].status",
                "equals": "confirmed",
                "description": "status is confirmed",
            },
            {
                "path": "$.db.orders[0].status",
                "equals": "cancelled",
                "description": "status is cancelled",
            },
            {"path": "$.db.orders[0].quantity", "equals": 3, "description": "quantity is three"},
            {
                "path": "$.db.orders[0].note",
                "contains_ci": "rush",
                "description": "note mentions rush",
            },
            {"path": "$.db.orders[0].missing", "equals": "x", "description": "absent field"},
        ]

        grade = _register_and_grade(runner_client, "jsonpath_state_e2e:0", checks)
        reasons = grade["reasons"]

        # The new state route fired (not the file route).
        assert "State:" in reasons, reasons

        # Passing assertions of both operator kinds, including a non-string equals
        # that proves the int round-tripped through the DB service intact.
        assert "PASS: status is confirmed" in reasons, reasons
        assert "PASS: quantity is three" in reasons, reasons
        assert "PASS: note mentions rush" in reasons, reasons

        # Failing assertion reports the actual value pulled from the DB.
        assert "FAIL: status is cancelled" in reasons, reasons
        assert "got 'confirmed'" in reasons, reasons

        # Missing path is a loud failure, not a silent skip.
        assert "Path not found" in reasons, reasons

        # 3 of 5 assertions pass; state_checks is the only weighted component.
        assert grade["components"]["state_checks"] == pytest.approx(0.6)
        assert grade["binary_pass"] is False

    def test_unknown_operator_fails_loud_over_grpc(self, runner_client: GrpcRunnerClient) -> None:
        """Parity with the core native grader (PR #66): a ``path:`` assertion with no
        recognized operator must FAIL loudly through the RPC, not silently pass."""
        checks = [
            {
                "path": "$.db.orders[0].status",
                "op": "gte",
                "expected": 5,
                "description": "bogus operator",
            },
        ]

        grade = _register_and_grade(runner_client, "jsonpath_state_e2e:1", checks)
        reasons = grade["reasons"]

        assert "no recognized operator" in reasons, reasons
        assert "bogus operator" in reasons, reasons
        # The supported operators are named so the task author can fix the assertion.
        assert "equals" in reasons, reasons
        assert grade["components"]["state_checks"] == pytest.approx(0.0)
        assert grade["binary_pass"] is False
