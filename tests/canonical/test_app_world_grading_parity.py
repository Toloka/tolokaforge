"""One world graded identically whichever holder serves it (ADR-0058).

The same help-desk world is held once by a stdio MCP server, as an MCP pack holds it,
and once by a world service over HTTP, as a compose pack declaring
``initial_state.app_world`` holds it. The same trajectory and the same golden path,
graded by the runner, must read back the same state and reach the same verdict:
grading is the engine's, and the holder only provides state.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.utils.fake_app_world import INITIAL_TABLES, MCP_WORLD_SERVER, FakeAppWorld
from tests.utils.loopback_asgi import serve_asgi_on_loopback
from tests.utils.runner_requests import execute_request, register_request, trial_spec_json
from tests.utils.secret_state import secret_manager_installed
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.tools.builtin.http_request import HTTPRequestTool

pytestmark = pytest.mark.canonical

GOLDEN_SUBJECT = "printer on fire"


@pytest.fixture(autouse=True)
def isolated_secrets() -> Iterator[None]:
    with secret_manager_installed({}):
        yield


@pytest.fixture
def world_url() -> Iterator[str]:
    with serve_asgi_on_loopback(FakeAppWorld().build()) as url:
        yield url


@pytest.fixture
def mcp_script(tmp_path: Path) -> str:
    path = tmp_path / "mcp_world_server.py"
    path.write_text(MCP_WORLD_SERVER)
    return str(path)


def _grading(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "combine_method": "weighted",
        "pass_threshold": 1.0,
        "weights": {"state_checks": 1.0},
        "state_checks": {
            "hash_enabled": True,
            "golden_actions": [{"tool_name": tool_name, "arguments": arguments}],
        },
    }


def _description(initial_state: dict[str, Any], tool: dict[str, Any], grading: dict) -> dict:
    return {
        "task_id": "help_desk",
        "name": "Help desk",
        "category": "test",
        "description": "File the customer's ticket",
        "adapter_type": "native",
        "system_prompt": "You are a help desk agent.",
        "initial_state": initial_state,
        "agent_tools": [tool],
        "user_tools": [],
        "grading": grading,
    }


def _mcp_pack(script: str) -> tuple[dict, str, Any]:
    tool = {
        "name": "create_ticket",
        "description": "File a ticket",
        "parameters": {"type": "object", "properties": {"subject": {"type": "string"}}},
        "source": {
            "toolset": "help_desk",
            "module_path": "mcp_world_server",
            "class_name": "create_ticket",
            "invocation_style": "mcp_server",
            "mcp_server_script": script,
        },
    }
    description = _description(
        {"tables": INITIAL_TABLES}, tool, _grading("create_ticket", {"subject": GOLDEN_SUBJECT})
    )
    return description, "create_ticket", lambda subject: {"subject": subject}


def _compose_pack(world_url: str) -> tuple[dict, str, Any]:
    host = world_url.removeprefix("http://")
    schema = HTTPRequestTool().get_schema()["function"]
    tool = {
        "name": "http_request",
        "description": schema["description"],
        "parameters": schema["parameters"],
        "tool_config": {"allowed_hosts": [host]},
    }
    tickets = f"{world_url}/api/tickets"

    def arguments(subject: str) -> dict[str, Any]:
        return {"method": "POST", "url": tickets, "json": {"subject": subject}}

    initial_state = {
        "tables": INITIAL_TABLES,
        "app_world": {"url": world_url, "hosts": [host], "actors": {"agent": None}},
    }
    description = _description(
        initial_state, tool, _grading("http_request", arguments(GOLDEN_SUBJECT))
    )
    return description, "http_request", arguments


def _graded_trial(runner_service, context, pack: tuple[dict, str, Any], subject: str):
    """Register a trial of ``pack``, take one agent action, and read back state and grade."""
    description, tool_name, arguments = pack
    trial_id = f"parity-{uuid.uuid4().hex[:8]}:0"
    spec = trial_spec_json(description, trial_id=trial_id)
    registered = runner_service.RegisterTrial(register_request(spec, trial_id=trial_id), context)
    assert registered.success, registered.error
    try:
        call = execute_request(trial_id, tool_name, json.dumps(arguments(subject)))
        executed = runner_service.ExecuteTool(call, context)
        assert executed.status == pb2.EXECUTION_STATUS_SUCCESS, executed.error_message
        state = runner_service.GetState(
            pb2.GetStateRequest(trial_id=trial_id, include_unstable=True), context
        )
        assert state.success, state.error
        graded = runner_service.GradeTrial(pb2.GradeTrialRequest(trial_id=trial_id), context)
        assert graded.success, graded.error
        return json.loads(state.state_json), state.stable_hash, graded.grade
    finally:
        runner_service._run_async(runner_service.cleanup_trial(trial_id))


@pytest.mark.parametrize("subject", [GOLDEN_SUBJECT, "a ticket nobody asked for"])
def test_one_golden_path_grades_identically_through_both_holders(
    runner_service, mock_grpc_context, world_url: str, mcp_script: str, subject: str
) -> None:
    mcp_state, mcp_hash, mcp_grade = _graded_trial(
        runner_service, mock_grpc_context, _mcp_pack(mcp_script), subject
    )
    world_state, world_hash, world_grade = _graded_trial(
        runner_service, mock_grpc_context, _compose_pack(world_url), subject
    )

    assert world_state == mcp_state
    assert world_hash == mcp_hash
    assert world_grade.binary_pass == mcp_grade.binary_pass == (subject == GOLDEN_SUBJECT)
    assert world_grade.score == mcp_grade.score
    assert world_grade.components.state_checks == mcp_grade.components.state_checks
