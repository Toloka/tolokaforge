"""Runner and BYOH containers share one isolated filesystem per attempt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import service as service_module

pytestmark = pytest.mark.unit


def _task(task_id: str, seed: str) -> dict[str, Any]:
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    return {
        "task_id": task_id,
        "name": task_id,
        "category": "test",
        "description": "workspace isolation",
        "adapter_type": "native",
        "system_prompt": "Use the file tools.",
        "initial_state": {
            "tables": {},
            "schemas": [],
            "unstable_fields": [],
            "filesystem": {"/work/seed.txt": seed},
        },
        "agent_tools": [
            {
                "name": "read_file",
                "description": "Read a file",
                "parameters": parameters,
                "category": "read",
            },
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
            },
        ],
        "user_tools": [],
    }


def _register(
    runner_service: Any,
    grpc_context: Any,
    *,
    trial_id: str,
    workspace: Path,
    seed: str,
) -> pb2.RegisterTrialResponse:
    return runner_service.RegisterTrial(
        pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(_task(trial_id, seed)),
            workspace_path=str(workspace),
        ),
        grpc_context,
    )


def _execute(
    runner_service: Any,
    grpc_context: Any,
    *,
    trial_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> pb2.ExecuteToolResponse:
    return runner_service.ExecuteTool(
        pb2.ExecuteToolRequest(
            trial_id=trial_id,
            tool_name=tool_name,
            arguments_json=json.dumps(arguments),
            executor="agent",
        ),
        grpc_context,
    )


def test_two_trials_provision_and_execute_in_distinct_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_service: Any, mock_grpc_context: Any
) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(service_module, "WORKSPACES_ROOT", root)
    first = root / "task" / "0" / "attempt-a"
    second = root / "task" / "1" / "attempt-b"

    first_registration = _register(
        runner_service,
        mock_grpc_context,
        trial_id="isolated-a:0",
        workspace=first,
        seed="first",
    )
    second_registration = _register(
        runner_service,
        mock_grpc_context,
        trial_id="isolated-b:0",
        workspace=second,
        seed="second",
    )

    assert first_registration.success, first_registration.error
    assert second_registration.success, second_registration.error
    assert (first / "seed.txt").read_text() == "first"
    assert (second / "seed.txt").read_text() == "second"

    written = _execute(
        runner_service,
        mock_grpc_context,
        trial_id="isolated-a:0",
        tool_name="write_file",
        arguments={"path": "/work/result.txt", "content": "only first"},
    )
    missing_from_second = _execute(
        runner_service,
        mock_grpc_context,
        trial_id="isolated-b:0",
        tool_name="read_file",
        arguments={"path": "/work/result.txt"},
    )

    assert written.status == pb2.EXECUTION_STATUS_SUCCESS
    assert (first / "result.txt").read_text() == "only first"
    assert not (second / "result.txt").exists()
    assert missing_from_second.status == pb2.EXECUTION_STATUS_ERROR
    assert "File not found" in missing_from_second.error_message


@pytest.mark.parametrize(
    "workspace_path",
    ["relative/path", "/tmp/outside-workspaces"],
)
def test_registration_rejects_workspace_outside_runner_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_service: Any,
    mock_grpc_context: Any,
    workspace_path: str,
) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(service_module, "WORKSPACES_ROOT", root)
    response = runner_service.RegisterTrial(
        pb2.RegisterTrialRequest(
            trial_id=f"invalid:{workspace_path}",
            task_description_json=json.dumps(_task("invalid", "seed")),
            workspace_path=workspace_path,
        ),
        mock_grpc_context,
    )

    assert not response.success
    assert "workspace_path" in response.error


def test_registration_rejects_initial_file_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_service: Any, mock_grpc_context: Any
) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(service_module, "WORKSPACES_ROOT", root)
    task = _task("escape", "seed")
    task["initial_state"]["filesystem"] = {"/work/../../other/secret": "nope"}

    response = runner_service.RegisterTrial(
        pb2.RegisterTrialRequest(
            trial_id="escape:0",
            task_description_json=json.dumps(task),
            workspace_path=str(root / "task" / "0" / "attempt"),
        ),
        mock_grpc_context,
    )

    assert not response.success
    assert "escapes the trial workspace" in response.error


def test_grading_translates_work_alias_to_trial_workspace(tmp_path: Path) -> None:
    from tolokaforge.runner.grading import evaluate_jsonpath_file_checks

    workspace = tmp_path / "attempt"
    workspace.mkdir()
    (workspace / "answer.txt").write_text("isolated answer")

    score, reasons = evaluate_jsonpath_file_checks(
        [
            {
                "path_glob": "/work/*.txt",
                "contains_ci": "isolated",
                "description": "trial file",
            }
        ],
        workspace_path=str(workspace),
    )

    assert score == 1.0
    assert "PASS: trial file" in reasons
