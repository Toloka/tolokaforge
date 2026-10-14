"""TrialSpec and TrialResult — the typed ``RegisterTrial`` wire format.

``EnvironmentManifest`` and its supporting models live alongside
``TaskDescription`` in :mod:`tolokaforge.runner.models`; they are re-exported
here for callers that work against the trial-spec surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from tolokaforge.core.models import (
    GraderConfig,
    ModelConfig,
    RateLimitProbeConfig,
    TaskConfig,
    Trajectory,
)
from tolokaforge.runner.models import (
    EnvironmentManifest,
    InitialStateKind,
    InitialStateRef,
    NetworkPolicy,
    SecurityContext,
    TaskDescription,
    TaskIsolation,
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

    db_url: str | None = None
    """URL of the DB service the runner's tool layer calls. ``None``
    when the run's substrate does not publish a discoverable ``db-service``
    endpoint on port 8000 — in env_manifest mode the runner-side
    ``DBServiceClient`` binds to ``DB_SERVICE_URL`` from its container
    environment (set in the task's compose file), and the JSON-DB
    builtins reach db-service through that client. Built-in-stack mode
    always populates this field."""

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


class TrialNodeInputs(BaseModel):
    """Everything a standalone node needs to run one trial to a graded result.

    Pure data: it names no live dependency and no remote address. A node
    rebuilds the adapter, LLM clients, runtime backend and grader from it plus
    the task pack it has mounted. ``task`` is the same full :class:`TaskConfig`
    the ``run-trial`` subprocess wire carries, so there is one task
    serialization; the pack is read only for file assets and for the content
    check below. Credentials never travel here: model configs carry
    ``${secret:NAME}`` references, resolved from the node's own secret payload.

    :attr:`task_pack_dir` and :attr:`output_dir` are node-local filesystem
    placement the node supplies, not portable data — a caller that moves these
    inputs between hosts sets them to the receiving node's mount and output paths.
    """

    task: TaskConfig
    """The full task configuration (the ``run-trial`` wire's ``task``)."""

    trial_spec: TrialSpec
    """The trial to run; its ``task`` projection and model configs drive the node."""

    grader_config: GraderConfig | None = None
    """The run's ``grader`` block; ``None`` defers to the adapter's default grader."""

    grpc_max_message_mb: int = Field(gt=0)
    """Bound on every gRPC channel the node opens."""

    runtime: str
    """Registered runtime-backend name, or ``"auto"``."""

    conductor: str
    """Registered conductor name."""

    task_pack_dir: Path | None = None
    """Where the node mounted the task pack: the directory holding the task's own
    files. ``None`` for a task that references no file assets."""

    task_pack_hash: str | None = None
    """Content hash of the pack the inputs were produced against; the node
    refuses to run when its mounted pack hashes differently. Set exactly when
    :attr:`task_pack_dir` is."""

    output_dir: Path | None = None
    """Where the node writes trial artifacts; ``None`` writes none."""

    rate_limit_probe: RateLimitProbeConfig | None = None
    """Rate-limit probe mode for the agent and simulator clients; ``None`` runs the
    standard retry path."""

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _rebase_compose_paths(cls, data: Any, info: ValidationInfo) -> Any:
        """Point every ``compose_file`` at the node's own copy of the task pack.

        A task-declared stack's compose path is absolute on the control plane,
        and the manifest validators require the file to exist while the inputs
        are deserialised. When the validation context carries ``task_pack_root``
        (the node's mount of the task packs), each ``compose_file`` under
        ``task_pack_dir`` is rewritten to the same relative location under
        ``<task_pack_root>/<task_id>``. Without that context the data is
        returned untouched. A ``compose_file`` outside ``task_pack_dir``, or a
        compose file with no pack hash to verify it against, is refused.
        """
        context = info.context
        if not context or context.get("task_pack_root") is None or not isinstance(data, dict):
            return data
        pack_dir = data.get("task_pack_dir")
        task = data.get("task")
        if not isinstance(task, dict) or "task_id" not in task:
            return data
        rebase = _ComposePathRebase(
            control_pack_dir=Path(pack_dir).resolve() if pack_dir is not None else None,
            node_pack_dir=Path(context["task_pack_root"]) / task["task_id"],
            has_pack_hash=data.get("task_pack_hash") is not None,
        )
        return rebase.apply(data)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if (self.task_pack_dir is None) != (self.task_pack_hash is None):
            raise ValueError(
                "task_pack_dir and task_pack_hash are set together: a pack with no hash "
                "cannot be verified, and a hash with no pack has nothing to verify"
            )
        if self.trial_spec.task.task_id != self.task.task_id:
            raise ValueError(
                f"trial_spec.task is {self.trial_spec.task.task_id!r} but task is "
                f"{self.task.task_id!r}; the two describe different tasks"
            )
        return self


@dataclass(frozen=True)
class _ComposePathRebase:
    """Rewrites every ``compose_file`` value of raw inputs onto the node's pack mount."""

    control_pack_dir: Path | None
    node_pack_dir: Path
    has_pack_hash: bool

    def apply(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: self._rebase(value) if key == "compose_file" else self.apply(value)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [self.apply(item) for item in node]
        return node

    def _rebase(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if not self.has_pack_hash or self.control_pack_dir is None:
            raise ValueError(
                f"compose_file {value!r} is declared but the inputs carry no task_pack_dir / "
                "task_pack_hash: the compose file cannot be located or verified on the node"
            )
        declared = Path(value)
        if not declared.is_absolute():
            declared = self.control_pack_dir / declared
        try:
            relative = declared.resolve().relative_to(self.control_pack_dir)
        except ValueError:
            raise ValueError(
                f"compose_file {value!r} is outside the task pack {str(self.control_pack_dir)!r}; "
                "only a compose file inside the task's own directory ships with the pack"
            ) from None
        return str(self.node_pack_dir / relative)


__all__ = [
    "DEFAULT_TOOL_TIMEOUT_S",
    "EnvEndpoints",
    "EnvironmentManifest",
    "InitialStateKind",
    "InitialStateRef",
    "NetworkPolicy",
    "SecurityContext",
    "TaskIsolation",
    "TrialNodeInputs",
    "TrialResult",
    "TrialSpec",
]
