"""The run-level agent episode-timeout override.

``orchestrator.timeouts.agent_episode_s`` supersedes the task-derived agent
budget — the ``timeout_s`` an adapter assigns each agent tool (for a vendor-CLI
harness, the task's ``[agent] timeout_sec``). These tests pin the two halves of
the contract the override promises:

- **set** — every agent tool's ``timeout_s`` becomes the override, the value
  crosses the register wire (so the runner's per-command budget and its
  in-container watchdog read it), the harness branch derives its episode budget
  from it, and a short override clears the ``episode_s`` floor that the longer
  task value would have tripped;
- **unset** — the registered timeouts are byte-identical to the task's, so a run
  that sets nothing reproduces today's behaviour exactly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import TaskDescription, ToolSchema

pytestmark = pytest.mark.unit


def _conductor(tmp_path: Path, *, agent_episode_s: int | None, episode_s: int = 1800):
    return InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(
                auto_start_services=False,
                timeouts={"episode_s": episode_s, "agent_episode_s": agent_episode_s},
            ),
            evaluation=EvaluationConfig(output_dir=str(tmp_path)),
        ),
        logger=MagicMock(),
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        trial_grader=MagicMock(),
        output_dir=tmp_path,
    )


def _spec(*tool_timeouts: float) -> TrialSpec:
    return TrialSpec(
        trial_id="t1:0",
        run_id="test-run",
        task=TaskDescription(
            task_id="t1",
            name="t1",
            category="test",
            description="stub",
            adapter_type="terminal_bench",
            system_prompt="",
            agent_tools=[
                ToolSchema(
                    name=f"agent_{i}",
                    description="d",
                    parameters={"type": "object", "properties": {}},
                    timeout_s=timeout,
                )
                for i, timeout in enumerate(tool_timeouts)
            ],
        ),
        agent_model_config=ModelConfig(provider="openai", name="gpt-4"),
        default_tool_timeout_s=30.0,
        env_endpoints=EnvEndpoints(db_url="http://db:8000", runner_url="http://runner:50051"),
    )


class TestOverrideRewritesTimeouts:
    def test_set_override_replaces_every_agent_tool_timeout(self, tmp_path: Path) -> None:
        spec = _spec(1800.0, 300.0)

        _conductor(tmp_path, agent_episode_s=120)._apply_agent_episode_override(spec)

        assert [tool.timeout_s for tool in spec.task.agent_tools] == [120.0, 120.0]

    def test_overridden_timeout_crosses_the_register_wire(self, tmp_path: Path) -> None:
        """The runner resolves the per-command budget and its watchdog backstop
        from the registered ``ToolSchema.timeout_s``, so the override has to be
        the value serialised into ``trial_spec_json`` — not just a conductor-side
        local."""
        spec = _spec(1800.0)

        _conductor(tmp_path, agent_episode_s=90)._apply_agent_episode_override(spec)
        wire = json.loads(spec.model_dump_json(exclude={"task": {"environment_manifest"}}))

        assert [t["timeout_s"] for t in wire["task"]["agent_tools"]] == [90.0]

    def test_unset_override_leaves_timeouts_unchanged(self, tmp_path: Path) -> None:
        spec = _spec(1800.0, 300.0)

        _conductor(tmp_path, agent_episode_s=None)._apply_agent_episode_override(spec)

        assert [tool.timeout_s for tool in spec.task.agent_tools] == [1800.0, 300.0]

    def test_override_does_not_mutate_the_adapters_cached_tools(self, tmp_path: Path) -> None:
        """The override copies the task description; the ``ToolSchema`` objects
        the adapter cached (and shares across concurrent trials) keep their
        declared budget."""
        spec = _spec(1800.0)
        cached_tool = spec.task.agent_tools[0]

        _conductor(tmp_path, agent_episode_s=120)._apply_agent_episode_override(spec)

        assert cached_tool.timeout_s == 1800.0
        assert spec.task.agent_tools[0] is not cached_tool


class TestHarnessEpisodeBudget:
    """The harness branch derives its episode budget — and the deadline handed
    to ``run_harness`` — from the agent tool's ``timeout_s``. The override has to
    reach it, and a short override has to clear the ``episode_s`` floor that the
    longer task value trips."""

    def _setup(self) -> MagicMock:
        setup = MagicMock()
        setup.trial_id = "t1:0"
        setup.task_dir = Path("/tmp")
        setup.tool_schemas = [
            {
                "type": "function",
                "function": {"name": "agent_0", "description": "d", "parameters": {}},
            }
        ]
        return setup

    def test_a_long_task_timeout_trips_the_episode_floor_without_an_override(
        self, tmp_path: Path
    ) -> None:
        # Agent tool declares 3000 s; the run's episode floor is 1800 s.
        spec = _spec(3000.0)
        conductor = _conductor(tmp_path, agent_episode_s=None, episode_s=1800)

        with pytest.raises(RuntimeError, match="the run allows 1800s"):
            conductor._run_harness_trial(
                spec, TaskConfig(task_id="t1", description="d"), self._setup(), "run"
            )

    def test_a_short_override_clears_the_floor_and_reaches_run_harness(
        self, tmp_path: Path
    ) -> None:
        spec = _spec(3000.0)
        conductor = _conductor(tmp_path, agent_episode_s=600, episode_s=1800)
        # The override is applied in ``_setup_trial`` before this branch runs.
        conductor._apply_agent_episode_override(spec)

        with (
            patch.object(InProcessConductor, "_build_system_prompt", return_value="sys"),
            patch("tolokaforge.core.conductor.TrialRunner") as runner_cls,
        ):
            conductor._run_harness_trial(
                spec, TaskConfig(task_id="t1", description="d"), self._setup(), "run"
            )

        run_harness = runner_cls.return_value.run_harness
        assert run_harness.call_args.kwargs["timeout_s"] == 600.0
        assert runner_cls.call_args.kwargs["episode_timeout_s"] == 600
