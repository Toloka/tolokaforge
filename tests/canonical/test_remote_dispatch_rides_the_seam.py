"""A native and a delegated trial both reach a graded result through the remote backend.

The backend is the one the :class:`Orchestrator` builds for ``--runtime remote``
from ``orchestrator.remote``; only the transport is replaced, by a recording
runner client. The trial runs through the same execution unit the orchestrator
builds, with the real in-process conductor and the real ``runner_rpc`` grader:
no local stack, no LLM, no node.

Both trials bottom out at ``backend.execute_tool`` / ``backend.grade_trial`` on
the client leased for that trial. The native agent's tool call comes out of the
engine loop; the delegated trial runs the adapter's harness command as its one
tool call. Neither dispatch path knows which kind of trial it carries.
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_env_endpoints, write_yaml_file
from tests.canonical._fakes import RecordingClientFactory
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import get_logger
from tolokaforge.core.loop import TerminationDecision, classify_loop_error
from tolokaforge.core.models import (
    EvaluationConfig,
    GraderConfig,
    Message,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    ToolCall,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.output.artifacts import InMemoryArtifactWriter
from tolokaforge.core.remote_runtime import RemoteRuntimeBackend
from tolokaforge.core.trial import TrialResult, TrialSpec
from tolokaforge.core.trial_node import (
    TrialExecutionContext,
    build_single_task_adapter,
    build_trial_execution_unit,
)

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_NODE = "node-a:50051"
_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")
_MAX_MB = 9


class _ScriptedAgent:
    """Agent client replaying a fixed list of generations."""

    def __init__(self, script: list[GenerationResult]) -> None:
        self._script = list(script)
        self.capabilities = ModelCapabilities()
        self.config = _AGENT

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: object = None,
    ) -> GenerationResult:
        return self._script.pop(0)

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


def _generation(text: str, *calls: ToolCall) -> GenerationResult:
    return GenerationResult(text=text, tool_calls=list(calls), usage=Usage(prompt_tokens=1))


def _orchestrator_built_backend(
    tmp_path: Path, factory: RecordingClientFactory
) -> tuple[RemoteRuntimeBackend, RunConfig]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        config = RunConfig(
            models={"agent": _AGENT, "user": _USER},
            orchestrator=OrchestratorConfig(
                workers=1,
                repeats=1,
                runtime="remote",
                remote={"nodes": [_NODE], "lease_timeout_s": 5},
            ),
            evaluation=EvaluationConfig(output_dir=str(tmp_path / "out")),
            compute={"grpc_max_message_mb": _MAX_MB},
        )
    backend = Orchestrator(config)._construct_runtime_backend("unused:1")
    assert isinstance(backend, RemoteRuntimeBackend)
    backend.runner_client_factory = factory
    return backend, config


def _run_trial(
    tmp_path: Path,
    task: TaskConfig,
    spec: TrialSpec,
    agent: _ScriptedAgent,
    factory: RecordingClientFactory,
) -> tuple[TrialResult, RemoteRuntimeBackend]:
    backend, config = _orchestrator_built_backend(tmp_path, factory)
    unit = build_trial_execution_unit(
        TrialExecutionContext(
            adapter=build_single_task_adapter(task),
            artifact_writer=InMemoryArtifactWriter(),
            config=config,
            logger=get_logger("remote-seam", strict=False),
            agent_client=agent,
            runtime_backend=backend,
            output_dir=tmp_path / "out",
            request_limiter=None,
            grader_config=GraderConfig(name="runner_rpc"),
            grpc_max_message_mb=_MAX_MB,
        )
    )
    try:
        return unit.execute(spec, task), backend
    finally:
        unit.close()
        backend.close()


def _spec_for(task: TaskConfig, **description_overrides: Any) -> TrialSpec:
    description = build_single_task_adapter(task).to_task_description(task.task_id)
    return TrialSpec(
        trial_id=f"{task.task_id}:0",
        run_id="remote-seam",
        task=description.model_copy(update=description_overrides),
        agent_model_config=_AGENT,
        user_model_config=_USER,
        env_endpoints=make_env_endpoints(),
    )


@pytest.fixture
def native_task(tmp_path: Path) -> TaskConfig:
    task_dir = tmp_path / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "flat",
            "name": "flat",
            "category": "tool_use",
            "description": "flat",
            "interaction_mode": "agent_only",
            "initial_user_message": "add a note",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "grading": "grading.yaml",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {"weights": {"state_checks": 1.0}, "pass_threshold": 1.0},
            "state_checks": {"jsonpaths": [{"path": "$.db.notes", "equals": []}]},
        },
    )
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"}).get_task(
        "flat"
    )


@pytest.fixture
def delegated_task(
    test_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TaskConfig:
    from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    adapter = TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(test_data_dir / "terminal_bench_tasks"),
            "staging_root": str(tmp_path / "staging"),
        }
    )
    return adapter.get_task("echo-hello")


def test_native_trial_dispatches_through_the_leased_node_to_a_graded_result(
    tmp_path: Path, native_task: TaskConfig
) -> None:
    echo_schema = {
        "name": "echo",
        "description": "Echo",
        "parameters": {"type": "object", "properties": {}},
    }
    factory = RecordingClientFactory(tool_schemas=[echo_schema], tool_output="echoed")
    agent = _ScriptedAgent(
        [
            _generation("calling", ToolCall(id="call-1", name="echo", arguments={})),
            _generation("done"),
        ]
    )

    result, _ = _run_trial(tmp_path, native_task, _spec_for(native_task), agent, factory)

    (client,) = factory.clients
    assert client.runner_address == _NODE
    assert client.max_message_mb == _MAX_MB
    assert client.names() == [
        "register_trial",
        "execute_tool",
        "get_state",
        "grade_trial",
        "cleanup_trial",
    ]
    assert client.closed
    assert result.trajectory.status is TrialStatus.COMPLETED
    assert result.trajectory.grade is not None
    assert result.trajectory.grade.reasons == f"graded on {_NODE}"
    assert result.trajectory.tool_log[0].tool_name == "echo"
    assert result.trajectory.tool_log[0].output == "echoed"


def test_delegated_trial_dispatches_through_the_leased_node_to_a_graded_result(
    tmp_path: Path, delegated_task: TaskConfig
) -> None:
    assert delegated_task.adapter_type == "terminal_bench"
    spec = _spec_for(delegated_task)
    harness_command = "claude --print 'write hello'"
    spec = spec.model_copy(
        update={
            "task": spec.task.model_copy(
                update={
                    "metadata": {**spec.task.metadata, "agent_harness_command": harness_command}
                }
            )
        }
    )
    (tool,) = spec.task.agent_tools
    factory = RecordingClientFactory(
        tool_schemas=[
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
        ],
        tool_output="wrote hello\n",
    )

    result, _ = _run_trial(tmp_path, delegated_task, spec, _ScriptedAgent([]), factory)

    (client,) = factory.clients
    assert client.runner_address == _NODE
    executed = [payload for name, payload in client.calls if name == "execute_tool"]
    assert [call["arguments"] for call in executed] == [{"command": harness_command}]
    assert client.names() == [
        "register_trial",
        "execute_tool",
        "grade_trial",
        "cleanup_trial",
    ]
    assert result.trajectory.status is TrialStatus.COMPLETED
    assert result.trajectory.grade is not None
    assert result.trajectory.grade.reasons == f"graded on {_NODE}"
