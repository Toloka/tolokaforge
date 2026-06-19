"""Typed control-plane ↔ trial wire format.

This module defines two types that fix the shape of what flows from the
orchestrator to a per-trial execution (``TrialSpec``) and what comes back
(``TrialResult``).

Today the engine threads a serialised ``TaskDescription`` through the runner
gRPC contract and assembles per-trial context inline from ad-hoc kwargs.
That works for the in-process case but leaves the boundary blurry — every
later refactor (env endpoints, runtime backend, conductor split, remote
runner) has to either invent its own shape or extend the JSON blob with
informal fields.

``TrialSpec`` makes the inbound surface explicit. It wraps the existing
``TaskDescription`` (no rewrite) and adds the per-trial context fields the
orchestrator currently passes as ad-hoc kwargs. Forward-looking slots —
``env_endpoints`` (typed in the next stage) and ``runtime_context`` (extension
point for runtime-backend payloads) — are kept as untyped dicts so later
stages add typing in place, without revising ``TrialSpec`` itself.

``TrialResult`` is a thin wrapper. The existing ``Trajectory`` model already
carries the trial's status, grade, metrics, message trace, and tool log;
duplicating those fields onto a new model would be ceremony. ``TrialResult``
adds only what ``Trajectory`` lacks: the canonical combined trial identifier
and a ``worker_id`` slot for distributed execution.

This is interface-only. No new behaviour. The runner consumes ``spec.task``
exactly as it used to consume the raw ``TaskDescription`` JSON; the
orchestrator continues to drive the same per-trial loop. Future stages
formalise the seams these types reach toward.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from tolokaforge.core.models import ModelConfig, Trajectory
from tolokaforge.runner.models import TaskDescription


class TrialSpec(BaseModel):
    """Everything a trial needs to run: identity + task + execution parameters.

    Carried as ``RegisterTrialRequest.trial_spec_json`` over the runner gRPC
    contract. Future seams that need per-trial context (env endpoints,
    runtime payloads, …) extend either the typed fields below or the
    ``runtime_context`` extension point — never a parallel ad-hoc kwarg path.
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
    """Embedded ``TaskDescription`` — the task pack the trial executes.
    Reused as-is; no rewrites of the existing schema in this seam."""

    # ---- Per-trial execution parameters ----------------------------------
    agent_model_config: ModelConfig
    """LLM config for the agent loop."""

    user_model_config: ModelConfig | None = None
    """LLM config for the user simulator when the task drives one."""

    max_turns: int | None = None
    """Optional per-trial turn cap override; ``None`` defers to the engine default."""

    default_tool_timeout_s: float | None = None
    """Optional per-tool default timeout in seconds; ``None`` defers to the runner."""

    # ---- Forward-looking extension points --------------------------------
    env_endpoints: dict[str, str] = Field(default_factory=dict)
    """URLs for trial-scoped environment services (db, rag, …). Untyped here
    because the typed ``EnvEndpoints`` model lands with the next stage —
    leaving this as a dict means that change adds typing in place rather
    than revising ``TrialSpec``."""

    runtime_context: dict[str, Any] = Field(default_factory=dict)
    """Free-form extension point for runtime-backend-specific payloads
    (e.g. K8s pod spec hints, sandbox configuration). Kept open so a new
    backend doesn't need a ``TrialSpec`` change to carry its inputs."""

    model_config = {"extra": "forbid"}


class TrialResult(BaseModel):
    """The result of a trial — thin wrapper around ``Trajectory``.

    The existing ``Trajectory`` already carries everything that semantically
    belongs to the result (status, grade, metrics, messages, tool log).
    ``TrialResult`` adds only the canonical combined trial identifier and a
    forward-looking ``worker_id`` slot. When a later stage formalises a
    ``Conductor.run(spec) → TrialResult`` seam, the conductor's return type
    has a single named shape without breaking the existing ``Trajectory``
    consumers (reports, snapshots, golden tests).
    """

    trial_id: str
    """Canonical ``"{task_id}:{trial_index}"`` identifier — the
    ``TaskDescription`` carries ``task_id`` separately and ``Trajectory``
    carries ``task_id`` + ``trial_index``, but the combined string is what
    appears in logs, retries, and queue records; expose it here so consumers
    don't have to recompose it."""

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


__all__ = ["TrialSpec", "TrialResult"]
