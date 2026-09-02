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
from tolokaforge.core.conductor import (
    Conductor,
    ConductorContext,
    require_rate_limit_probe_support,
)
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
    require_user_simulator_config,
)
from tolokaforge.core.orchestrator import _tasks_use_compose_variant_tools
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
            budget to twice the probe's per-*turn* 429 wall ceiling (a probe
            absorbs 429s by sleeping, and episode wall-time counts that sleep),
            which is what keeps the budget invariant true by construction for
            this composed-config surface. ``conductor`` must name an
            implementation that supports the mode.

    Returns:
        The trial's :class:`TrialResult`.

    Raises:
        pydantic.ValidationError: ``models`` is missing ``agent`` or malformed,
            or the composed run config is invalid.
        ValueError: ``rate_limit_probe`` is enabled with budgets that cannot
            fit inside the episode budget, or with a ``conductor`` that does not
            support the mode.
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
            mount_docker_socket=_tasks_use_compose_variant_tools([task]),
        )
    )
    trial_grader = load_trial_grader(grader)(
        TrialGraderContext(runner_address=runner_address, logger=logger)
    )
    # Resolve the conductor factory before validating the user-simulator config
    # so an unknown ``conductor`` name surfaces the registry error rather than
    # the (fail-loud but less actionable) missing-user-model error.
    conductor_factory = load_conductor(conductor)

    agent_client = LLMClient(resolved_models.agent, rate_limit_probe=rate_limit_probe)
    user_config = require_user_simulator_config(resolved_models.user)
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

    conductor_impl = conductor_factory(
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
    # ``conductor`` names any registered implementation, and the agent client
    # above is already armed — the same split the orchestrator guards.
    require_rate_limit_probe_support(
        conductor_impl,
        config.orchestrator.rate_limit_probe,
        source=f"run_trial(conductor={conductor!r})",
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

    ``"auto"`` picks per-trial substrate when the task's manifest requires it,
    otherwise the shared-stack path. This mirrors the single-trial subprocess
    seam's contract; the orchestrator's own selection is composer-driven and
    routes both paths through :class:`SharedStackRuntimeBackend`. Any explicit
    name passes straight through.
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
    """Episode budget wide enough for *probe*'s per-turn 429 handling to fit inside.

    Twice ``turn_wall_ceiling_s`` — the agent's per-call budget plus one per
    user-reply attempt, which one turn spends back to back, plus the overshoot
    each of those calls can add — floored at the mode's minimum. Doubling the
    *ceiling* rather than the bare budget is what makes
    :func:`validate_rate_limit_probe_budget` pass by construction for any legal
    block, including one with a large ``retry_interval_s``.
    """
    return max(
        int(probe.turn_wall_ceiling_s * 2) + 1,
        RATE_LIMIT_PROBE_MIN_EPISODE_S + 1,
    )
