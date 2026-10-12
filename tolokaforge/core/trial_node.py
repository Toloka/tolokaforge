"""The per-trial execution unit: grader, conductor and provisioning executor.

:func:`build_trial_execution_unit` composes everything one trial needs from a
frozen :class:`TrialExecutionContext`, so any caller that holds the same
dependencies — the orchestrator or a standalone entry — builds the same
thing. The unit owns the grader it resolves and releases it on :meth:`close`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tolokaforge.core.conductor import (
    Conductor,
    ConductorContext,
    ConductorFactory,
    require_rate_limit_probe_support,
)
from tolokaforge.core.plugin_registry import (
    TrialGraderContext,
    load_conductor,
    load_trial_grader,
)
from tolokaforge.core.run_display_events import _NULL_EVENTS, RunDisplayEvents
from tolokaforge.core.trial import TrialResult, TrialSpec
from tolokaforge.core.trial_executor import (
    ProvisionErrorPolicy,
    ProvisioningTrialExecutor,
)
from tolokaforge.core.trial_grader import TrialGrader
from tolokaforge.observability.observer import NullTrialObserver, TrialObserver

if TYPE_CHECKING:
    from pathlib import Path

    from tolokaforge.adapters import BaseAdapter
    from tolokaforge.core.llm import LLMClient
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import RunConfig, TaskConfig
    from tolokaforge.core.models.run_config import GraderConfig
    from tolokaforge.core.output.artifacts import TrialArtifactWriter
    from tolokaforge.core.rate_limiter import GlobalRateLimiter
    from tolokaforge.core.runtime import RuntimeBackend
    from tolokaforge.observability.factory import RunIdentity

__all__ = [
    "TrialExecutionContext",
    "TrialExecutionUnit",
    "build_trial_execution_unit",
]


@dataclass(frozen=True)
class TrialExecutionContext:
    """The per-run dependency set the per-trial composition reads.

    ``conductor_factory`` overrides the default ``in_process`` conductor;
    ``grader_config`` is the run's ``grader`` block, which picks the trial
    grader by name (the adapter's default applies otherwise).
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
            source="orchestrator",
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
