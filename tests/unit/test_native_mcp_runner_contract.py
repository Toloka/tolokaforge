"""Fail-closed consumer tests for stateful native MCP tasks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import GoldenAction, TaskDescription, ToolSchema
from tolokaforge.runner.service import RunnerServiceImpl, TrialContextRuntime
from tolokaforge.runner.tool_factory import (
    MCPServerProcess,
    MCPServerToolWrapper,
    ToolExecutionError,
    _mcp_subprocess_env,
)

pytestmark = pytest.mark.unit


def test_native_mcp_subprocess_environment_excludes_runner_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("CURRENT_CONVERSATION_TIME", "2026-07-23T12:00:00Z")

    environment = _mcp_subprocess_env()

    assert "OPENROUTER_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["CURRENT_CONVERSATION_TIME"] == "2026-07-23T12:00:00Z"
    assert environment["PYTHONUNBUFFERED"] == "1"


def _task_description() -> TaskDescription:
    return TaskDescription.model_validate(
        {
            "task_id": "native-state-contract",
            "name": "Native state contract",
            "category": "test",
            "description": "Exercise non-id primary keys.",
            "adapter_type": "native",
            "system_prompt": "Test.",
            "initial_state": {
                "tables": {
                    "cases": [
                        {"case_id": "CASE-1", "status": "open"},
                        {"case_id": "CASE-2", "status": "open"},
                    ]
                },
                "schemas": [
                    {
                        "table_name": "cases",
                        "fields": {"case_id": "string", "status": "string"},
                        "primary_key": "case_id",
                    }
                ],
            },
            "agent_tools": [],
            "user_tools": [],
        }
    )


def _wrapper(trial_id: str = "native:0") -> MCPServerToolWrapper:
    return MCPServerToolWrapper(
        ToolSchema.model_validate(
            {
                "name": "update_case",
                "description": "Update a case.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "source": {
                    "toolset": "native",
                    "module_path": "mcp_server",
                    "class_name": "update_case",
                    "invocation_style": "mcp_server",
                    "mcp_server_script": "/tmp/native-server.py",
                },
            }
        ),
        "/tmp/native-server.py",
        MagicMock(),
        trial_id,
    )


@pytest.mark.asyncio
async def test_mcp_state_sync_uses_registered_non_id_primary_key() -> None:
    service = object.__new__(RunnerServiceImpl)
    service.db_client = MagicMock()
    service.db_client.get_state = AsyncMock(
        return_value=SimpleNamespace(
            data={
                "cases": [
                    {"case_id": "CASE-1", "status": "open"},
                    {"case_id": "CASE-2", "status": "open"},
                ]
            },
        )
    )
    service.db_client.mutate = AsyncMock()
    service.trials = {"native:0": TrialContextRuntime("native:0", _task_description())}

    await service._sync_mcp_state_to_db(
        "native:0",
        {
            "cases": [
                {"case_id": "CASE-1", "status": "closed"},
                {"case_id": "CASE-3", "status": "open"},
            ]
        },
    )

    service.db_client.mutate.assert_awaited_once_with(
        "native:0",
        "cases",
        [
            {
                "op": "insert",
                "record": {"case_id": "CASE-3", "status": "open"},
            },
            {
                "op": "upsert",
                "record": {"case_id": "CASE-1", "status": "closed"},
                "key": "case_id",
            },
            {"op": "delete", "filter": {"case_id": "CASE-2"}},
        ],
    )


@pytest.mark.asyncio
async def test_mcp_state_sync_rejects_missing_and_duplicate_primary_keys() -> None:
    service = object.__new__(RunnerServiceImpl)
    service.db_client = MagicMock()
    service.db_client.get_state = AsyncMock(return_value=SimpleNamespace(data={"cases": []}))
    service.db_client.mutate = AsyncMock()
    service.trials = {"native:0": TrialContextRuntime("native:0", _task_description())}

    with pytest.raises(RuntimeError, match="missing primary key"):
        await service._sync_mcp_state_to_db("native:0", {"cases": [{"status": "open"}]})
    with pytest.raises(RuntimeError, match="duplicate primary key"):
        await service._sync_mcp_state_to_db(
            "native:0",
            {
                "cases": [
                    {"case_id": "CASE-1", "status": "open"},
                    {"case_id": "CASE-1", "status": "closed"},
                ]
            },
        )
    service.db_client.mutate.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_wrapper_turns_is_error_into_failed_execution() -> None:
    wrapper = _wrapper()
    server = MagicMock(spec=MCPServerProcess)
    server.send_request.return_value = {
        "isError": True,
        "content": [{"type": "text", "text": "Input validation failed"}],
    }
    wrapper._get_server = MagicMock(return_value=server)

    with pytest.raises(ToolExecutionError, match="Input validation failed"):
        await wrapper.execute({})


@pytest.mark.asyncio
async def test_native_mcp_seed_reset_and_cleanup_are_per_trial() -> None:
    service = object.__new__(RunnerServiceImpl)
    first = _wrapper("native:0")
    second_tool_same_trial = _wrapper("native:0")
    other_trial = _wrapper("native:1")
    first.reset_state = MagicMock()
    second_tool_same_trial.reset_state = MagicMock()
    other_trial.reset_state = MagicMock()
    first.close_server = MagicMock()
    second_tool_same_trial.close_server = MagicMock()
    other_trial.close_server = MagicMock()

    context = TrialContextRuntime("native:0", _task_description())
    context.agent_tools = {
        "first": first,
        "second": second_tool_same_trial,
        "other": other_trial,
    }

    await service._reset_mcp_servers(context)
    first.reset_state.assert_called_once_with(
        {
            "cases": [
                {"case_id": "CASE-1", "status": "open"},
                {"case_id": "CASE-2", "status": "open"},
            ]
        }
    )
    second_tool_same_trial.reset_state.assert_not_called()
    other_trial.reset_state.assert_called_once()

    service._close_mcp_servers(context)
    first.close_server.assert_called_once()
    second_tool_same_trial.close_server.assert_not_called()
    other_trial.close_server.assert_called_once()


@pytest.mark.asyncio
async def test_execute_tool_records_native_mcp_error() -> None:
    service = object.__new__(RunnerServiceImpl)
    context = TrialContextRuntime("native:0", _task_description())
    tool = MagicMock()
    tool.execute.side_effect = ToolExecutionError("update_case", "Input validation failed")

    response = await service._execute_tool_async(
        trial_context=context,
        tool=tool,
        tool_name="update_case",
        arguments={},
        executor="agent",
        timeout_seconds=1,
    )

    assert response.status == pb2.EXECUTION_STATUS_ERROR
    assert response.metrics.exit_code == 1
    assert "Input validation failed" in response.error_message
    assert context.tool_call_history[0].status == "error"


def test_mcp_server_registry_isolated_and_released_per_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[MCPServerProcess] = []

    def start(server: MCPServerProcess) -> None:
        started.append(server)

    monkeypatch.setattr(MCPServerProcess, "start", start)
    monkeypatch.setattr(MCPServerProcess, "stop", MagicMock())
    MCPServerToolWrapper._servers.clear()
    first = _wrapper("native:0")
    first_alias = _wrapper("native:0")
    second = _wrapper("native:1")

    assert first._get_server() is first_alias._get_server()
    assert first._get_server() is not second._get_server()
    assert len(started) == 2

    first.close_server()
    assert (first.server_script, "native:0") not in MCPServerToolWrapper._servers
    assert (second.server_script, "native:1") in MCPServerToolWrapper._servers
    second.close_server()
    assert not MCPServerToolWrapper._servers


@pytest.mark.asyncio
async def test_failed_golden_replay_cannot_grade_unchanged_state_as_pass() -> None:
    """A broken reference call is a grading error, not a partial golden."""
    service = object.__new__(RunnerServiceImpl)
    wrapper = _wrapper()
    wrapper.get_state = MagicMock(
        return_value={
            "cases": [
                {"case_id": "CASE-1", "status": "open"},
                {"case_id": "CASE-2", "status": "open"},
            ]
        }
    )
    wrapper.reset_state = MagicMock()
    wrapper.execute = AsyncMock(side_effect=ToolExecutionError("update_case", "bad golden"))
    context = TrialContextRuntime("native:0", _task_description())
    context.agent_tools = {"update_case": wrapper}
    service.db_client = MagicMock()
    service.db_client.get_stable_hash = AsyncMock(return_value="same-hash")
    service.db_client.create_snapshot = AsyncMock()
    service.db_client.reset_trial = AsyncMock()

    with pytest.raises(RuntimeError, match="golden replay failed"):
        await service._execute_hash_grading(
            "native:0",
            context,
            [GoldenAction(tool_name="update_case", arguments={})],
        )

    # The implementation must fail before it computes a partial golden hash.
    service.db_client.get_stable_hash.assert_awaited_once()


@pytest.mark.asyncio
async def test_shared_mcp_sync_feeds_non_hash_state_consumers() -> None:
    """JSONPath/DB grading can invoke the same authoritative state bridge."""
    service = object.__new__(RunnerServiceImpl)
    wrapper = _wrapper()
    live_state = {"cases": [{"case_id": "CASE-1", "status": "closed"}]}
    wrapper.get_state = MagicMock(return_value=live_state)
    context = TrialContextRuntime("native:0", _task_description())
    context.agent_tools = {"update_case": wrapper}
    service._sync_mcp_state_to_db = AsyncMock()

    await service._sync_trial_mcp_state("native:0", context)

    service._sync_mcp_state_to_db.assert_awaited_once_with("native:0", live_state)
