"""Real Docker/gRPC smoke test for the BYOH shared-workspace contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tolokaforge.core.docker_runtime import RunnerClient
from tolokaforge.docker.stacks import core_stack

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def _task_description() -> dict:
    return {
        "task_id": "workspace_e2e",
        "name": "Workspace E2E",
        "category": "test",
        "description": "Verify shared bytes and the server ledger",
        "adapter_type": "native",
        "system_prompt": "Use the file tools.",
        "initial_state": {
            "tables": {},
            "schemas": [],
            "unstable_fields": [],
            "filesystem": {"/work/seed.txt": "seeded by runner"},
        },
        "agent_tools": [
            {
                "name": "write_file",
                "description": "Write a file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                "category": "write",
            }
        ],
        "user_tools": [],
    }


def test_runner_and_host_share_isolated_trial_workspace(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    stack = core_stack(extra_runner_binds=[(workspace_root, "/workspaces")])
    client: RunnerClient | None = None
    try:
        stack.start_all(wait=True)
        runner_address = stack.get_service_url("runner", 50051).replace("http://", "")
        client = RunnerClient(runner_address=runner_address)
        client.connect()
        trial_id = "workspace_e2e:0"
        registered = client.register_trial(
            trial_id=trial_id,
            task_description_json=json.dumps(_task_description()),
            workspace_path="/workspaces/workspace_e2e/0/attempt",
        )
        assert registered["success"], registered["error"]

        host_workspace = workspace_root / "workspace_e2e" / "0" / "attempt"
        assert (host_workspace / "seed.txt").read_text() == "seeded by runner"

        executed = client.execute_tool(
            trial_id=trial_id,
            tool_name="write_file",
            arguments={"path": "/work/result.txt", "content": "written through gRPC"},
        )
        assert executed.success, executed.error
        assert (host_workspace / "result.txt").read_text() == "written through gRPC"

        history = client.get_trial_history(trial_id)
        assert history["success"], history["error"]
        assert history["tool_history"][0]["tool_name"] == "write_file"
        assert len(history["tool_history"][0]["result_hash"]) == 64
    finally:
        if client is not None:
            client.close()
        stack.destroy()
