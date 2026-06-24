"""TrialSpec and TrialResult — the typed ``RegisterTrial`` wire format."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from tolokaforge.core.models import ModelConfig, Trajectory
from tolokaforge.runner.models import TaskDescription

DEFAULT_TOOL_TIMEOUT_S = 30.0
"""Fallback per-tool execution timeout when a trial does not specify one.

Single source of truth for the producer (orchestrator spec build), the gRPC
client default, and the runner-side consumer fallback."""


class TrialSpec(BaseModel):
    """Everything a trial needs to run.

    Carried as ``RegisterTrialRequest.trial_spec_json`` over the runner gRPC
    contract.
    """

    # ---- Identity --------------------------------------------------------
    trial_id: str
    """Canonical ``"{task_id}:{trial_index}"`` identifier for this trial."""

    run_id: str
    """The orchestrator-level run this trial belongs to."""

    attempt_id: int = 0
    """0-based retry counter. 0 means the first attempt."""

    worker_id: str | None = None
    """Identifier of the worker process that owns this attempt, or ``None``
    in single-process orchestrator mode."""

    # ---- The task itself -------------------------------------------------
    task: TaskDescription
    """The task pack the trial executes."""

    # ---- Per-trial execution parameters ----------------------------------
    agent_model_config: ModelConfig
    """LLM config for the agent loop."""

    user_model_config: ModelConfig | None = None
    """LLM config for the user simulator when the task drives one."""

    max_turns: int | None = None
    """Optional per-trial turn cap override; ``None`` defers to the engine default."""

    default_tool_timeout_s: float | None = None
    """Optional per-tool default timeout in seconds; ``None`` defers to the
    runner, which falls back to ``DEFAULT_TOOL_TIMEOUT_S``."""

    # ---- Extension points ------------------------------------------------
    env_endpoints: dict[str, str] = Field(default_factory=dict)
    """URLs for trial-scoped environment services, keyed by service name
    (``db``, ``rag``, …)."""

    runtime_context: dict[str, Any] = Field(default_factory=dict)
    """Free-form payload for runtime-backend-specific inputs (e.g. K8s pod
    spec hints, sandbox configuration)."""

    model_config = {"extra": "forbid"}


class TrialResult(BaseModel):
    """The result of a trial — ``Trajectory`` plus the canonical trial id and worker id."""

    trial_id: str
    """Canonical ``"{task_id}:{trial_index}"`` identifier."""

    trajectory: Trajectory
    """Status, grade, metrics, message trace, tool log, final env state."""

    worker_id: str | None = None
    """Identifier of the worker that ran this trial. ``None`` in single-process
    mode; populated under distributed execution."""

    model_config = {"extra": "forbid"}

    @classmethod
    def from_trajectory(
        cls, trial_id: str, trajectory: Trajectory, worker_id: str | None = None
    ) -> TrialResult:
        """Convenience constructor for the orchestrator-side aggregation path."""
        return cls(trial_id=trial_id, trajectory=trajectory, worker_id=worker_id)


__all__ = ["DEFAULT_TOOL_TIMEOUT_S", "TrialSpec", "TrialResult"]
