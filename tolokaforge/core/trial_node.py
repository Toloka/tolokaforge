"""The per-trial execution unit and the standalone entry that runs one trial.

:func:`build_trial_execution_unit` composes everything one trial needs from a
frozen :class:`TrialExecutionContext`, so any caller that holds the same
dependencies — the orchestrator or a standalone entry — builds the same
thing. The unit owns the grader it resolves and releases it on :meth:`close`.

:func:`run_trial_from_inputs` is the standalone entry: it rebuilds those
dependencies from serialized :class:`~tolokaforge.core.trial.TrialNodeInputs`
and a mounted task pack, runs one trial to a graded
:class:`~tolokaforge.core.trial.TrialResult`, and tears down — with no
orchestrator anywhere in its call graph.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tolokaforge.adapters import get_adapter
from tolokaforge.core.assets import compute_seed_digest
from tolokaforge.core.compose_materialisation import (
    LogCaptureConfig,
    run_needs_docker_cli,
)
from tolokaforge.core.conductor import (
    Conductor,
    ConductorContext,
    ConductorFactory,
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
    TimeoutConfig,
    require_user_simulator_config,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter, InMemoryArtifactWriter
from tolokaforge.core.plugin_registry import (
    RuntimeBackendBuildContext,
    TrialGraderContext,
    load_conductor,
    load_runtime_backend,
    load_trial_grader,
)
from tolokaforge.core.run_display_events import _NULL_EVENTS, RunDisplayEvents
from tolokaforge.core.trial import TrialNodeInputs, TrialResult, TrialSpec
from tolokaforge.core.trial_executor import (
    ProvisionErrorPolicy,
    ProvisioningTrialExecutor,
)
from tolokaforge.core.trial_grader import TrialGrader
from tolokaforge.observability.observer import NullTrialObserver, TrialObserver
from tolokaforge.secrets import (
    CONTAINER_SECRETS_ENV_VAR,
    SecretManager,
    init_default_from,
)

if TYPE_CHECKING:
    from tolokaforge.adapters import BaseAdapter
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import TaskConfig
    from tolokaforge.core.models.run_config import GraderConfig
    from tolokaforge.core.output.artifacts import TrialArtifactWriter
    from tolokaforge.core.rate_limiter import GlobalRateLimiter
    from tolokaforge.core.runtime import RuntimeBackend
    from tolokaforge.observability.factory import RunIdentity

__all__ = [
    "TaskPackDriftError",
    "TrialExecutionContext",
    "TrialExecutionUnit",
    "build_single_task_adapter",
    "build_trial_execution_unit",
    "compute_task_pack_hash",
    "run_trial_from_inputs",
]

_PRELOADED_TASKS_GLOB = "**/task.yaml"


@dataclass(frozen=True)
class TrialExecutionContext:
    """The per-run dependency set the per-trial composition reads.

    ``conductor_factory`` overrides the default ``in_process`` conductor;
    ``grader_config`` is the run's ``grader`` block, which picks the trial
    grader by name (the adapter's default applies otherwise). ``probe_source``
    names the caller in a rate-limit-probe refusal.
    """

    adapter: BaseAdapter
    artifact_writer: TrialArtifactWriter
    config: RunConfig
    logger: StructuredLogger
    agent_client: LLMClient
    runtime_backend: RuntimeBackend
    output_dir: Path
    request_limiter: GlobalRateLimiter | None
    grader_config: GraderConfig | None
    grpc_max_message_mb: int
    verbose: bool = False
    strict: bool = False
    conductor_factory: ConductorFactory | None = None
    events: RunDisplayEvents = _NULL_EVENTS
    trial_observer: TrialObserver = field(default_factory=NullTrialObserver)
    run_identity: RunIdentity | None = None
    provision_error: ProvisionErrorPolicy = "synthesize"
    probe_source: str = "orchestrator"


def _close_grader(grader: TrialGrader) -> None:
    """Release a grader's transport. A grader with no ``close()`` has nothing to release."""
    close = getattr(grader, "close", None)
    if callable(close):
        close()


class TrialExecutionUnit:
    """A built grader, conductor and provisioning executor, ready to run trials."""

    def __init__(
        self,
        *,
        grader: TrialGrader,
        conductor: Conductor,
        provisioning_executor: ProvisioningTrialExecutor,
    ) -> None:
        self.grader = grader
        self.conductor = conductor
        self.provisioning_executor = provisioning_executor

    def execute(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        """Provision the trial's substrate, run the conductor, tear down."""
        return self.provisioning_executor.execute(spec, task_config)

    def close(self) -> None:
        """Release the grader's transport (gRPC channel, queue broker and workers).

        A grader registered downstream without a ``close()`` has nothing to
        release.
        """
        _close_grader(self.grader)


def build_trial_execution_unit(ctx: TrialExecutionContext) -> TrialExecutionUnit:
    """Compose the per-trial execution unit for one run.

    Resolves the trial grader (``grader_config.name`` over the adapter's
    default), builds the conductor through ``conductor_factory`` or the
    ``in_process`` registry entry, and wraps it in a
    :class:`ProvisioningTrialExecutor`. The caller owns the returned unit and
    must :meth:`~TrialExecutionUnit.close` it.

    Rate-limit probe mode's two halves meet here: the caller arms the agent
    client, while the simulator probe, the per-task budget re-check and the
    telemetry accumulator belong to the conductor. A conductor that does not
    declare support raises, because the alternative is a run that really
    absorbs 429s while its artifacts read all-default — see
    :func:`~tolokaforge.core.conductor.require_rate_limit_probe_support`.
    A failed build closes the grader it already resolved.
    """
    # ``None`` when the backend has no runner surface (in-memory / test, or
    # ``PerTrialRuntimeBackend`` — each trial owns its own runner endpoint so
    # no single address applies); the built-in address-needing factories
    # (``grader_rpc``, ``queue``) raise loudly when they observe it.
    runner_address = getattr(ctx.runtime_backend, "runner_address", None)
    grader_config = ctx.grader_config
    grader_name = (
        grader_config.name if grader_config and grader_config.name else None
    ) or ctx.adapter.trial_grader_name
    # In-process routing shim: only populated when the backend has no static
    # runner endpoint (``PerTrialRuntimeBackend``). Shared-stack keeps its
    # address-only grader client.
    in_process_backend_shim = ctx.runtime_backend if runner_address is None else None
    grader = load_trial_grader(grader_name)(
        TrialGraderContext(
            runner_address=runner_address,
            logger=ctx.logger,
            grader_config=grader_config,
            runtime_backend=in_process_backend_shim,
            grpc_max_message_mb=ctx.grpc_max_message_mb,
        )
    )
    try:
        conductor_ctx = ConductorContext(
            adapter=ctx.adapter,
            artifact_writer=ctx.artifact_writer,
            config=ctx.config,
            logger=ctx.logger,
            verbose=ctx.verbose,
            strict=ctx.strict,
            agent_client=ctx.agent_client,
            runtime_backend=ctx.runtime_backend,
            trial_grader=grader,
            output_dir=ctx.output_dir,
            request_limiter=ctx.request_limiter,
            events=ctx.events,
            trial_observer=ctx.trial_observer,
            run_identity=ctx.run_identity,
        )
        factory = ctx.conductor_factory or load_conductor("in_process")
        conductor = factory(conductor_ctx)
        require_rate_limit_probe_support(
            conductor,
            ctx.config.orchestrator.rate_limit_probe,
            source=ctx.probe_source,
        )
        provisioning_executor = ProvisioningTrialExecutor(
            runtime_backend=ctx.runtime_backend,
            conductor=conductor,
            logger=ctx.logger,
            output_dir=ctx.output_dir,
            artifact_writer=ctx.artifact_writer,
            events=ctx.events,
            provision_error=ctx.provision_error,
        )
    except BaseException:
        _close_grader(grader)
        raise
    return TrialExecutionUnit(
        grader=grader,
        conductor=conductor,
        provisioning_executor=provisioning_executor,
    )


def compute_task_pack_hash(pack_dir: Path) -> str:
    """Content hash (``sha256:<hex>``) of the task pack rooted at *pack_dir*.

    Folds every regular file's relative path and content, so any edit, rename
    or addition flips it. :func:`run_trial_from_inputs` compares it against the
    hash its inputs were produced with.
    """
    return compute_seed_digest(Path(pack_dir))


def build_single_task_adapter(task: TaskConfig, task_dir: Path | None = None) -> BaseAdapter:
    """Build an adapter that resolves ``task.task_id`` to this pre-loaded config.

    *task_dir* is the directory holding the task's own files; it defaults to
    the task's ``source_dir`` and then the current directory, which is where a
    task with no file assets resolves from. The adapter must implement
    :meth:`~tolokaforge.adapters.BaseAdapter.register_preloaded_task`, or this
    raises.
    """
    adapter = get_adapter(task.adapter_type, {"tasks_glob": _PRELOADED_TASKS_GLOB})
    adapter.register_preloaded_task(task, task_dir or task.source_dir or Path())
    return adapter


def run_trial_from_inputs(inputs: TrialNodeInputs) -> TrialResult:
    """Run one trial to a graded :class:`TrialResult` from serialized *inputs*.

    The standalone trial entry: no orchestrator, run queue or batch lifecycle
    is involved. It verifies the mounted task pack against
    ``inputs.task_pack_hash``, installs the process secret manager from the
    ``TOLOKAFORGE_SECRETS_JSON`` payload when one is present, rebuilds the
    adapter, LLM client, runtime backend and trial-execution unit, owns the
    backend's ``connect()`` / ``close()``, and runs the trial. A provisioning
    failure propagates as :class:`~tolokaforge.core.runtime.ProvisionError`
    rather than becoming a synthesised result.

    One trial per process: the secret manager is process-global, so a node
    runs a single trial and exits.

    Raises:
        TaskPackDriftError: the mounted pack does not hash to ``task_pack_hash``.
        UnknownImplementationError: ``runtime`` / grader / ``conductor`` names no
            registered implementation.
        ValueError: no user-simulator model is configured, the secret payload is
            malformed, or the rate-limit probe cannot fit its budgets or the
            conductor does not support it.
        NotImplementedError: the task's adapter is not node-runnable.
        ProvisionError: the substrate failed to provision.
    """
    task = inputs.task
    spec = inputs.trial_spec
    _verify_task_pack(inputs)
    _install_container_secrets()

    logger = get_logger(spec.run_id)
    adapter = build_single_task_adapter(task, inputs.task_pack_dir)
    manifest = spec.task.environment_manifest
    runner_address = os.environ.get("EXECUTOR_ADDRESS", "executor:50051")
    runtime_backend = load_runtime_backend(_resolve_runtime_name(inputs.runtime, spec))(
        RuntimeBackendBuildContext(
            runner_address=runner_address,
            env_manifest=manifest,
            run_id=spec.run_id,
            seeds={},
            log_capture=_build_log_capture(inputs.output_dir),
            mount_docker_socket=run_needs_docker_cli(task.adapter_type, [task])
            or spec.agent_model_config.harness is not None,
            expose_substrate=inputs.grader_config is not None
            and inputs.grader_config.expose_substrate,
            grpc_max_message_mb=inputs.grpc_max_message_mb,
        )
    )
    # Resolve the conductor factory before validating the user-simulator config
    # so an unknown ``conductor`` name surfaces the registry error rather than
    # the (fail-loud but less actionable) missing-user-model error.
    conductor_factory = load_conductor(inputs.conductor)

    agent_client = LLMClient(spec.agent_model_config, rate_limit_probe=inputs.rate_limit_probe)
    user_config = require_user_simulator_config(spec.user_model_config)
    output_path = inputs.output_dir if inputs.output_dir is not None else Path(spec.run_id)
    artifact_writer = (
        FileArtifactWriter() if inputs.output_dir is not None else InMemoryArtifactWriter()
    )
    unit = build_trial_execution_unit(
        TrialExecutionContext(
            adapter=adapter,
            artifact_writer=artifact_writer,
            config=_build_run_config(
                agent_client.config,
                user_config,
                spec.judge_model_config,
                output_path,
                inputs.rate_limit_probe,
            ),
            logger=logger,
            agent_client=agent_client,
            runtime_backend=runtime_backend,
            output_dir=output_path,
            request_limiter=None,
            grader_config=inputs.grader_config,
            grpc_max_message_mb=inputs.grpc_max_message_mb,
            conductor_factory=conductor_factory,
            provision_error="propagate",
            probe_source=f"run_trial_from_inputs(conductor={inputs.conductor!r})",
        )
    )
    try:
        runtime_backend.connect()
        try:
            return unit.execute(spec, task)
        finally:
            runtime_backend.close()
    finally:
        unit.close()


class TaskPackDriftError(RuntimeError):
    """The task pack mounted on a node is not the one its inputs were produced against."""


def _verify_task_pack(inputs: TrialNodeInputs) -> None:
    """Fail loud when the mounted pack's content hash differs from the inputs'."""
    if inputs.task_pack_dir is None or inputs.task_pack_hash is None:
        return
    actual = compute_task_pack_hash(inputs.task_pack_dir)
    if actual != inputs.task_pack_hash:
        raise TaskPackDriftError(
            f"task pack at {str(inputs.task_pack_dir)!r} hashes to {actual} but the trial "
            f"inputs were produced against {inputs.task_pack_hash}; the mounted pack has "
            "drifted from the one the trial was planned on"
        )


def _install_container_secrets() -> None:
    """Install the process secret manager from the container secret payload.

    The payload is the same ``TOLOKAFORGE_SECRETS_JSON`` mapping the runner
    container bootstraps from. With none set the default manager initialises
    lazily from its configured providers, as for any in-process caller.
    """
    payload = os.environ.get(CONTAINER_SECRETS_ENV_VAR)
    if not payload:
        return
    try:
        secrets = json.loads(payload)
    except json.JSONDecodeError as exc:
        # Structural fields only: ``exc.doc`` is the credential payload.
        raise ValueError(
            f"{CONTAINER_SECRETS_ENV_VAR} is not valid JSON (line {exc.lineno}, "
            f"col {exc.colno}): {exc.msg}"
        ) from None
    if not isinstance(secrets, dict):
        raise ValueError(f"{CONTAINER_SECRETS_ENV_VAR} must be a JSON object")
    init_default_from(SecretManager.from_dict(secrets))


def _resolve_runtime_name(runtime: str, spec: TrialSpec) -> str:
    """Map ``runtime`` to a registry name, intercepting the reserved ``"auto"``.

    ``"auto"`` picks per-trial substrate when the task's manifest requires it,
    otherwise the shared-stack path. Any explicit name passes straight through.
    """
    if runtime != "auto":
        return runtime
    manifest = spec.task.environment_manifest
    if manifest is not None and manifest.requires_per_trial:
        return "per_trial"
    return "shared"


def _build_log_capture(output_dir: Path | None) -> LogCaptureConfig | None:
    """Build the log-capture policy, or ``None`` when writing no disk artifacts."""
    if output_dir is None:
        return None
    compute = ComputeConfig()
    return LogCaptureConfig(
        output_root=output_dir,
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
    inputs' ``runtime``, not the deprecated override field.
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
