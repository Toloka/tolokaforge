"""``PerTrialRuntimeBackend`` — thin preset over :class:`SharedStackRuntimeBackend`.

Per-trial substrate is a plan-shape of :class:`SharedStackRuntimeBackend`
configured for trial-scope-only plans: no run-scope stacks materialise at
:meth:`connect`, each :meth:`provision` brings up the trial's compose
project via the composer, and per-trial RPCs route through a trial-owned
runner client with deferred connect.

This class delegates every :class:`~tolokaforge.core.runtime.RuntimeBackend`
method to an internal :class:`SharedStackRuntimeBackend` constructed with
``env_manifest=None`` and its :attr:`_per_trial_mode` flag set. The
:class:`~tolokaforge.core.composition_runtime.SubstrateComposer` seam
owns docker-compose materialisation, reset-recipe cycling, and log-router
attachment — see :mod:`~tolokaforge.core.default_substrate_composer` and
:mod:`~tolokaforge.core.docker_compose_materialiser`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.composition_runtime import ComposedEnvHandle, SubstrateComposer
from tolokaforge.core.models import SeedRef
from tolokaforge.core.models.trajectory import Trajectory
from tolokaforge.core.plugin_registry import load_readiness_probe
from tolokaforge.core.run_display_events import ContainerSnapshot
from tolokaforge.core.runtime import EnvHandle, IsolationMode
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, EnvEndpoints
from tolokaforge.runner.models import TaskDescription
from tolokaforge.tools.registry import ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from tolokaforge.core.grading.bundle import GradeBundleManifest
    from tolokaforge.core.plugin_registry import ReadinessProbeFactory, RuntimeBackendBuildContext
    from tolokaforge.core.trial import TrialSpec


@dataclass
class PerTrialRuntimeBackend:
    """Per-trial :class:`~tolokaforge.core.runtime.RuntimeBackend` preset.

    Delegates every method to an internal
    :class:`SharedStackRuntimeBackend` in per-trial mode. Constructor
    kwargs (:attr:`seeds`, :attr:`log_capture`, :attr:`mount_docker_socket`,
    :attr:`readiness_probe_loader`, :attr:`connect_timeout`,
    :attr:`connect_retry_interval`) flow onto the delegate; the optional
    :attr:`composer` seam lets tests inject a materialiser stub without
    monkeypatching module symbols.
    """

    isolation_mode: ClassVar[IsolationMode] = IsolationMode.PER_TRIAL_STACK
    advertised_capabilities: ClassVar[frozenset[str]] = frozenset(
        {
            "per_trial_stack",
            "reset_recipes:sql_dump",
            "reset_recipes:filesystem_dir",
            "reset_recipes:redis_dump",
            "reset_recipes:bare",
            "network_isolation:no_internet",
            "network_isolation:limited_internet",
        }
    )
    seeds: dict[str, SeedRef] = field(default_factory=dict)
    log_capture: LogCaptureConfig | None = None
    mount_docker_socket: bool = False
    readiness_probe_loader: Callable[[str], ReadinessProbeFactory] = load_readiness_probe
    connect_timeout: float = 30.0
    connect_retry_interval: float = 1.0
    composer: SubstrateComposer | None = None
    _delegate: SharedStackRuntimeBackend = field(init=False)

    def __post_init__(self) -> None:
        self._delegate = SharedStackRuntimeBackend(
            env_manifest=None,
            seeds=self.seeds,
            log_capture=self.log_capture,
            mount_docker_socket=self.mount_docker_socket,
            connect_timeout=self.connect_timeout,
            connect_retry_interval=self.connect_retry_interval,
            composer=self._build_composer(),
        )
        self._delegate._per_trial_mode = True

    def _build_composer(self) -> SubstrateComposer:
        if self.composer is not None:
            return self.composer
        from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer

        return DefaultSubstrateComposer(
            readiness_probe_loader=self.readiness_probe_loader,
            connect_timeout=self.connect_timeout,
        )

    # ---- Run-level lifecycle ----

    def connect(
        self,
        timeout: float | None = None,
        retry_interval: float | None = None,
    ) -> None:
        return self._delegate.connect(timeout=timeout, retry_interval=retry_interval)

    def close(self) -> None:
        return self._delegate.close()

    def health_check(self) -> bool:
        return self._delegate.health_check()

    # ---- Per-trial provisioning (ADR-0010) ----

    def provision(self, spec: TrialSpec) -> EnvHandle:
        return self._delegate.provision(spec)

    def await_ready(self, handle: EnvHandle) -> None:
        return self._delegate.await_ready(handle)

    def endpoints(self, handle: EnvHandle) -> EnvEndpoints:
        return self._delegate.endpoints(handle)

    def teardown(self, handle: EnvHandle) -> None:
        return self._delegate.teardown(handle)

    def capture_service_logs(self, handle: EnvHandle, *, capture_worthy: bool) -> dict[str, int]:
        return self._delegate.capture_service_logs(handle, capture_worthy=capture_worthy)

    def get_infrastructure_snapshot(self, handle: EnvHandle) -> list[ContainerSnapshot]:
        return self._delegate.get_infrastructure_snapshot(handle)

    # ---- Per-trial RPC operations (ADR-0013) ----

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict[str, Any]:
        return self._delegate.register_trial(
            trial_id=trial_id,
            trial_spec_json=trial_spec_json,
            default_tool_timeout_s=default_tool_timeout_s,
        )

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        return self._delegate.execute_tool(
            trial_id=trial_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=executor,
            call_id=call_id,
        )

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._delegate.grade_trial(
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
            termination_reason=termination_reason,
        )

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._delegate.get_state(
            trial_id=trial_id,
            include_unstable=include_unstable,
            tables=tables,
        )

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict[str, Any]:
        return self._delegate.reset_trial(
            trial_id=trial_id,
            execute_init_actions=execute_init_actions,
        )

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        return self._delegate.cleanup_trial(trial_id)

    def remember_trial_inputs(
        self,
        trial_id: str,
        trajectory: Trajectory,
        task_description: TaskDescription,
    ) -> None:
        """Stash trial inputs the orchestrator will pass to ``build_grade_bundle``.

        Snapshot-mode producer seam (see ``RuntimeBackend`` Protocol). Cleared
        by :meth:`cleanup_trial` so per-run memory stays bounded.
        """
        self._delegate.remember_trial_inputs(trial_id, trajectory, task_description)

    def build_grade_bundle(
        self,
        trial_id: str,
        *,
        out_dir: Path,
    ) -> GradeBundleManifest:
        """Produce a grade bundle for ``trial_id`` under ``out_dir``.

        Delegates to the shared-stack impl which composes reads via
        :class:`LiveRunnerCallbackGradingSubstrate`.
        """
        return self._delegate.build_grade_bundle(trial_id, out_dir=out_dir)


def per_trial_runtime_backend_factory(
    ctx: RuntimeBackendBuildContext,
) -> PerTrialRuntimeBackend:
    """Build a :class:`PerTrialRuntimeBackend` from a build context."""
    return PerTrialRuntimeBackend(
        seeds=ctx.seeds,
        log_capture=ctx.log_capture,
        mount_docker_socket=ctx.mount_docker_socket,
        connect_timeout=ctx.connect_timeout_s,
        connect_retry_interval=ctx.connect_retry_interval_s,
    )


__all__ = ["ComposedEnvHandle", "PerTrialRuntimeBackend", "per_trial_runtime_backend_factory"]
