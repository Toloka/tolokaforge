"""Every ``ToolExecutionStatus`` member is produced by a real recording path.

A ``status`` matcher naming a value no run can produce is a check that silently
never fires. So this lock does not compare the enum against a hand-written table
of names — it *drives* a real recording path for each member and asserts the set
of statuses it observed equals the enum. Adding a member without a producing
path fails here.

Four members come from the core in-process executor's four distinct branches;
``TIMEOUT`` comes from the runner substrate, where a tool that outlives its
budget is cancelled and the status crosses the wire. It is unproducible on the
pure in-process path because that executor implements no timeout at all
(``ToolPolicy.timeout_s`` is declared and never read — #691): a missing feature,
not a recording gap, so there is no behaviour to record.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from tests.utils.runner_requests import (
    execute_request,
    register_request,
    simple_task_description,
    trial_spec_json,
)
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import LoopConfig, MetricsSink, ToolCallingLoop
from tolokaforge.core.models import ToolCall, ToolExecutionStatus
from tolokaforge.core.runner import TrialToolCallRecorder
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry, ToolResult

pytestmark = pytest.mark.canonical


class _Echo(Tool):
    """Schema requires a string ``payload``, so a non-string argument reaches the
    executor's real jsonschema validation."""

    def __init__(self) -> None:
        super().__init__("echo", "Echo the payload back")

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {"payload": {"type": "string"}},
                    "required": ["payload"],
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        if kwargs.get("payload") == "raise":
            raise RuntimeError("kaboom")
        return ToolResult(success=True, output=str(kwargs.get("payload")))


class _NullSink(MetricsSink):
    def record_generation(self, result: GenerationResult) -> None:
        pass

    def record_tool_call(self) -> None:
        pass


class _ScriptedClient:
    def __init__(self, result: GenerationResult) -> None:
        self._result = result

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        return self._result


def _core_recorded_status(tool_name: str, arguments: dict[str, Any]) -> ToolExecutionStatus:
    """Drive one call through the core substrate's real recording path."""
    registry = ToolRegistry()
    registry.register(_Echo())
    recorder = TrialToolCallRecorder()
    ToolCallingLoop(
        llm_client=_ScriptedClient(
            GenerationResult(
                text="",
                tool_calls=[ToolCall(id="toolu_A", name=tool_name, arguments=arguments)],
                usage=Usage(prompt_tokens=1),
            )
        ),
        tool_executor=ToolExecutor(registry),
        tool_schemas=[],
        config=LoopConfig(max_turns=1, episode_timeout_s=10_000),
        metrics=_NullSink(),
        should_terminate=lambda result, turn, messages: None,
        logger=get_logger("status-reachability", strict=False),
        recorder=recorder,
    ).run("sys", [], time.time())
    return recorder.recorded[0].status


@pytest.fixture
def _timeout_trial(runner_service, mock_grpc_context, request) -> str:
    """A registered trial holding a tool that outlives any sane budget."""
    trial_id = f"{request.node.name}:0"
    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(simple_task_description(), trial_id=trial_id), trial_id=trial_id
        ),
        mock_grpc_context,
    )
    assert registered.success is True, registered.error

    async def never_returns(args):
        await asyncio.sleep(30)
        return json.dumps(args)

    runner_service.trials[trial_id].agent_tools["never_returns"] = never_returns
    return trial_id


def test_every_status_member_has_a_producing_recording_path(
    runner_service, mock_grpc_context, _timeout_trial
) -> None:
    observed = {
        _core_recorded_status("echo", {"payload": "hi"}),
        _core_recorded_status("echo", {"payload": "raise"}),
        _core_recorded_status("echo", {"payload": 17}),
        _core_recorded_status("no_such_tool", {}),
    }

    response = runner_service.ExecuteTool(
        execute_request(
            _timeout_trial, "never_returns", call_id="toolu_slow", timeout_seconds=0.05
        ),
        mock_grpc_context,
    )
    assert response.status == pb2.EXECUTION_STATUS_TIMEOUT, (
        "the runner did not time the call out, so TIMEOUT was never recorded and this "
        "lock would pass for the wrong reason"
    )
    observed.add(runner_service.trials[_timeout_trial].tool_call_history[-1].status)

    assert observed == set(ToolExecutionStatus), (
        "a ToolExecutionStatus member has no recording path that produces it, so a "
        f"matcher naming it could never fire: {sorted(m.value for m in set(ToolExecutionStatus) - observed)}"
    )
