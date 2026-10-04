"""Builders for driving the conductor's grading and artifact-write phases.

``InProcessConductor.run()`` is not drivable without a real environment — it
dies inside ``EnvironmentState.hydrate()`` — so a test that needs the production
``_grade`` / ``_write_artifacts`` phases assembles their arguments here: a
conductor whose I/O seams (adapter, agent client, runtime backend) are doubles,
the :class:`~tolokaforge.core.conductor._TrialSetup` those phases read, and the
three ``TrialRunner`` attributes they touch. A test that drives
``_run_agent_loop`` itself takes :data:`AGENT_LOOP_IDENTITY`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from tolokaforge.core.conductor import InProcessConductor, _TrialSetup
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter, TrialArtifactWriter
from tolokaforge.observability.observer import TrialIdentity

AGENT_LOOP_IDENTITY = TrialIdentity(run_id="run", task_id="t1", trial_index=0, attempt_id=0)
"""A fixed identity for a test that calls ``InProcessConductor._run_agent_loop``
directly, whatever its spec says."""


@dataclass(frozen=True)
class RunnerStub:
    """The ``TrialRunner`` attributes the two phases read."""

    effective_system_prompt: str
    user_system_prompt: str
    logger: StructuredLogger
    # Staged harness artifacts the ``native`` / ``both`` write phase reads;
    # ``None`` mirrors a trial that preserved no native artifacts.
    harness_native_artifacts: dict[str, bytes] | None = None


def make_run_config(output_dir: Path, *, repeats: int = 1) -> RunConfig:
    # models.user is required — the orchestrator fails loud otherwise (see
    # require_user_simulator_config).
    return RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
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


def make_conductor(
    config: RunConfig,
    output_dir: Path,
    grader: Any,
    *,
    artifact_writer: TrialArtifactWriter | None = None,
) -> InProcessConductor:
    agent_client = MagicMock()
    agent_client.config = ModelConfig(provider="openai", name="gpt-4")
    agent_client.capabilities.schema_sanitizer.sanitize.return_value = []
    adapter = MagicMock()
    adapter.get_grading_config.return_value = None
    # Single-adapter double: ``for_entry`` returns the same configured adapter,
    # mirroring ``BaseAdapter.for_entry``'s no-op default so the conductor's
    # per-trial ``_adapter_for`` resolution reaches the configured seams.
    adapter.for_entry.return_value = adapter
    return InProcessConductor(
        adapter=adapter,
        artifact_writer=artifact_writer or FileArtifactWriter(),
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
        user_tool_schemas=[],
        user_tool_executor=None,
    )


def runner_stub(*, harness_native_artifacts: dict[str, bytes] | None = None) -> RunnerStub:
    return RunnerStub(
        effective_system_prompt="You are a test assistant.",
        user_system_prompt="You are a user.",
        logger=StructuredLogger("test-conductor-phases-trial"),
        harness_native_artifacts=harness_native_artifacts,
    )
