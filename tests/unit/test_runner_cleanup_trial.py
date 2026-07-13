"""Tests for the CleanupTrial RPC on the Runner service.

CleanupTrial is the explicit way to forget a trial registration so the
orchestrator's retry path can re-issue ``RegisterTrial`` for the same
``trial_id`` after a transient failure. See GitHub issue #132.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from tolokaforge.runner import runner_pb2 as pb2


@pytest.fixture
def task_description() -> dict[str, Any]:
    """Minimal TaskDescription that can be registered without external tools."""
    return {
        "task_id": "cleanup_trial_test",
        "name": "Cleanup Trial Test",
        "category": "test",
        "description": "Round-trip register/cleanup/re-register flow",
        "adapter_type": "tau",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": {"users": [{"id": "u1", "name": "Alice"}]},
            "schemas": [
                {"table_name": "users", "fields": {"id": "string", "name": "string"}},
            ],
            "unstable_fields": [],
        },
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "all",
            "pass_threshold": 1.0,
            "state_checks": {"hash_enabled": True, "golden_actions": []},
        },
    }


def _register(runner_service, mock_grpc_context, trial_id: str, td: dict[str, Any]):
    request = pb2.RegisterTrialRequest(
        trial_id=trial_id,
        task_description_json=json.dumps(td),
        default_tool_timeout_s=30.0,
    )
    return runner_service.RegisterTrial(request, mock_grpc_context)


class TestCleanupTrialRPC:
    """Behavioural contract for the CleanupTrial RPC."""

    def test_cleanup_removes_registered_trial(
        self, runner_service, mock_grpc_context, task_description
    ):
        trial_id = "cleanup_trial_test:0"
        register = _register(runner_service, mock_grpc_context, trial_id, task_description)
        assert register.success is True, register.error
        assert trial_id in runner_service.trials

        response = runner_service.CleanupTrial(
            pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context
        )

        assert response.success is True
        assert response.error == ""
        assert trial_id not in runner_service.trials

    def test_cleanup_is_idempotent_on_unknown_trial(self, runner_service, mock_grpc_context):
        """Cleanup of a trial that was never registered succeeds — repeated cleanup
        on the same retry path must not itself crash."""
        response = runner_service.CleanupTrial(
            pb2.CleanupTrialRequest(trial_id="never_registered:0"), mock_grpc_context
        )

        assert response.success is True
        assert response.error == ""

    def test_cleanup_stops_lifecycle_tools(
        self, runner_service, mock_grpc_context, task_description
    ):
        trial_id = "cleanup_lifecycle_test:0"
        register = _register(runner_service, mock_grpc_context, trial_id, task_description)
        assert register.success is True, register.error
        lifecycle_tool = MagicMock()
        lifecycle_tool.has_lifecycle = True
        runner_service.trials[trial_id].agent_tools["bash"] = lifecycle_tool

        response = runner_service.CleanupTrial(
            pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context
        )

        assert response.success is True, response.error
        lifecycle_tool.cleanup.assert_called_once_with()

    def test_cleanup_enables_reregistration_with_same_trial_id(
        self, runner_service, mock_grpc_context, task_description
    ):
        """The retry contract: after CleanupTrial, RegisterTrial with the same
        ``trial_id`` must not fail with ``Trial 'X' already exists`` (#132)."""
        trial_id = "cleanup_trial_test:0"

        first = _register(runner_service, mock_grpc_context, trial_id, task_description)
        assert first.success is True, first.error

        # Without cleanup, the second RegisterTrial must fail — this pins the
        # baseline that motivates CleanupTrial in the first place.
        duplicate = _register(runner_service, mock_grpc_context, trial_id, task_description)
        assert duplicate.success is False
        assert "already exists" in (duplicate.error or "").lower()

        cleanup = runner_service.CleanupTrial(
            pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context
        )
        assert cleanup.success is True, cleanup.error

        # After cleanup, re-registration succeeds.
        second = _register(runner_service, mock_grpc_context, trial_id, task_description)
        assert second.success is True, second.error
        assert trial_id in runner_service.trials

    def test_gateway_coordinates_are_returned_and_unregistered(
        self, runner_service, mock_grpc_context, task_description
    ):
        gateway = MagicMock()
        gateway.register.return_value = SimpleNamespace(
            url="http://runner:8765/mcp/namespace",
            bearer_token="trial-token",
        )
        runner_service.mcp_gateway = gateway
        trial_id = "cleanup_gateway_test:0"

        registered = _register(runner_service, mock_grpc_context, trial_id, task_description)
        cleaned = runner_service.CleanupTrial(
            pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context
        )

        assert registered.success is True, registered.error
        assert registered.mcp_gateway_url == "http://runner:8765/mcp/namespace"
        assert registered.mcp_bearer_token == "trial-token"
        assert cleaned.success is True
        gateway.unregister.assert_called_once_with(trial_id)
