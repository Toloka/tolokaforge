"""Shared model factories for canonical + unit tests.

Each test file that constructs one of these Pydantic models centralises
HOW the model is constructed here, so a new required field on the
underlying model lands in one place. Callers pass site-specific literals
as keyword overrides.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tolokaforge.core.models import (
    InitialStateConfig,
    Metrics,
    ModelConfig,
    TaskConfig,
    TerminationReason,
    ToolsConfig,
    Trajectory,
    TrialStatus,
    UserSimulatorConfig,
)
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import TaskDescription


def make_task_description(
    *,
    task_id: str = "task",
    name: str = "task",
    category: str = "test",
    description: str = "contract-test task",
    adapter_type: str = "native",
    system_prompt: str = "",
    environment_manifest: EnvironmentManifest | None = None,
) -> TaskDescription:
    return TaskDescription(
        task_id=task_id,
        name=name,
        category=category,
        description=description,
        adapter_type=adapter_type,
        system_prompt=system_prompt,
        environment_manifest=environment_manifest,
    )


def make_env_endpoints(
    *,
    db_url: str = "http://db.local:8000",
    rag_url: str | None = None,
    runner_url: str = "http://runner.local:50051",
) -> EnvEndpoints:
    return EnvEndpoints(db_url=db_url, rag_url=rag_url, runner_url=runner_url)


def make_task_config(
    *,
    task_id: str = "task-1",
    name: str = "task-1",
    category: str = "test",
    description: str = "test task",
    grading: str = "grading.yaml",
) -> TaskConfig:
    return TaskConfig(
        task_id=task_id,
        name=name,
        category=category,
        description=description,
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        user_simulator=UserSimulatorConfig(mode="scripted"),
        grading=grading,
    )


def make_trial_spec(
    *,
    trial_id: str = "task-1:0",
    run_id: str = "run-1",
    task_id: str = "task-1",
    agent_model_config: ModelConfig | None = None,
    env_endpoints: EnvEndpoints | None = None,
) -> TrialSpec:
    return TrialSpec(
        trial_id=trial_id,
        run_id=run_id,
        task=make_task_description(task_id=task_id),
        agent_model_config=agent_model_config or ModelConfig(provider="openai", name="gpt-4"),
        env_endpoints=env_endpoints or make_env_endpoints(),
    )


def make_trajectory(
    *,
    task_id: str = "task-1",
    trial_index: int = 0,
    status: TrialStatus = TrialStatus.COMPLETED,
    termination_reason: TerminationReason | None = None,
    metrics: Metrics | None = None,
) -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_index,
        start_ts=now,
        end_ts=now,
        status=status,
        termination_reason=termination_reason,
        messages=[],
        metrics=metrics or Metrics(),
    )
