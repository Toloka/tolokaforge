"""Top-level single-trial library entry point (ADR-0022 § Surface 2).

Composes the minimum single-trial wiring the :class:`~tolokaforge.core.orchestrator.Orchestrator`
composes today — LLM client, adapter, runtime backend, conductor, trial
grader — and returns a typed :class:`~tolokaforge.core.trial.TrialResult`,
without the caller reconstructing the orchestrator or its batch lifecycle
(queue, run-state, worker pool, budget, resume). The runtime backend, grader,
and conductor are resolved by registered name through the entry-point
registries in :mod:`tolokaforge.core.plugin_registry`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tolokaforge.adapters import get_adapter
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.conductor import Conductor, ConductorContext
from tolokaforge.core.llm import LLMClient
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    RATE_LIMIT_PROBE_MIN_EPISODE_S,
    ComputeConfig,
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RateLimitProbeConfig,
    RunConfig,
    TaskConfig,
    TimeoutConfig,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter, InMemoryArtifactWriter
from tolokaforge.core.plugin_registry import (
    RuntimeBackendBuildContext,
    TrialGraderContext,
    load_conductor,
    load_runtime_backend,
    load_trial_grader,
)
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.shared_stack_runtime import _build_env_endpoints
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, TrialResult, TrialSpec
from tolokaforge.runner.models import TaskDescription

_RUN_ID = "run_trial"
_PRELOADED_TASKS_GLOB = "**/task.yaml"

# Mirrors the orchestrator's default user model (orchestrator.py) so a
# library call that omits ``user`` drives the same simulator as a batch run.
_DEFAULT_USER_MODEL = ModelConfig(
    provider="openrouter",
    name="anthropic/claude-sonnet-4.6",
    temperature=0.2,
)


class _RunTrialModels(BaseModel):
    """Coercion gate for the ``models`` argument.

    A required ``agent`` and an ``extra="forbid"`` policy make both a missing
    ``agent`` key and any malformed / unexpected role value raise a Pydantic
    ``ValidationError`` (the ADR-named error type) before any registry or
    backend work.
    """

    model_config = {"extra": "forbid"}

    agent: ModelConfig
    user: ModelConfig | None = None
    judge: ModelConfig | None = None


def run_trial(
    *,
    task: TaskConfig,
    models: dict[str, ModelConfig | dict[str, Any]],
    runtime: str = "auto",
    grader: str = "runner_rpc",
    conductor: str = "in_process",
    output_dir: Path | str | None = None,
    trial_index: int = 0,
    rate_limit_probe: RateLimitProbeConfig | None = None,
) -> TrialResult:
    """Run one trial in-process and return its :class:`TrialResult`.

    Args:
        task: The task to run. When obtained through the adapter/loader it
            carries its source directory (``TaskConfig.source_dir``) so file
            assets resolve without a filesystem glob; a hand-built config
            resolves assets against the current directory and fails loud the
            moment a missing file is required.
        models: Role → model map. ``agent`` is required; ``user`` and
            ``judge`` are optional. Values may be :class:`ModelConfig`
            instances or plain dicts.
        runtime: Registered runtime-backend name, or ``"auto"`` (default) to
            pick ``"per_trial"`` when the task's manifest requires per-trial
            isolation, else ``"shared"``.
        grader: Registered trial-grader name.
        conductor: Registered conductor name.
        output_dir: When ``None`` (default) no artifacts touch disk; otherwise
            artifacts are written under this directory.
        trial_index: Trial index within the task; forms the ``"{task_id}:{n}"``
            trial id.
        rate_limit_probe: Rate-limit probe mode for the agent and simulator
            clients. ``None`` (default) runs the standard bounded-exponential
            retry path. Enabling it raises the composed run config's episode
            budget to twice the probe's per-call budget (a probe absorbs 429s
            by sleeping, and episode wall-time counts that sleep), which is
            what keeps the ``per_call_budget_s < episode_s`` invariant true
            for this composed-config surface.

    Returns:
        The trial's :class:`TrialResult`.

    Raises:
        pydantic.ValidationError: ``models`` is missing ``agent`` or malformed,
            or the composed run config is invalid.
        ValueError: ``rate_limit_probe`` is enabled with budgets that cannot
            fit inside the episode budget.
        UnknownImplementationError: ``runtime`` / ``grader`` / ``conductor``
            names no registered implementation.
        ProvisionError: The substrate failed to provision (propagated, not
            swallowed — the single-trial contract diverges from the batch
            executor's synthesise-and-continue behaviour).
    """
    resolved_models = _RunTrialModels.model_validate(models)

    logger = get_logger(_RUN_ID)
    adapter = _build_single_task_adapter(task)
    task_desc = adapter.to_task_description(task.task_id)

    runner_address = os.environ.get("EXECUTOR_ADDRESS", "executor:50051")
    log_capture = _build_log_capture(output_dir)
    runtime_backend = load_runtime_backend(_resolve_runtime_name(runtime, task_desc))(
        RuntimeBackendBuildContext(
            runner_address=runner_address,
            env_manifest=task_desc.environment_manifest,
            run_id=_RUN_ID,
            seeds={},
            log_capture=log_capture,
        )
    )
    trial_grader = load_trial_grader(grader)(
        TrialGraderContext(runtime_backend=runtime_backend, logger=logger)
    )

    agent_client = LLMClient(resolved_models.agent, rate_limit_probe=rate_limit_probe)
    user_config = resolved_models.user or _DEFAULT_USER_MODEL
    judge_config = resolved_models.judge

    output_path = Path(output_dir) if output_dir is not None else Path(_RUN_ID)
    artifact_writer = FileArtifactWriter() if output_dir is not None else InMemoryArtifactWriter()
    config = _build_run_config(
        resolved_models.agent,
        user_config,
        judge_config,
        output_path,
        rate_limit_probe,
    )

    conductor_impl = load_conductor(conductor)(
        ConductorContext(
            adapter=adapter,
            artifact_writer=artifact_writer,
            config=config,
            logger=logger,
            verbose=False,
            strict=False,
            agent_client=agent_client,
            runtime_backend=runtime_backend,
            trial_grader=trial_grader,
            output_dir=output_path,
            request_limiter=None,
        )
    )

    spec = TrialSpec(
        trial_id=f"{task.task_id}:{trial_index}",
        run_id=_RUN_ID,
        task=task_desc,
        agent_model_config=agent_client.config,
        user_model_config=user_config,
        judge_model_config=judge_config,
        max_turns=task.max_turns,
        default_tool_timeout_s=DEFAULT_TOOL_TIMEOUT_S,
        env_endpoints=_build_env_endpoints(runner_address),
    )

    runtime_backend.connect()
    try:
        return _execute_trial(runtime_backend, conductor_impl, spec, task)
    finally:
        runtime_backend.close()


def _execute_trial(
    runtime_backend: RuntimeBackend,
    conductor: Conductor,
    spec: TrialSpec,
    task: TaskConfig,
) -> TrialResult:
    """Provision → run → teardown for a single trial.

    Deliberately does not reuse ``ProvisioningTrialExecutor.execute``: that path
    swallows ``ProvisionError`` into a synthesised failure result (correct for a
    batch run), whereas the library contract is to let it propagate.
    """
    handle = runtime_backend.provision(spec)
    try:
        runtime_backend.await_ready(handle)
        real_endpoints = runtime_backend.endpoints(handle)
        final_spec = spec.model_copy(update={"env_endpoints": real_endpoints})
        return conductor.run(final_spec, task)
    finally:
        runtime_backend.teardown(handle)


def _build_single_task_adapter(task: TaskConfig) -> BaseAdapter:
    """Build an adapter that resolves ``task.task_id`` to this pre-loaded config."""
    adapter = get_adapter(task.adapter_type, {"tasks_glob": _PRELOADED_TASKS_GLOB})
    register = getattr(adapter, "register_preloaded_task", None)
    if register is None:
        raise TypeError(
            f"adapter_type {task.adapter_type!r} does not support single-task "
            "pre-loading; run_trial requires an adapter with register_preloaded_task."
        )
    register(task, task.source_dir or Path())
    return adapter


def _resolve_runtime_name(runtime: str, task_desc: TaskDescription) -> str:
    """Map ``runtime`` to a registry name, intercepting the reserved ``"auto"``.

    ``"auto"`` mirrors the orchestrator's ``_select_backend_from_tasks``: a task
    whose resolved manifest requires per-trial isolation picks ``"per_trial"``,
    otherwise ``"shared"``. Any explicit name passes straight through.
    """
    if runtime != "auto":
        return runtime
    manifest = task_desc.environment_manifest
    if manifest is not None and manifest.requires_per_trial:
        return "per_trial"
    return "shared"


def _build_log_capture(output_dir: Path | str | None) -> LogCaptureConfig | None:
    """Build the log-capture policy, or ``None`` when writing no disk artifacts."""
    if output_dir is None:
        return None
    compute = ComputeConfig()
    return LogCaptureConfig(
        output_root=Path(output_dir),
        tail=compute.log_tail,
        on_success=compute.capture_logs_on_success,
    )


def _build_run_config(
    agent: ModelConfig,
    user: ModelConfig,
    judge: ModelConfig | None,
    output_path: Path,
    rate_limit_probe: RateLimitProbeConfig | None,
) -> RunConfig:
    """Compose the minimal :class:`RunConfig` the conductor reads.

    ``orchestrator.runtime`` is left unset — backend selection flows through the
    ``runtime`` argument, not the deprecated override field.
    """
    models: dict[str, ModelConfig] = {"agent": agent, "user": user}
    if judge is not None:
        models["judge"] = judge
    orchestrator_kwargs: dict[str, Any] = {
        "workers": 1,
        "repeats": 1,
        "auto_start_services": False,
    }
    if rate_limit_probe is not None and rate_limit_probe.enabled:
        orchestrator_kwargs["rate_limit_probe"] = rate_limit_probe
        orchestrator_kwargs["timeouts"] = TimeoutConfig(
            episode_s=_probe_episode_s(rate_limit_probe)
        )
    return RunConfig(
        models=models,
        # OrchestratorConfig validates the probe budget against this episode
        # budget on construction, so an unfittable combination fails here.
        orchestrator=OrchestratorConfig(**orchestrator_kwargs),
        evaluation=EvaluationConfig(output_dir=str(output_path)),
    )


def _probe_episode_s(probe: RateLimitProbeConfig) -> int:
    """Episode budget wide enough for *probe*'s per-call budget to fit inside.

    Twice the per-call budget, floored at the mode's minimum, so a single
    blocked call can spend its whole budget and the trial still has room for
    the rest of the episode.
    """
    return max(
        int(probe.per_call_budget_s * 2),
        RATE_LIMIT_PROBE_MIN_EPISODE_S + 1,
    )
