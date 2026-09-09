"""End-to-end integration tests for DB JSONPath state grading over real gRPC.

These tests exercise the path added in PR #80: the Docker runner's ``GradeTrial``
RPC now grades ``path:``-style JSONPath assertions against the trial's structured
DB state (``$.db.*``), not just ``path_glob:`` file checks. The flow under test is

    RegisterTrial (loads initial_state.tables into the DB service)
        -> GradeTrial RPC
            -> service fetches db_client.get_stable_state(trial_id)
            -> core.grading.jsonpath_evaluators.evaluate_jsonpath_checks
               routes path: -> state checks
            -> grade.reasons carries "State: PASS/FAIL ..." segments

Unlike the unit tests in ``tests/unit/test_runner_jsonpath_grading.py`` (which call
the evaluators directly), these drive the same transport the orchestrator uses in
production and assert on the grade the runner container actually returns. They are
deterministic: the DB state is the registered ``initial_state`` (no mutations, no
LLM), so the verdicts are pinned.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


_DEFAULT_TABLES: dict[str, list[dict[str, Any]]] = {
    "orders": [
        {
            "id": "O-1",
            "status": "confirmed",
            "quantity": 3,
            "note": "Priority RUSH order",
        }
    ]
}

_DEFAULT_SCHEMAS: list[dict[str, Any]] = [
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
]


def _task_description(
    jsonpath_checks: list[dict[str, Any]],
    *,
    tables: dict[str, list[dict[str, Any]]] | None = None,
    schemas: list[dict[str, Any]] | None = None,
    id_fields: dict[str, str | list[str]] | None = None,
) -> dict[str, Any]:
    """A trial whose DB holds known seeded rows, graded by ``jsonpath_checks``.

    Defaults to a single ``orders`` row. Only ``state_checks.jsonpath_checks``
    is configured (hash grading disabled and no transcript rules), so the
    combined ``state_checks`` component equals the JSONPath score — making the
    expected score easy to pin.
    """
    return {
        "task_id": "jsonpath_state_e2e",
        "name": "JSONPath State Grading E2E",
        "category": "test",
        "description": "Grade $.db.* assertions through the runner GradeTrial RPC",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": _DEFAULT_TABLES if tables is None else tables,
            "schemas": _DEFAULT_SCHEMAS if schemas is None else schemas,
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
                "id_fields": id_fields or {},
            },
        },
    }


def _trial_spec_json(
    trial_id: str, jsonpath_checks: list[dict[str, Any]], **task_overrides: Any
) -> str:
    """Build a valid ``TrialSpec`` JSON wrapping the JSONPath-graded task description.

    The runner-side ``RegisterTrial`` handler validates the full ``TrialSpec``
    (not just ``spec.task``), so the wire payload must be a complete spec.
    """
    return TrialSpec(
        trial_id=trial_id,
        run_id="jsonpath_state_e2e_run",
        task=TaskDescription.model_validate(_task_description(jsonpath_checks, **task_overrides)),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


@pytest.fixture
def db_service_url(json_db_container) -> str:
    """Host-reachable base URL of the containerized db-service HTTP API."""
    host = json_db_container.get_container_host_ip()
    port = json_db_container.get_exposed_port(8000)
    return f"http://{host}:{port}"


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
    *,
    mutate: Callable[[], None] | None = None,
    **task_overrides: Any,
) -> dict[str, Any]:
    """Register a trial with the given checks, grade it, clean up, return the grade.

    ``mutate`` runs between registration and grading, so a test can drive the
    trial's DB state through the same mutation surface a tool call would use
    and assert the grade reflects the post-mutation state.
    """
    spec_json = _trial_spec_json(trial_id, jsonpath_checks, **task_overrides)
    registered = runner_client.register_trial(trial_id=trial_id, trial_spec_json=spec_json)
    assert registered["success"] is True, registered["error"]
    try:
        if mutate is not None:
            mutate()
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


# Rows sharing each key component individually (A1 twice, MSFT twice) while every
# (account_id, symbol) pair stays unique — the shape a single-field or concatenated
# matcher gets wrong, and the shape the RegisterTrial id_fields gate requires
# (a composite key must uniquely identify the seeded records).
_COMPOSITE_POSITIONS = [
    {"account_id": "A1", "symbol": "AAPL", "qty": 10},
    {"account_id": "A1", "symbol": "MSFT", "qty": 5},
    {"account_id": "A2", "symbol": "MSFT", "qty": 7},
]


class TestCompositeIdFieldsOverTheRealWire:
    """Composite ``state_checks.id_fields`` across both containerized surfaces.

    The runner wire (``RegisterTrial`` accepts a list-valued ``id_fields``
    entry) and the db-service HTTP mutate API (list-valued upsert ``key``)
    are compat surfaces between separately-built images; the in-process unit
    suites cannot see a skew between them. An older runner image refuses the
    list value at ``RegisterTrial`` with a Pydantic ``string_type`` error, and
    an older db-service refuses the list ``key`` with a 422 — the documented
    version-lock behaviour (docs/GRADING.md § Runner-engine version lock).
    """

    def test_composite_key_crosses_the_wire_and_governs_the_upsert(
        self, runner_client: GrpcRunnerClient, db_service_url: str
    ) -> None:
        trial_id = "composite_id_fields_e2e:0"
        checks = [
            {
                "path": "$.db.positions[1].qty",
                "equals": 99,
                "description": "fully-matching row restated",
            },
            {
                "path": "$.db.positions[0].qty",
                "equals": 10,
                "description": "row sharing only account_id untouched",
            },
            {
                "path": "$.db.positions[2].qty",
                "equals": 7,
                "description": "row sharing only symbol untouched",
            },
        ]

        def _upsert_on_the_composite_key() -> None:
            """PATCH the containerized db-service with a list-valued ``key``.

            Row-level outcome asserted directly against the service: only the
            row matching **every** component changed; the rows sharing one
            component each came back byte-identical.
            """
            with httpx.Client(base_url=db_service_url, timeout=10.0) as db:
                resp = db.patch(
                    f"/trials/{trial_id}/state/positions",
                    json={
                        "operations": [
                            {
                                "op": "upsert",
                                "record": {"account_id": "A1", "symbol": "MSFT", "qty": 99},
                                "key": ["account_id", "symbol"],
                            }
                        ]
                    },
                )
                assert resp.status_code == 200, resp.text
                assert resp.json()["affected_rows"] == 1
                state = db.get(f"/trials/{trial_id}/state")
                assert state.status_code == 200, state.text
                assert state.json()["data"]["positions"] == [
                    {"account_id": "A1", "symbol": "AAPL", "qty": 10},
                    {"account_id": "A1", "symbol": "MSFT", "qty": 99},
                    {"account_id": "A2", "symbol": "MSFT", "qty": 7},
                ]

        grade = _register_and_grade(
            runner_client,
            trial_id,
            checks,
            tables={"positions": _COMPOSITE_POSITIONS},
            schemas=[],
            id_fields={"positions": ["account_id", "symbol"]},
            mutate=_upsert_on_the_composite_key,
        )

        # GradeTrial reads the post-upsert state: all three row assertions hold.
        reasons = grade["reasons"]
        assert "PASS: fully-matching row restated" in reasons, reasons
        assert "PASS: row sharing only account_id untouched" in reasons, reasons
        assert "PASS: row sharing only symbol untouched" in reasons, reasons
        assert grade["components"]["state_checks"] == pytest.approx(1.0)
        assert grade["binary_pass"] is True
