"""Shared model factories for canonical + unit tests.

Each test file that constructs one of these Pydantic models centralises
HOW the model is constructed here, so a new required field on the
underlying model lands in one place. Callers pass site-specific literals
as keyword overrides.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from tolokaforge.core.models import (
    ActorSpec,
    InitialStateConfig,
    Message,
    MessageRole,
    Metrics,
    ModelConfig,
    RecordedToolCall,
    TaskConfig,
    TerminationReason,
    ToolCall,
    ToolsConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.models.task_config import InteractionMode
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


class _DefaultScriptedUser:
    """Sentinel: the caller said nothing about ``actors``, so a scripted user
    actor applies. Distinct from an explicit ``actors=None``, which declares a
    task whose user actor resolves from the simulator defaults."""


_DEFAULT_SCRIPTED_USER = _DefaultScriptedUser()


def make_task_config(
    task_id: str = "task-1",
    *,
    name: str | None = None,
    category: str = "test",
    description: str = "test task",
    interaction_mode: InteractionMode = "conversational",
    actors: dict[str, ActorSpec] | None | _DefaultScriptedUser = _DEFAULT_SCRIPTED_USER,
    initial_user_message: str | None = None,
    grading: str = "grading.yaml",
) -> TaskConfig:
    return TaskConfig(
        task_id=task_id,
        name=name or task_id,
        category=category,
        description=description,
        initial_state=InitialStateConfig(),
        tools=ToolsConfig(),
        interaction_mode=interaction_mode,
        actors=(
            {"user": ActorSpec(mode="scripted")}
            if isinstance(actors, _DefaultScriptedUser)
            else actors
        ),
        initial_user_message=initial_user_message,
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


def write_yaml_file(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` as YAML, creating parent dirs as needed.

    Shared by the native-adapter tests that stage a task-directory
    fixture on tmp_path; centralised so a schema/serialisation tweak
    lands in one place instead of drifting across N duplicated helpers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def make_trial_messages(calls: Sequence[RecordedToolCall], turns: tuple[str, str]) -> list[Message]:
    """The message view a trial leaves behind, as ``ToolCallingLoop`` writes it.

    A user turn, an assistant turn declaring every call, and then one ``role: tool``
    message per executed call carrying that call's output and keyed by its id. The
    last part is what a bundle persists and what the timeline falls back to for
    ``result`` text, so a view that omits it understates what a re-graded bundle can
    still decide.
    """
    return make_turn_trial_messages([calls], turns)


def make_turn_trial_messages(
    calls_per_turn: Sequence[Sequence[RecordedToolCall]], turns: tuple[str, str]
) -> list[Message]:
    """The same message view, with the calls grouped by the turn that declared them.

    :func:`make_trial_messages` lands every call on one assistant turn, which is what
    a fixture asserting about one turn wants. This one takes the calls per turn, for
    the properties whose whole subject is *which* turn declared a call — a provider
    numbering its tool-call ids within a turn reuses one only across turns, so a
    single-turn view cannot express that trial at all.
    """
    user, assistant = turns
    messages = [Message(role=MessageRole.USER, content=user)]
    for turn_calls in calls_per_turn:
        messages.append(
            Message(
                role=MessageRole.ASSISTANT,
                content=assistant,
                tool_calls=[
                    ToolCall(id=call.call_id, name=call.tool_name, arguments=call.arguments)
                    for call in turn_calls
                ],
            )
        )
        messages.extend(
            Message(role=MessageRole.TOOL, content=call.output, tool_call_id=call.call_id)
            for call in turn_calls
        )
    return messages


def make_trajectory(
    *,
    task_id: str = "task-1",
    trial_index: int = 0,
    status: TrialStatus = TrialStatus.COMPLETED,
    termination_reason: TerminationReason | None = None,
    metrics: Metrics | None = None,
    messages: list[Message] | None = None,
    tool_log: list[RecordedToolCall] | None = None,
    grading_error: str | None = None,
) -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_index,
        start_ts=now,
        end_ts=now,
        status=status,
        termination_reason=termination_reason,
        messages=messages or [],
        tool_log=tool_log or [],
        metrics=metrics or Metrics(),
        grading_error=grading_error,
    )
