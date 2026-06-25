"""End-to-end integration tests for the CleanupTrial RPC over real gRPC.

These tests bring up the runner+db-service Docker stack and exercise the
register → cleanup → re-register flow through the wire, validating the fix for
GitHub issue #132 against the same transport the orchestrator's retry path
uses in production.
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from tolokaforge.core.docker_runtime import RunnerClient
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.trial import TrialSpec
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def _task_description() -> dict[str, Any]:
    """Minimal TaskDescription that registers without external tools."""
    return {
        "task_id": "cleanup_e2e",
        "name": "Cleanup E2E",
        "category": "test",
        "description": "Round-trip register/cleanup/re-register over gRPC",
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


def _trial_spec_json(trial_id: str) -> str:
    """Build a valid ``TrialSpec`` JSON wrapping the minimal task description.

    The runner-side ``RegisterTrial`` handler validates the full ``TrialSpec``
    (not just ``spec.task``), so the wire payload must be a complete spec.
    """
    return TrialSpec(
        trial_id=trial_id,
        run_id="cleanup_e2e_run",
        task=TaskDescription.model_validate(_task_description()),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
    ).model_dump_json()


@pytest.fixture
def runner_client(runner_container) -> RunnerClient:
    """RunnerClient connected to the testcontainer Runner over gRPC."""
    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = RunnerClient(runner_address=f"{host}:{port}")
    client.connect()
    yield client
    client.close()


class TestCleanupTrialOverGrpc:
    """Validate the fix for #132 against the real wire protocol."""

    def test_cleanup_then_reregister_succeeds(self, runner_client: RunnerClient) -> None:
        """The retry contract: after CleanupTrial over gRPC, RegisterTrial with the
        same ``trial_id`` must not fail with ``Trial 'X' already exists``."""
        trial_id = "cleanup_e2e:0"
        spec_json = _trial_spec_json(trial_id)

        first = runner_client.register_trial(trial_id=trial_id, trial_spec_json=spec_json)
        assert first["success"] is True, first["error"]

        # Baseline: without cleanup, re-register collides.
        duplicate = runner_client.register_trial(trial_id=trial_id, trial_spec_json=spec_json)
        assert duplicate["success"] is False
        assert "already exists" in (duplicate["error"] or "").lower()

        # The new RPC removes the registration over the wire.
        cleanup = runner_client.cleanup_trial(trial_id=trial_id)
        assert cleanup["success"] is True, cleanup["error"]

        # And now re-registration succeeds — burning a retry attempt is no
        # longer a guaranteed failure mode.
        second = runner_client.register_trial(trial_id=trial_id, trial_spec_json=spec_json)
        assert second["success"] is True, second["error"]

        # Tidy: leave the server clean for the next test.
        runner_client.cleanup_trial(trial_id=trial_id)

    def test_cleanup_is_idempotent_on_unknown_trial(self, runner_client: RunnerClient) -> None:
        """Cleanup of a trial that was never registered succeeds over the wire."""
        response = runner_client.cleanup_trial(trial_id="never_registered_on_wire:0")
        assert response["success"] is True
        assert response["error"] is None

    def test_cleanup_rpc_is_routed(self, runner_container) -> None:
        """Direct stub call: confirm CleanupTrial is wired through the servicer.

        Bypasses RunnerClient to validate the proto-level dispatch table — if
        the regenerated stubs forgot to route CleanupTrial, this fails with
        UNIMPLEMENTED before the servicer is even consulted.
        """
        from tolokaforge.runner import runner_pb2_grpc

        host = runner_container.get_container_host_ip()
        port = runner_container.get_exposed_port(50051)
        with grpc.insecure_channel(f"{host}:{port}") as channel:
            stub = runner_pb2_grpc.RunnerServiceStub(channel)
            response = stub.CleanupTrial(pb2.CleanupTrialRequest(trial_id="routing_check:0"))
            assert response.success is True
