"""An MCP tool's refused arguments stay an argument error through the runner.

A real stdio MCP subprocess refuses arguments in the two typed ways the runner
reads: the JSON-RPC "Invalid params" error (-32602), and a tool result with
``isError`` whose ``_meta`` declares ``tolokaforge/error_category:
invalid_arguments``. Both must reach the recorded call as ``INVALID_ARGUMENTS``,
which leaves the environment-error budget alone, while an untyped ``isError`` (or
an unknown category) stays an environment error and any other JSON-RPC error a
raised failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.utils.runner_requests import (
    execute_request,
    register_request,
    simple_task_description,
    trial_spec_json,
)
from tolokaforge.core.models import ToolExecutionStatus
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    INVALID_ARGUMENTS_CATEGORY,
    MCPProtocolError,
    MCPServerPool,
    MCPServerToolWrapper,
)

pytestmark = pytest.mark.unit

SERVER = r"""
import json, sys

STATE = {"quantity": 0}


def answer(request_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": request_id}
    message.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def text(payload, failed=False, meta=None):
    result = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": failed}
    if meta is not None:
        result["_meta"] = meta
    return result


for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method, params = request["method"], request.get("params", {})
    if method == "initialize":
        answer(request["id"], {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "probe", "version": "1"}})
        continue
    name, quantity = params.get("name"), params.get("arguments", {}).get("quantity")
    if name == "strict_quantity" and quantity == 97:
        answer(request["id"], error={"code": -32603, "message": "internal failure"})
    elif name == "strict_quantity" and quantity <= 0:
        answer(request["id"], error={"code": -32602, "message": "quantity must be a positive integer"})
    elif name == "set_quantity" and quantity <= 0:
        answer(request["id"], text({"error": "quantity must be positive"}, True, {"tolokaforge/error_category": "invalid_arguments"}))
    elif name == "set_quantity" and quantity == 99:
        answer(request["id"], text({"error": "backend unavailable"}, True))
    elif name == "set_quantity" and quantity == 98:
        answer(request["id"], text({"error": "odd"}, True, {"tolokaforge/error_category": "made_up"}))
    else:
        STATE["quantity"] = quantity
        answer(request["id"], text(dict(STATE)))
"""


@pytest.fixture
def server_script(tmp_path: Path) -> str:
    script = tmp_path / "probe_mcp_server.py"
    script.write_text(SERVER)
    return str(script)


@pytest.fixture
def pool():
    created = MCPServerPool()
    yield created
    created.cleanup()


def _wrapper(name: str, script: str, pool: MCPServerPool, trial_id: str = "probe:0"):
    return MCPServerToolWrapper(
        tool_schema=ToolSchema(name=name, description=name, parameters={}),
        server_script=script,
        db_client=None,
        trial_id=trial_id,
        server_pool=pool,
    )


@pytest.mark.parametrize(
    ("tool", "quantity", "failed", "category"),
    [
        ("set_quantity", 0, True, INVALID_ARGUMENTS_CATEGORY),
        ("strict_quantity", 0, True, INVALID_ARGUMENTS_CATEGORY),
        ("set_quantity", 99, True, None),
        ("set_quantity", 98, True, None),
        ("set_quantity", 3, False, None),
    ],
)
async def test_the_wrapper_reads_only_typed_categories(
    tool, quantity, failed, category, server_script, pool
):
    outcome = await _wrapper(tool, server_script, pool).execute_call({"quantity": quantity})
    assert (outcome.declared_failure, outcome.category) == (failed, category)


async def test_invalid_params_carry_the_servers_feedback(server_script, pool):
    outcome = await _wrapper("strict_quantity", server_script, pool).execute_call({"quantity": 0})
    assert outcome.output == "quantity must be a positive integer"


async def test_any_other_jsonrpc_error_still_raises(server_script, pool):
    with pytest.raises(MCPProtocolError) as raised:
        await _wrapper("strict_quantity", server_script, pool).execute_call({"quantity": 97})
    assert raised.value.code == -32603 and isinstance(raised.value, RuntimeError)


def test_the_runner_records_argument_errors_apart_from_environment_errors(
    runner_service, mock_grpc_context, server_script, pool, request
):
    trial_id = f"{request.node.name}:0"
    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(simple_task_description(), trial_id=trial_id), trial_id=trial_id
        ),
        mock_grpc_context,
    )
    assert registered.success is True, registered.error
    tools = runner_service.trials[trial_id].agent_tools
    for name in ("set_quantity", "strict_quantity"):
        tools[name] = _wrapper(name, server_script, pool, trial_id)

    def call(name: str, quantity: int, call_id: str) -> pb2.ExecuteToolResponse:
        return runner_service.ExecuteTool(
            execute_request(
                trial_id,
                name,
                arguments_json=json.dumps({"quantity": quantity}),
                call_id=call_id,
                timeout_seconds=5,
            ),
            mock_grpc_context,
        )

    assert call("set_quantity", 0, "c1").status == pb2.EXECUTION_STATUS_INVALID_ARGUMENTS
    assert call("strict_quantity", 0, "c2").status == pb2.EXECUTION_STATUS_INVALID_ARGUMENTS
    assert call("set_quantity", 99, "c3").status == pb2.EXECUTION_STATUS_ENVIRONMENT_ERROR
    corrected = call("set_quantity", 2, "c4")
    assert corrected.status == pb2.EXECUTION_STATUS_SUCCESS
    assert json.loads(corrected.output) == {"quantity": 2}
    assert [r.status for r in runner_service.trials[trial_id].tool_call_history] == [
        ToolExecutionStatus.INVALID_ARGUMENTS,
        ToolExecutionStatus.INVALID_ARGUMENTS,
        ToolExecutionStatus.ENVIRONMENT_ERROR,
        ToolExecutionStatus.SUCCESS,
    ]
