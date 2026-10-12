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
import time
from concurrent import futures
from pathlib import Path

import grpc
import pytest

from tests.utils.runner_requests import (
    execute_request,
    register_request,
    simple_task_description,
    trial_spec_json,
)
from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import LoopConfig, MetricsSink, ToolCallingLoop, classify_loop_error
from tolokaforge.core.models import ToolCall, ToolExecutionStatus
from tolokaforge.core.models.trajectory import MessageRole
from tolokaforge.core.runner import TrialToolCallRecorder
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.tool_message_format import TOOL_ERROR_MESSAGE_PREFIX
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.runner_pb2_grpc import add_RunnerServiceServicer_to_server
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


class _NullSink(MetricsSink):
    def record_generation(self, result: GenerationResult) -> None:
        pass

    def record_tool_call(self) -> None:
        pass


class _OneToolCall:
    """A model that asks for one tool call."""

    def __init__(self, call: ToolCall) -> None:
        self._result = GenerationResult(text="", tool_calls=[call], usage=Usage(prompt_tokens=1))

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        return self._result


def test_the_refusal_reaches_the_model_and_the_record_over_the_docker_path(
    runner_service, mock_grpc_context, server_script, pool, request
):
    """Runner service over gRPC → host client → agent loop: the server's own
    feedback is what the model reads and what the trial records."""
    trial_id = f"{request.node.name}:0"
    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(simple_task_description(), trial_id=trial_id), trial_id=trial_id
        ),
        mock_grpc_context,
    )
    assert registered.success is True, registered.error
    runner_service.trials[trial_id].agent_tools["strict_quantity"] = _wrapper(
        "strict_quantity", server_script, pool, trial_id
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_RunnerServiceServicer_to_server(runner_service, server)
    port = server.add_insecure_port("localhost:0")
    server.start()
    client = GrpcRunnerClient(f"localhost:{port}")
    messages: list = []
    try:
        client.connect(timeout=10.0, retry_interval=0.1)
        ToolCallingLoop(
            llm_client=_OneToolCall(
                ToolCall(id="toolu_refused", name="strict_quantity", arguments={"quantity": 0})
            ),
            tool_executor=DockerRunnerAdapter(client, trial_id),
            tool_schemas=[],
            config=LoopConfig(max_turns=1, episode_timeout_s=10_000),
            metrics=_NullSink(),
            should_terminate=lambda result, turn, history: None,
            classify_error=lambda exc: classify_loop_error(exc, ()),
            logger=get_logger("argument-refusal", strict=False),
            recorder=TrialToolCallRecorder(),
        ).run("sys", messages, time.time())
    finally:
        client.close()
        server.stop(grace=None)

    feedback = "quantity must be a positive integer"
    (tool_message,) = [m for m in messages if m.role is MessageRole.TOOL]
    assert tool_message.content == f"{TOOL_ERROR_MESSAGE_PREFIX}{feedback}"
    record = runner_service.trials[trial_id].tool_call_history[-1]
    assert record.status is ToolExecutionStatus.INVALID_ARGUMENTS
    assert record.output == feedback
