"""Tests for the CleanupTrial RPC on the Runner service.

CleanupTrial is the explicit way to forget a trial registration so the
orchestrator's retry path can re-issue ``RegisterTrial`` for the same
``trial_id`` after a transient failure. See GitHub issue #132.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.unit

from tolokaforge.core.models import ModelConfig
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.db_client import DBServiceError
from tolokaforge.runner.models import TaskDescription
from tolokaforge.runner.protocol import ENGINE_PROTOCOL_VERSION
from tolokaforge.runner.tool_factory import ToolFactory


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
    spec = TrialSpec(
        trial_id=trial_id,
        run_id="test_run",
        task=TaskDescription.model_validate(td),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    )
    request = pb2.RegisterTrialRequest(
        trial_id=trial_id,
        trial_spec_json=spec.model_dump_json(),
        default_tool_timeout_s=30.0,
        engine_protocol_version=ENGINE_PROTOCOL_VERSION,
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

    def test_cleanup_clears_per_trial_kb_search(
        self, runner_service, mock_grpc_context, task_description
    ):
        """CleanupTrial tears down the per-trial judge KnowledgeSearch (issue #95).

        The KB resolver is bound at trial setup and must be dropped at teardown.
        We hold a reference to the trial context across cleanup (the dict entry is
        deleted) and assert the KB was explicitly cleared, not merely orphaned.
        """
        trial_id = "cleanup_trial_test:0"
        register = _register(runner_service, mock_grpc_context, trial_id, task_description)
        assert register.success is True, register.error

        ctx = runner_service.trials[trial_id]
        # Simulate a trial that resolved a judge KB at setup.
        sentinel = object()
        ctx.register_kb_search(sentinel)  # type: ignore[arg-type]
        assert ctx.resolve_kb_search() is sentinel

        response = runner_service.CleanupTrial(
            pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context
        )

        assert response.success is True, response.error
        assert trial_id not in runner_service.trials
        # The context we still hold had its KB explicitly cleared.
        assert ctx.resolve_kb_search() is None

    def test_cleanup_is_idempotent_on_unknown_trial(self, runner_service, mock_grpc_context):
        """Cleanup of a trial that was never registered succeeds — repeated cleanup
        on the same retry path must not itself crash."""
        response = runner_service.CleanupTrial(
            pb2.CleanupTrialRequest(trial_id="never_registered:0"), mock_grpc_context
        )

        assert response.success is True
        assert response.error == ""

    def test_cleanup_reports_db_deletion_failure(
        self, runner_service, mock_grpc_context, task_description, monkeypatch
    ):
        trial_id = "cleanup_db_failure:0"
        assert _register(runner_service, mock_grpc_context, trial_id, task_description).success

        async def refuse_deletion(identifier):
            raise DBServiceError("injected deletion failure")

        monkeypatch.setattr(runner_service.db_client, "delete_trial", refuse_deletion)
        response = runner_service.CleanupTrial(
            pb2.CleanupTrialRequest(trial_id=trial_id), mock_grpc_context
        )
        assert response.success is False
        assert "injected deletion failure" in response.error
        assert trial_id in runner_service.trials

    def test_concurrent_cleanup_is_idempotent_and_allows_reregistration(
        self, runner_service, mock_grpc_context, task_description
    ):
        trial_id = "cleanup_concurrent:0"
        assert _register(runner_service, mock_grpc_context, trial_id, task_description).success

        async def overlapping_cleanup():
            await asyncio.gather(*(runner_service.cleanup_trial(trial_id) for _ in range(8)))

        runner_service._run_async(overlapping_cleanup())
        assert trial_id not in runner_service.trials
        assert _register(runner_service, mock_grpc_context, trial_id, task_description).success

    def test_failed_lifecycle_start_retires_every_resource(
        self, runner_service, mock_grpc_context, task_description, monkeypatch
    ):
        events = []

        class Resource:
            has_lifecycle = True

            def __init__(self, name):
                self.name = name

            def start(self, context):
                events.append((self.name, "start"))
                if self.name == "bad":
                    raise RuntimeError("injected startup failure")

            def stop(self):
                events.append((self.name, "stop"))

            def cleanup(self):
                events.append((self.name, "cleanup"))

        monkeypatch.setattr(
            ToolFactory, "_create_wrapper", lambda factory, schema: Resource(schema.name)
        )
        task_description["agent_tools"] = [
            {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}
            for name in ("good", "bad")
        ]
        trial_id = "cleanup_failed_start:0"
        response = _register(runner_service, mock_grpc_context, trial_id, task_description)
        assert response.success is False
        assert "injected startup failure" in response.error
        assert trial_id not in runner_service.trials
        assert events == [
            ("good", "start"),
            ("bad", "start"),
            ("good", "stop"),
            ("good", "cleanup"),
            ("bad", "stop"),
            ("bad", "cleanup"),
        ]

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
