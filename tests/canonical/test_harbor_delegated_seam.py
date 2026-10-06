"""Canonical: a Harbor trial rides the DELEGATED seam, no special path.

No Docker, no provider key. Two halves, both over the real
:class:`~tolokaforge_adapter_harbor.adapter.HarborAdapter` and the vendored TB2
pack at ``examples/harbor/write-release-note/``:

- **The built ``TaskDescription`` is a delegated handshake.**
  ``select_execution_mode`` classifies it ``DELEGATED`` from its metadata, it
  carries a single ``bash`` exec tool, its ``agent_harness_command`` starts a
  ``harbor run … -a terminus-2`` invocation, and grading is ``test_execution`` —
  the same emit shape every delegated adapter produces.
- **The trial dispatches through the runtime backend's ``execute_tool``.** Driving
  the production :meth:`~tolokaforge.core.runner.TrialRunner.run_harness` branch
  (the one the conductor takes for a ``DELEGATED`` spec) over a recording
  :class:`~tolokaforge.core.runtime.RuntimeBackend` and the production
  :class:`~tolokaforge.core.docker_adapter.DockerRunnerAdapter` tool executor,
  the whole trial is ONE ``execute_tool`` call carrying the ``harbor run``
  command — proving the delegated trial rides the same ``RuntimeBackend`` seam a
  tool call rides, with no harbor-specific dispatch path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tolokaforge_adapter_harbor.adapter import HarborAdapter

from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.core.execution_mode import ExecutionMode, select_execution_mode
from tolokaforge.core.llm import LLMClient
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.tools.registry import ToolExecutionStatus, ToolResult

pytestmark = pytest.mark.canonical

_PACK = Path(__file__).resolve().parents[2] / "examples" / "harbor"
_TASK_ID = "write-release-note"


def _adapter(tmp_path: Path) -> HarborAdapter:
    return HarborAdapter(
        {
            "harbor_tasks_dir": str(_PACK),
            "agent_model": "openrouter/anthropic/claude-sonnet-4.6",
            "staging_root": str(tmp_path / "staging"),
        }
    )


# ---------------------------------------------------------------------------
# Recording runtime backend — the one seam a delegated trial must ride
# ---------------------------------------------------------------------------


@dataclass
class _ExecuteCall:
    trial_id: str
    tool_name: str
    arguments: dict[str, Any]
    executor: str
    call_id: str


class _RecordingBackend(InMemoryRuntimeBackend):
    """In-memory backend whose ``execute_tool`` records the call and returns a
    successful :class:`ToolResult` — so a delegated trial that rides the seam
    completes, and the test can assert exactly what reached it."""

    def __init__(self) -> None:
        super().__init__()
        self.executed: list[_ExecuteCall] = []

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        self.executed.append(_ExecuteCall(trial_id, tool_name, dict(arguments), executor, call_id))
        return ToolResult(
            success=True,
            output="harbor run complete",
            status=ToolExecutionStatus.SUCCESS,
        )


# ---------------------------------------------------------------------------
# The built TaskDescription is a delegated handshake
# ---------------------------------------------------------------------------


def test_task_description_is_a_delegated_handshake(tmp_path: Path) -> None:
    desc = _adapter(tmp_path).to_task_description(_TASK_ID)

    # Classified DELEGATED from the metadata the producer stamped — the same
    # function the orchestrator uses at dispatch, so the test and production
    # read one field.
    assert select_execution_mode(desc.metadata) is ExecutionMode.DELEGATED

    # One exec tool, the bash command the single `harbor run` rides.
    assert len(desc.agent_tools) == 1
    assert desc.agent_tools[0].name == "bash"

    command = desc.metadata["agent_harness_command"]
    assert command.startswith("harbor run ")
    assert "-a terminus-2" in command

    # Graded off Harbor's own result.json via the shared test_execution runner.
    assert desc.grading.grading_method == "test_execution"


# ---------------------------------------------------------------------------
# The trial dispatches through the runtime backend's execute_tool
# ---------------------------------------------------------------------------


def test_trial_rides_the_runtime_backend_execute_tool_seam(tmp_path: Path) -> None:
    desc = _adapter(tmp_path).to_task_description(_TASK_ID)
    command = desc.metadata["agent_harness_command"]
    tool = desc.agent_tools[0]

    trial_id = f"{_TASK_ID}:0"
    backend = _RecordingBackend()
    # The production per-trial tool executor: binds trial_id + executor identity
    # to the backend and forwards each .execute() to backend.execute_tool().
    tool_executor = DockerRunnerAdapter(runtime=backend, trial_id=trial_id)

    runner = TrialRunner(
        task_id=_TASK_ID,
        trial_index=0,
        agent_client=LLMClient(ModelConfig(provider="openai", name="gpt-4")),
        user_simulator=None,
        tool_executor=tool_executor,
        tool_schemas=[],
        episode_timeout_s=1800,
        interaction_mode="agent_only",
    )

    trajectory = runner.run_harness(
        tool_name=tool.name,
        command=command,
        instruction="Write the release note.",
        timeout_s=tool.timeout_s,
    )

    # The whole delegated trial is exactly one execute_tool call, carrying the
    # harbor command as the bash tool's argument under the trial's identity.
    assert len(backend.executed) == 1
    call = backend.executed[0]
    assert call.trial_id == trial_id
    assert call.tool_name == "bash"
    assert call.executor == "agent"
    assert call.arguments == {"command": command}
    assert call.arguments["command"].startswith("harbor run ")

    # And the trial completed off that single call — no engine turn loop ran.
    assert trajectory.status.value == "completed"
