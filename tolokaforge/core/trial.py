"""TrialSpec and TrialResult — the typed ``RegisterTrial`` wire format.

``EnvironmentManifest`` and its supporting models live alongside
``TaskDescription`` in :mod:`tolokaforge.runner.models`; they are re-exported
here for callers that work against the trial-spec surface.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from tolokaforge.core.models import ModelConfig, Trajectory
from tolokaforge.runner.models import (
    DependsOn,
    DependsOnCondition,
    EnvironmentManifest,
    HealthProbe,
    HealthProbeKind,
    InitialStateKind,
    InitialStateRef,
    NetworkMode,
    PortSpec,
    Resources,
    SecurityContext,
    ServiceSpec,
    TaskDescription,
    VolumeKind,
    VolumeMount,
)

DEFAULT_TOOL_TIMEOUT_S = 30.0
"""Fallback per-tool execution timeout when a trial does not specify one.

Single source of truth for the producer (orchestrator spec build), the gRPC
client default, and the runner-side consumer fallback."""


class EnvEndpoints(BaseModel):
    """URLs for trial-scoped environment services the runner talks to.

    Travels on the wire inside :class:`TrialSpec`. The orchestrator
    resolves the URLs once (from its service-discovery layer) and writes
    them here; the runner reads the same values per trial without
    inheriting any host-default. Co-located engine-internal services
    (TypeSense) do **not** belong here — they flow through
    ``TaskDescription.search_config``.
    """

    db_url: str
    """URL of the per-trial DB service the runner's tool layer calls."""

    rag_url: str | None = None
    """URL of the RAG service the runner's tool layer calls. ``None``
    when the run's service stack does not include RAG (e.g.
    ``core_stack``; ``rag-service`` ships in ``full_stack`` only)."""

    runner_url: str
    """URL of the runner gRPC endpoint as the orchestrator that produced
    this spec sees it (e.g. ``http://localhost:55310`` in auto-start
    mode, ``http://executor:50051`` for workers). Carried on the wire
    ahead of the remote-conductor design — today's value encodes the
    writer's local view of the runner; a future out-of-process consumer
    will need a network-reachable URL, which is on the design backlog."""

    model_config = {"extra": "forbid"}


class TrialSpec(BaseModel):
    """Everything a trial needs to run.

    Carried as ``RegisterTrialRequest.trial_spec_json`` over the runner gRPC
    contract.
    """

    # ---- Identity --------------------------------------------------------
    trial_id: str
    """Canonical ``"{task_id}:{trial_index}"`` identifier for this trial."""

    run_id: str = Field(min_length=1)
    """The orchestrator-level run this trial belongs to. Set by the
    orchestrator at the top of ``run()`` and persisted into the engine
    run-state file so workers can read the same value for resumes.
    Independent of the output directory's filesystem name."""

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

    judge_model_config: ModelConfig | None = None
    """LLM config for the read-only rubric judge. None when no selected task uses
    an llm_judge grading component."""

    max_turns: int | None = None
    """Optional per-trial turn cap override; ``None`` defers to the engine default."""

    default_tool_timeout_s: float | None = None
    """Optional per-tool default timeout in seconds; ``None`` defers to the
    runner, which falls back to ``DEFAULT_TOOL_TIMEOUT_S``."""

    # ---- Extension points ------------------------------------------------
    env_endpoints: EnvEndpoints
    """URLs for trial-scoped environment services. Required — the producer
    (orchestrator) resolves them once and the consumer (runner) reads
    them per trial."""

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


__all__ = [
    "DEFAULT_TOOL_TIMEOUT_S",
    "DependsOn",
    "DependsOnCondition",
    "EnvEndpoints",
    "EnvironmentManifest",
    "HealthProbe",
    "HealthProbeKind",
    "InitialStateKind",
    "InitialStateRef",
    "NetworkMode",
    "PortSpec",
    "Resources",
    "SecurityContext",
    "ServiceSpec",
    "TrialResult",
    "TrialSpec",
    "VolumeKind",
    "VolumeMount",
]
