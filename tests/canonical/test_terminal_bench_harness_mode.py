"""Pin the harness-mode trial: one exec, no turn loop, grading untouched.

A task whose ``TaskDescription.metadata`` carries ``agent_harness_command``
brings its own agent — a vendor coding-harness CLI that plans and edits
inside the trial's container. The conductor runs that command once instead
of driving :class:`~tolokaforge.core.loop.ToolCallingLoop`.

The three properties locked here are what make that safe:

* no ``ToolCallingLoop`` is constructed — the CLI is the only agent, and a
  second one on top of it would spend LLM budget re-solving the task;
* exactly one tool call reaches the runtime, carrying the adapter-built
  command and the harness deadline;
* the grading phase still fires, because it reads the trajectory and the
  trial's env state rather than how they were produced.

Driven through :meth:`InProcessConductor.run` against fakes, so the branch
is exercised end-to-end without a Docker daemon or an LLM key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core import runner as runner_module
from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    InitialStateConfig,
    ModelConfig,
    RateLimitProbeConfig,
    TaskConfig,
    TerminationReason,
    ToolsConfig,
    TrialStatus,
)
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import (
    InvocationStyle,
    TaskDescription,
    ToolSchema,
    ToolSource,
)
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.canonical

_TASK_ID = "echo-hello"
_INSTRUCTION = "Write hello to /app/out.txt"
_HARNESS_COMMAND = "claude --print 'Write hello to /app/out.txt'"
_HARNESS_TIMEOUT_S = 60.0
_CLI_OUTPUT = "wrote /app/out.txt\n"


def _bash_tool(timeout_s: float = _HARNESS_TIMEOUT_S) -> ToolSchema:
    return ToolSchema(
        name="bash",
        description="Execute a bash command inside the task container",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        category="compute",
        timeout_s=timeout_s,
        source=ToolSource(
            toolset="terminal_bench",
            module_path="",
            class_name="bash",
            invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
            extra={"service": "main", "compose_project_prefix": "tbench_"},
        ),
    )


class _RecordingRuntime:
    """Runtime backend fake recording every per-trial RPC the conductor makes."""

    def __init__(self, tools: list[ToolSchema]) -> None:
        self._tools = tools
        self.executed_tools: list[dict[str, Any]] = []
        self.graded: list[str] = []

    def register_trial(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "success": True,
            "num_agent_tools": len(self._tools),
            "tool_schemas": [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in self._tools
            ],
        }

    def execute_tool(self, **kwargs: Any) -> ToolResult:
        self.executed_tools.append(kwargs)
        return ToolResult(success=True, output=_CLI_OUTPUT, error=None)

    def get_state(self, trial_id: str, **kwargs: Any) -> dict[str, Any]:
        return {"success": True, "state_json": "{}"}


class _RecordingGrader:
    """Trial grader fake — records that the grading phase fired at all."""

    def __init__(self) -> None:
        self.graded: list[str] = []

    def grade(self, spec: TrialSpec, trajectory: Any, system_prompt: str) -> Grade:
        self.graded.append(spec.trial_id)
        return Grade(binary_pass=True, score=1.0, components=GradeComponents(), reasons="ok")


def _task_config() -> TaskConfig:
    return TaskConfig(
        task_id=_TASK_ID,
        name=_TASK_ID,
        category="terminal",
        description=_INSTRUCTION,
        adapter_type="terminal_bench",
        initial_user_message=_INSTRUCTION,
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(agent={"enabled": ["bash"]}, user={"enabled": []}),
    )


def _spec(metadata: dict[str, Any], tools: list[ToolSchema]) -> TrialSpec:
    return TrialSpec(
        trial_id=f"{_TASK_ID}:0",
        run_id="harness-canon",
        task=TaskDescription(
            task_id=_TASK_ID,
            name=_TASK_ID,
            category="terminal",
            description=_INSTRUCTION,
            adapter_type="terminal_bench",
            system_prompt="",
            agent_tools=tools,
            metadata=metadata,
        ),
        agent_model_config=ModelConfig(provider="anthropic", name="stub"),
        env_endpoints=EnvEndpoints(db_url="http://db:8000", runner_url="http://runner:50051"),
    )


@pytest.fixture
def harness_trial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run one harness trial through the real conductor against fakes."""

    def _run(
        *,
        metadata: dict[str, Any] | None = None,
        tools: list[ToolSchema] | None = None,
        episode_s: int = 3600,
    ):
        tools = tools if tools is not None else [_bash_tool()]
        metadata = (
            metadata
            if metadata is not None
            else {"agent_harness": "claude-code", "agent_harness_command": _HARNESS_COMMAND}
        )

        def _no_loop(*args: Any, **kwargs: Any):
            raise AssertionError(
                "ToolCallingLoop was constructed on the harness path — the CLI is "
                "the only agent the trial may run"
            )

        monkeypatch.setattr(runner_module, "ToolCallingLoop", _no_loop)

        adapter = MagicMock()
        adapter.get_task_dir.return_value = tmp_path
        adapter.create_environment.return_value = MagicMock(data={}, task_dir=tmp_path)
        adapter.get_grading_config.return_value = None

        config = MagicMock()
        config.orchestrator.timeouts.episode_s = episode_s
        # A real config value: the turn-loop branch validates the probe budget
        # against the episode budget, and a MagicMock reads as enabled.
        config.orchestrator.rate_limit_probe = RateLimitProbeConfig()
        config.models = {"agent": {"name": "stub", "provider": "anthropic"}}

        runtime = _RecordingRuntime(tools)
        grader = _RecordingGrader()
        agent_client = MagicMock()
        agent_client.capabilities.schema_sanitizer.sanitize.side_effect = lambda s: s

        conductor = InProcessConductor(
            adapter=adapter,
            artifact_writer=MagicMock(),
            config=config,
            logger=MagicMock(),
            verbose=False,
            strict=False,
            agent_client=agent_client,
            runtime_backend=runtime,
            trial_grader=grader,
            output_dir=tmp_path / "out",
            request_limiter=None,
        )
        spec = _spec(metadata, tools)
        result = conductor.run(spec, _task_config())
        return result, runtime, grader

    return _run


class TestHarnessTrialBypassesTheTurnLoop:
    def test_exactly_one_tool_call_carries_the_harness_command(self, harness_trial):
        _, runtime, _ = harness_trial()
        assert len(runtime.executed_tools) == 1
        call = runtime.executed_tools[0]
        assert call["tool_name"] == "bash"
        assert call["arguments"] == {"command": _HARNESS_COMMAND}
        assert call["executor"] == "agent"

    def test_the_tools_own_timeout_is_the_harness_deadline(self, harness_trial):
        _, runtime, _ = harness_trial()
        assert runtime.executed_tools[0]["timeout_seconds"] == _HARNESS_TIMEOUT_S

    def test_run_level_episode_cap_still_bounds_the_deadline(self, harness_trial):
        _, runtime, _ = harness_trial(episode_s=30)
        assert runtime.executed_tools[0]["timeout_seconds"] == 30.0

    def test_the_trial_completes_without_an_llm_turn(self, harness_trial):
        result, _, _ = harness_trial()
        assert result.trajectory.status is TrialStatus.COMPLETED

    def test_grading_still_fires(self, harness_trial):
        _, _, grader = harness_trial()
        assert grader.graded == [f"{_TASK_ID}:0"]


class TestHarnessTrajectoryShape:
    def test_instruction_and_cli_output_are_the_transcript(self, harness_trial):
        result, _, _ = harness_trial()
        assert result.trajectory is not None
        contents = [(m.role.value, m.content) for m in result.trajectory.messages]
        assert contents == [("user", _INSTRUCTION), ("assistant", _CLI_OUTPUT)]

    def test_the_cli_invocation_is_in_the_tool_log(self, harness_trial):
        """A post-mortem must be able to read back what was actually run."""
        result, _, _ = harness_trial()
        assert len(result.trajectory.tool_log) == 1
        recorded = result.trajectory.tool_log[0]
        assert recorded.tool_name == "bash"
        assert recorded.arguments == {"command": _HARNESS_COMMAND}
        assert recorded.output == _CLI_OUTPUT

    def test_termination_is_agent_done(self, harness_trial):
        result, _, _ = harness_trial()
        assert result.trajectory.termination_reason is TerminationReason.AGENT_DONE
        assert result.trajectory.metrics.tool_calls == 1


class TestHarnessModeSelection:
    def test_a_task_without_the_command_keeps_the_turn_loop(self, harness_trial):
        """No ``agent_harness_command`` routes to the turn loop, as before.

        The sentinel loop raises, which ``TrialRunner.run`` records as an
        initialization error — that recorded message is the evidence the
        branch was taken, and the run-level default stays the LLM path.
        """
        result, runtime, _ = harness_trial(metadata={"agent_harness": "terminus-2"})
        assert result.trajectory.status is TrialStatus.ERROR
        assert any(
            "ToolCallingLoop was constructed" in (m.content or "")
            for m in result.trajectory.messages
        )
        assert runtime.executed_tools == []

    def test_multiple_agent_tools_are_refused(self, harness_trial):
        with pytest.raises(RuntimeError, match="runs through exactly one"):
            harness_trial(tools=[_bash_tool(), _bash_tool()])
