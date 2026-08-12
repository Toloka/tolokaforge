"""Builders for driving the conductor's grading and artifact-write phases.

``InProcessConductor.run()`` is not drivable without a real environment — it
dies inside ``EnvironmentState.hydrate()`` — so a test that needs the production
``_grade`` / ``_write_artifacts`` phases assembles their arguments here: a
conductor whose I/O seams (adapter, agent client, runtime backend) are doubles,
the :class:`~tolokaforge.core.conductor._TrialSetup` those phases read, and the
three ``TrialRunner`` attributes they touch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from tolokaforge.core.conductor import InProcessConductor, _TrialSetup
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    ActorSpec,
    EvaluationConfig,
    InitialStateConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    ToolsConfig,
)
from tolokaforge.core.models.task_config import InteractionMode
from tolokaforge.core.output.artifacts import FileArtifactWriter


@dataclass(frozen=True)
class RunnerStub:
    """The three ``TrialRunner`` attributes the two phases read."""

    effective_system_prompt: str
    user_system_prompt: str
    logger: StructuredLogger


class _UnsetActors:
    """Sentinel: the caller said nothing about ``actors``, so the scripted user
    actor applies. Distinct from an explicit ``actors=None``, which declares a
    task whose user actor resolves from the simulator defaults."""


_UNSET_ACTORS = _UnsetActors()


def make_run_config(output_dir: Path, *, repeats: int = 1) -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=repeats,
            auto_start_services=False,
            shuffle_trials=False,
            # A retry budget a run must decline to spend: with 0 a not-retried
            # assertion could not tell "declined" from "unavailable".
            max_attempt_retries=1,
        ),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
    )


def make_task_config(
    task_id: str,
    *,
    interaction_mode: InteractionMode = "conversational",
    actors: dict[str, ActorSpec] | None | _UnsetActors = _UNSET_ACTORS,
    initial_user_message: str | None = None,
) -> TaskConfig:
    return TaskConfig(
        task_id=task_id,
        name=task_id,
        category="tool_use",
        description="d",
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        interaction_mode=interaction_mode,
        actors={"user": ActorSpec(mode="scripted")} if isinstance(actors, _UnsetActors) else actors,
        initial_user_message=initial_user_message,
        grading="grading.yaml",
    )


def make_conductor(config: RunConfig, output_dir: Path, grader: Any) -> InProcessConductor:
    agent_client = MagicMock()
    agent_client.config = ModelConfig(provider="openai", name="gpt-4")
    agent_client.capabilities.schema_sanitizer.sanitize.return_value = []
    adapter = MagicMock()
    adapter.get_grading_config.return_value = None
    return InProcessConductor(
        adapter=adapter,
        artifact_writer=FileArtifactWriter(),
        config=config,
        logger=StructuredLogger("test-conductor-phases"),
        agent_client=agent_client,
        runtime_backend=MagicMock(),
        trial_grader=grader,
        output_dir=output_dir,
    )


def make_setup(output_dir: Path, task_id: str, trial_idx: int) -> _TrialSetup:
    return _TrialSetup(
        trial_id=f"{task_id}:{trial_idx}",
        trial_idx=trial_idx,
        task_dir=output_dir,
        trial_dir=output_dir / "trials" / task_id / str(trial_idx),
        env_state=MagicMock(),
        adapter_env=MagicMock(),
        tool_schemas=[],
        tool_executor=MagicMock(),
    )


def runner_stub() -> RunnerStub:
    return RunnerStub(
        effective_system_prompt="You are a test assistant.",
        user_system_prompt="You are a user.",
        logger=StructuredLogger("test-conductor-phases-trial"),
    )
