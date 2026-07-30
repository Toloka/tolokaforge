"""The provider's tool-call id is required and reaches every executor.

``ToolCall.id`` is the only key that joins a tool call to the tool result it
produced. Position does not resolve the case that matters — the same tool called
twice with byte-identical arguments — so the id is required at construction *and*
at load, and every executor the engine drives is handed it explicitly.

The runner-side half (the id on the wire and in the runner's recorded history)
is locked in ``tests/unit/test_runner_pipeline.py``.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.core.models import Message, MessageRole, ToolCall, Trajectory, TrialStatus
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry, ToolResult

pytestmark = pytest.mark.unit


class _RecordingRuntime:
    """Captures what :class:`DockerRunnerAdapter` forwards to the runtime seam."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float = 30.0,
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        self.calls.append(
            {
                "trial_id": trial_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "executor": executor,
                "call_id": call_id,
            }
        )
        return ToolResult(success=True, output="ok")


class _Echo(Tool):
    def __init__(self) -> None:
        super().__init__("echo", "Echo the payload back")

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo the payload back",
                "parameters": {
                    "type": "object",
                    "properties": {"payload": {"type": "string"}},
                    "required": ["payload"],
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output=str(kwargs.get("payload")))


def _trajectory_payload(call_id: str) -> dict[str, Any]:
    """A recorded-bundle payload shaped like ``trajectory.yaml``."""
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    return {
        "task_id": "call-id-contract",
        "trial_index": 0,
        "start_ts": ts,
        "end_ts": ts,
        "status": TrialStatus.COMPLETED.value,
        "messages": [
            {
                "role": MessageRole.ASSISTANT.value,
                "content": "",
                "tool_calls": [{"id": call_id, "name": "refund", "arguments": {"id": "PAY-1"}}],
                "ts": ts,
            }
        ],
    }


class TestToolCallIdIsRequired:
    def test_empty_id_is_rejected_and_the_tool_is_named(self) -> None:
        with pytest.raises(ValidationError, match="tool call for 'refund' carries an empty id"):
            ToolCall(id="", name="refund", arguments={"id": "PAY-1"})

    def test_a_provider_id_is_accepted_unchanged(self) -> None:
        call = ToolCall(id="toolu_vrtx_01NQ96", name="refund", arguments={})
        assert call.id == "toolu_vrtx_01NQ96"

    def test_a_message_carrying_an_unnamed_call_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError, match="carries an empty id"):
            Message(
                role=MessageRole.ASSISTANT,
                tool_calls=[ToolCall.model_construct(id="", name="refund", arguments={})],
            )

    def test_loading_a_recorded_bundle_without_a_call_id_is_rejected(self) -> None:
        """``Trajectory.model_validate`` runs on every recorded bundle, so the
        contract gates replay and re-grading, not only live provider output."""
        with pytest.raises(ValidationError, match="carries an empty id"):
            Trajectory.model_validate(_trajectory_payload(""))

    def test_loading_a_recorded_bundle_with_a_call_id_still_works(self) -> None:
        trajectory = Trajectory.model_validate(_trajectory_payload("toolu_vrtx_01NQ96"))
        assert trajectory.messages[0].tool_calls[0].id == "toolu_vrtx_01NQ96"


class TestDockerRunnerAdapterCallId:
    def test_call_id_reaches_the_runtime(self) -> None:
        runtime = _RecordingRuntime()
        adapter = DockerRunnerAdapter(runtime, trial_id="t:0")

        adapter.execute("echo", {"payload": "hi"}, call_id="toolu_A")

        assert runtime.calls[0]["call_id"] == "toolu_A"

    def test_call_id_is_never_sent_to_the_tool_as_an_argument(self) -> None:
        """``execute`` folds ``**kwargs`` into the tool's own arguments, so an id
        arriving that way would be delivered to the tool as a parameter."""
        runtime = _RecordingRuntime()
        adapter = DockerRunnerAdapter(runtime, trial_id="t:0")

        adapter.execute("echo", {"payload": "hi"}, call_id="toolu_A")

        assert runtime.calls[0]["arguments"] == {"payload": "hi"}

    def test_recorded_adapter_log_carries_the_call_id(self) -> None:
        runtime = _RecordingRuntime()
        adapter = DockerRunnerAdapter(runtime, trial_id="t:0")

        adapter.execute("echo", {"payload": "hi"}, call_id="toolu_A")

        assert adapter.get_logs()[0]["call_id"] == "toolu_A"

    def test_the_recording_runtime_matches_the_backend_calling_contract(self) -> None:
        """Proof the spy is not looser than production: it accepts exactly the
        parameters :class:`RuntimeBackend.execute_tool` declares, so a ``call_id``
        dropped from the Protocol would surface here rather than passing silently."""

        def shape(func: Any) -> list[tuple[str, Any, Any]]:
            params = list(inspect.signature(func).parameters.values())
            return [(p.name, p.kind, p.default) for p in params if p.name != "self"]

        assert shape(_RecordingRuntime.execute_tool) == shape(RuntimeBackend.execute_tool)


class TestInProcessExecutorCallId:
    def test_two_identical_calls_are_distinguishable_in_the_log(self) -> None:
        registry = ToolRegistry()
        registry.register(_Echo())
        executor = ToolExecutor(registry)

        executor.execute("echo", {"payload": "hi"}, call_id="toolu_A")
        executor.execute("echo", {"payload": "hi"}, call_id="toolu_B")

        assert [log["call_id"] for log in executor.get_logs()] == ["toolu_A", "toolu_B"]
