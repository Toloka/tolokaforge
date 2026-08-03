"""Builders for the runner service's gRPC request messages.

Shared by every in-process test that drives ``RunnerServiceImpl`` directly
(via the ``runner_service`` / ``mock_grpc_context`` fixtures) rather than over a
real channel.
"""

from __future__ import annotations

from typing import Any

from tolokaforge.core.models import ModelConfig
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import TaskDescription
from tolokaforge.runner.protocol import ENGINE_PROTOCOL_VERSION


def trial_spec_json(task_dict: dict[str, Any], trial_id: str = "test:0") -> str:
    """Build a valid ``TrialSpec`` JSON wrapping ``task_dict``.

    The runner-side ``RegisterTrial`` handler validates the full ``TrialSpec``
    (not just ``spec.task``), so tests must construct a complete spec rather
    than a minimal ``{"task": ...}`` wrapper.
    """
    return TrialSpec(
        trial_id=trial_id,
        run_id="test_run",
        task=TaskDescription.model_validate(task_dict),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


def register_request(
    spec_json: str,
    trial_id: str = "test:0",
    default_tool_timeout_s: float = 0.0,
    engine_protocol_version: int = ENGINE_PROTOCOL_VERSION,
) -> pb2.RegisterTrialRequest:
    """A ``RegisterTrialRequest`` this runner accepts.

    ``engine_protocol_version`` defaults to the version this engine declares so
    the runner's skew gate passes; skew tests pass an older value.
    """
    return pb2.RegisterTrialRequest(
        trial_id=trial_id,
        trial_spec_json=spec_json,
        default_tool_timeout_s=default_tool_timeout_s,
        engine_protocol_version=engine_protocol_version,
    )


def execute_request(
    trial_id: str,
    tool_name: str,
    arguments_json: str = "{}",
    executor: str = "agent",
    call_id: str = "call_0",
    timeout_seconds: float = 0.0,
) -> pb2.ExecuteToolRequest:
    """An ``ExecuteToolRequest`` carrying a non-empty ``call_id``, which the
    runner requires of every call."""
    return pb2.ExecuteToolRequest(
        trial_id=trial_id,
        tool_name=tool_name,
        arguments_json=arguments_json,
        executor=executor,
        call_id=call_id,
        timeout_seconds=timeout_seconds,
    )


def simple_task_description() -> dict[str, Any]:
    """A minimal TlkMcpCore/Tau-shaped task the runner can register.

    Exercises the full pipeline without external dependencies. Tools are absent
    because they need source config for reconstruction; tests that need tools
    inject callables onto the registered trial context directly.
    """
    return {
        "task_id": "test_task_001",
        "name": "Simple Integration Test",
        "category": "test",
        "description": "A simple integration test task",
        "adapter_type": "tau",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": {
                "users": [
                    {"id": "u1", "name": "Alice", "balance": 100},
                    {"id": "u2", "name": "Bob", "balance": 200},
                ],
                "orders": [],
            },
            "schemas": [
                {
                    "table_name": "users",
                    "fields": {"id": "string", "name": "string", "balance": "integer"},
                },
                {
                    "table_name": "orders",
                    "fields": {"id": "string", "user_id": "string", "amount": "integer"},
                },
            ],
            "unstable_fields": [
                {"table_name": "orders", "field_name": "id", "reason": "auto_id"},
            ],
        },
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "all",
            "pass_threshold": 1.0,
            # Declared rather than left to the model's default, which weights nothing: the
            # fold refuses to invent a share for a component the payload configures.
            "weights": {"state_checks": 1.0},
            "state_checks": {
                "hash_enabled": True,
                "golden_actions": [
                    {
                        "tool_name": "create_order",
                        "arguments": {"user_id": "u1", "amount": 50},
                    },
                ],
            },
        },
    }
