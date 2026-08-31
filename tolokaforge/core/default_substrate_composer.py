"""Built-in :class:`SubstrateComposer` — the composition-plan sequencer.

Walks a :data:`CompositionPlan` in scope order — run-scope stacks at
:meth:`materialise_run`, then per-``(task_id, stack_id)`` task-scope stacks
and trial-scope stacks at :meth:`provision_trial` — and delegates each
stack's materialisation to a :class:`ComposeMaterialiser` and each
service's between-trial cycle to a
:class:`ServiceLifecycleDispatcher`. The composer owns the composition-
plan invariants (INV-12) at :meth:`materialise_run`.

The class stands beside :class:`SharedStackRuntimeBackend` and
:class:`PerTrialRuntimeBackend` — the built-in triple against which
``tests/canonical/test_composition_baseline_parity.py`` locks byte-parity
with the frozen inline-flow baseline fixture for the single-stack shapes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from tolokaforge.core.compose_materialisation import (
    DB_SERVICE_DEFAULT,
    DB_SERVICE_PORT_DEFAULT,
    TOLOKAFORGE_ENV_PREFIX,
    LogCaptureConfig,
    run_services_dir,
    trial_services_dir,
)
from tolokaforge.core.composition_runtime import (
    ComposedEnvHandle,
    ComposeMaterialiser,
    CompositionPlan,
    MaterialiseContext,
    MaterialiseLogCapture,
    RunCtx,
    RunSubstrate,
    ServiceLifecycleDispatcher,
    StackHandle,
    WriteComposeEnv,
)
from tolokaforge.core.docker_compose_materialiser import DockerComposeMaterialiser
from tolokaforge.core.models.task_config import SeedRef
from tolokaforge.core.plugin_registry import ReadinessProbeFactory, load_readiness_probe
from tolokaforge.core.run_display_events import RunDisplayEvents
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.service_lifecycle_dispatchers import DISPATCHER_REGISTRY
from tolokaforge.core.service_readiness import ResolvedEndpoint
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient, RunnerClient
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import ServiceIsolation, ServiceSpec, StackDecl

__all__ = ["DefaultSubstrateComposer"]

logger = logging.getLogger(__name__)


def _default_runner_client_factory(
    runner_address: str,
    events: RunDisplayEvents | None,
) -> RunnerClient:
    """Construct a :class:`GrpcRunnerClient` — the production factory.

    Kept module-level so :attr:`DefaultSubstrateComposer.runner_client_factory`
    can default to a stable reference without a lambda that captures a
    per-instance closure.
    """
    return GrpcRunnerClient(runner_address=runner_address, events=events)


@dataclass
class DefaultSubstrateComposer:
    """Sequences a composition plan across a materialiser and dispatchers.

    Fields are injection seams: production defaults are the built-in
    docker-compose materialiser, the built-in dispatcher registry copied
    at construction, the real :class:`GrpcRunnerClient`, and the
    entry-point readiness-probe loader. Tests substitute fakes at any
    layer without monkeypatching.
    """

    materialiser: ComposeMaterialiser = field(default_factory=DockerComposeMaterialiser)
    dispatcher_registry: dict[ServiceIsolation, ServiceLifecycleDispatcher] = field(
        default_factory=lambda: dict(DISPATCHER_REGISTRY)
    )
    runner_client_factory: Callable[[str, RunDisplayEvents | None], RunnerClient] = (
        _default_runner_client_factory
    )
    readiness_probe_loader: Callable[[str], ReadinessProbeFactory] = load_readiness_probe
    connect_timeout: float = 30.0

    # ------------------------------------------------------------------
    # materialise_run
    # ------------------------------------------------------------------

    def materialise_run(self, plan: CompositionPlan, ctx: RunCtx) -> RunSubstrate:
        _validate_plan(plan)
        run_scope_decls = [decl for decl in plan if decl.stack_scope == "run"]
        manifest = ctx.manifest
        materialised: list[StackHandle] = []
        try:
            for decl in run_scope_decls:
                m_ctx = MaterialiseContext(
                    scope_key=ctx.run_id,
                    stack_id=decl.stack_id,
                    network_policy=manifest.network_policy,
                    limited_internet_allowlist=tuple(manifest.limited_internet_allowlist),
                    restricted_services=manifest.restricted_services,
                    mount_docker_socket=ctx.mount_docker_socket,
                    log_capture=_run_scope_log_capture(ctx.log_capture),
                    write_compose_env=None,
                    events=ctx.events,
                    component_id_prefix="engine",
                )
                handle = self.materialiser.materialise(decl, m_ctx)
                materialised.append(handle)
        except BaseException:
            _teardown_handles_best_effort(self.materialiser, materialised)
            raise

        runner_handle, runner_decl = _find_runner_owner(materialised, run_scope_decls)
        if runner_handle is None:
            return RunSubstrate(
                run_id=ctx.run_id,
                run_stack_handles=tuple(materialised),
                task_stack_handles={},
                runner_client=None,
                endpoints=None,
                seeds=ctx.seeds,
                mount_docker_socket=ctx.mount_docker_socket,
                log_capture=ctx.log_capture,
                events=ctx.events,
            )

        assert runner_decl is not None  # narrowed by runner_handle presence
        runner_endpoint = self.materialiser.resolve_endpoint(
            runner_handle, runner_decl.runner_service or "", manifest.runner_port
        )
        if runner_endpoint is None:
            _teardown_handles_best_effort(self.materialiser, materialised)
            raise ProvisionError(
                trial_id=ctx.run_id,
                stage="materialise_run",
                reason=(
                    f"runner_service {runner_decl.runner_service!r} does not expose port "
                    f"{manifest.runner_port} in stack {runner_decl.stack_id!r}"
                ),
            )
        runner_host, runner_host_port = runner_endpoint
        endpoints = _resolve_env_endpoints(
            self.materialiser, runner_handle, runner_host, runner_host_port, manifest
        )
        runner_client = self.runner_client_factory(f"{runner_host}:{runner_host_port}", ctx.events)
        return RunSubstrate(
            run_id=ctx.run_id,
            run_stack_handles=tuple(materialised),
            task_stack_handles={},
            runner_client=runner_client,
            endpoints=endpoints,
            seeds=ctx.seeds,
            mount_docker_socket=ctx.mount_docker_socket,
            log_capture=ctx.log_capture,
            events=ctx.events,
        )

    # ------------------------------------------------------------------
    # provision_trial
    # ------------------------------------------------------------------

    def provision_trial(
        self,
        plan: CompositionPlan,
        spec: TrialSpec,
        run_sub: RunSubstrate,
    ) -> ComposedEnvHandle:
        manifest = _require_manifest(spec)
        _refuse_reserved_prefix(manifest, spec.trial_id)

        newly: list[tuple[StackDecl, StackHandle]] = []
        try:
            self._materialise_task_stacks(plan, spec, manifest, run_sub, newly)
            trial_handles = self._materialise_trial_stacks(plan, spec, manifest, run_sub, newly)
        except BaseException:
            _teardown_handles_best_effort(self.materialiser, [h for _, h in newly])
            raise

        try:
            self._apply_reset_recipes(manifest, [h for _, h in newly], run_sub.seeds, spec.trial_id)
        except BaseException:
            _teardown_handles_best_effort(self.materialiser, [h for _, h in newly])
            raise

        return self._wire_trial_runner(plan, spec, manifest, trial_handles, newly, run_sub)

    def _materialise_task_stacks(
        self,
        plan: CompositionPlan,
        spec: TrialSpec,
        manifest: EnvironmentManifest,
        run_sub: RunSubstrate,
        newly: list[tuple[StackDecl, StackHandle]],
    ) -> None:
        for decl in [d for d in plan if d.stack_scope == "task"]:
            key = (spec.task.task_id, decl.stack_id)
            if key in run_sub.task_stack_handles:
                continue
            ctx = MaterialiseContext(
                scope_key=spec.task.task_id,
                stack_id=decl.stack_id,
                network_policy=manifest.network_policy,
                limited_internet_allowlist=tuple(manifest.limited_internet_allowlist),
                restricted_services=manifest.restricted_services,
                mount_docker_socket=run_sub.mount_docker_socket,
                log_capture=_trial_scope_log_capture(run_sub.log_capture, spec.trial_id),
                write_compose_env=None,
                events=run_sub.events,
                component_id_prefix=f"task/{spec.task.task_id}",
            )
            handle = self.materialiser.materialise(decl, ctx)
            run_sub.task_stack_handles[key] = handle
            newly.append((decl, handle))

    def _materialise_trial_stacks(
        self,
        plan: CompositionPlan,
        spec: TrialSpec,
        manifest: EnvironmentManifest,
        run_sub: RunSubstrate,
        newly: list[tuple[StackDecl, StackHandle]],
    ) -> list[StackHandle]:
        trial_handles: list[StackHandle] = []
        for decl in [d for d in plan if d.stack_scope == "trial"]:
            ctx = MaterialiseContext(
                scope_key=spec.trial_id,
                stack_id=decl.stack_id,
                network_policy=manifest.network_policy,
                limited_internet_allowlist=tuple(manifest.limited_internet_allowlist),
                restricted_services=manifest.restricted_services,
                mount_docker_socket=run_sub.mount_docker_socket,
                log_capture=_trial_scope_log_capture(run_sub.log_capture, spec.trial_id),
                write_compose_env=WriteComposeEnv(
                    trial_id=spec.trial_id,
                    stack_inputs=manifest.stack_inputs,
                ),
                events=run_sub.events,
                component_id_prefix=f"trial/{spec.trial_id}",
            )
            handle = self.materialiser.materialise(decl, ctx)
            trial_handles.append(handle)
            newly.append((decl, handle))
        return trial_handles

    def _wire_trial_runner(
        self,
        plan: CompositionPlan,
        spec: TrialSpec,
        manifest: EnvironmentManifest,
        trial_handles: list[StackHandle],
        newly: list[tuple[StackDecl, StackHandle]],
        run_sub: RunSubstrate,
    ) -> ComposedEnvHandle:
        runner_handle, runner_decl = _find_runner_owner(
            trial_handles, [d for d in plan if d.stack_scope == "trial"]
        )
        if runner_handle is None:
            return ComposedEnvHandle(
                trial_id=spec.trial_id,
                trial_stack_handles=tuple(trial_handles),
                trial_endpoints=None,
                trial_runner_client=None,
            )
        assert runner_decl is not None
        runner_endpoint = self.materialiser.resolve_endpoint(
            runner_handle, runner_decl.runner_service or "", manifest.runner_port
        )
        if runner_endpoint is None:
            _teardown_handles_best_effort(self.materialiser, [h for _, h in newly])
            raise ProvisionError(
                trial_id=spec.trial_id,
                stage="provision",
                reason=(
                    f"runner_service {runner_decl.runner_service!r} does not expose port "
                    f"{manifest.runner_port} in stack {runner_decl.stack_id!r}"
                ),
            )
        runner_host, runner_host_port = runner_endpoint
        try:
            targets = _readiness_targets(
                manifest,
                self.materialiser,
                runner_handle,
                runner_host,
                runner_host_port,
                spec.trial_id,
            )
            _run_readiness_gate(
                readiness_probe_loader=self.readiness_probe_loader,
                services=targets,
                trial_id=spec.trial_id,
                timeout=self.connect_timeout,
            )
        except ProvisionError:
            _teardown_handles_best_effort(self.materialiser, [h for _, h in newly])
            raise
        endpoints = _resolve_env_endpoints(
            self.materialiser, runner_handle, runner_host, runner_host_port, manifest
        )
        client = self.runner_client_factory(f"{runner_host}:{runner_host_port}", run_sub.events)
        return ComposedEnvHandle(
            trial_id=spec.trial_id,
            trial_stack_handles=tuple(trial_handles),
            trial_endpoints=endpoints,
            trial_runner_client=client,
        )

    # ------------------------------------------------------------------
    # cycle_between_trials
    # ------------------------------------------------------------------

    def cycle_between_trials(self, run_sub: RunSubstrate, spec: TrialSpec) -> None:
        manifest = spec.task.environment_manifest
        if manifest is None:
            return
        stacks: list[StackHandle] = list(run_sub.run_stack_handles)
        for (task_id, _stack_id), handle in run_sub.task_stack_handles.items():
            if task_id == spec.task.task_id:
                stacks.append(handle)
        for stack_handle in stacks:
            for service_name, service_spec in manifest.services.items():
                dispatcher = self.dispatcher_registry.get(service_spec.isolation)
                if dispatcher is None:
                    raise ProvisionError(
                        trial_id=spec.trial_id,
                        stage="cycle",
                        reason=(
                            f"no dispatcher registered for isolation label "
                            f"{service_spec.isolation!r} on stack {stack_handle.stack_id!r} "
                            f"(scope={stack_handle.stack_scope}, service={service_name!r})"
                        ),
                    )
                try:
                    dispatcher.cycle(
                        service_name,
                        service_spec,
                        stack_handle,
                        self.materialiser,
                        seeds=run_sub.seeds,
                    )
                except ProvisionError as exc:
                    raise ProvisionError(
                        trial_id=spec.trial_id,
                        stage=exc.stage,
                        reason=(
                            f"stack {stack_handle.stack_id!r} "
                            f"(scope={stack_handle.stack_scope}) "
                            f"service {service_name!r}: {exc.reason}"
                        ),
                    ) from exc

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------

    def teardown_trial(self, env_handle: ComposedEnvHandle) -> None:
        if env_handle.trial_runner_client is not None:
            try:
                env_handle.trial_runner_client.close()
            except Exception:  # noqa: BLE001 — teardown must not raise past caller
                logger.exception(
                    "DefaultSubstrateComposer.teardown_trial: trial runner client "
                    "close failed for trial %r",
                    env_handle.trial_id,
                )
        _teardown_handles_best_effort(self.materialiser, list(env_handle.trial_stack_handles))

    def teardown_run(self, run_sub: RunSubstrate) -> None:
        if run_sub.runner_client is not None:
            try:
                run_sub.runner_client.close()
            except Exception:  # noqa: BLE001 — teardown must not raise past caller
                logger.exception(
                    "DefaultSubstrateComposer.teardown_run: run runner client close "
                    "failed for run %r",
                    run_sub.run_id,
                )
        _teardown_handles_best_effort(self.materialiser, list(run_sub.task_stack_handles.values()))
        _teardown_handles_best_effort(self.materialiser, list(run_sub.run_stack_handles))

    # ------------------------------------------------------------------
    # runner_client_for / endpoints_for
    # ------------------------------------------------------------------

    def runner_client_for(
        self, run_sub: RunSubstrate, env_handle: ComposedEnvHandle
    ) -> RunnerClient:
        if env_handle.trial_runner_client is not None:
            return env_handle.trial_runner_client
        if run_sub.runner_client is not None:
            return run_sub.runner_client
        raise RuntimeError(
            f"DefaultSubstrateComposer.runner_client_for: neither run-scope nor "
            f"trial-scope stack owns a runner client for run={run_sub.run_id!r} "
            f"trial={env_handle.trial_id!r}; the composition plan declared no "
            "runner_service."
        )

    def endpoints_for(self, run_sub: RunSubstrate, env_handle: ComposedEnvHandle) -> EnvEndpoints:
        if env_handle.trial_endpoints is not None:
            return env_handle.trial_endpoints
        if run_sub.endpoints is not None:
            return run_sub.endpoints
        raise RuntimeError(
            f"DefaultSubstrateComposer.endpoints_for: neither run-scope nor "
            f"trial-scope stack owns env endpoints for run={run_sub.run_id!r} "
            f"trial={env_handle.trial_id!r}; the composition plan declared no "
            "runner_service."
        )

    # ------------------------------------------------------------------
    # Internal helpers (instance-scope)
    # ------------------------------------------------------------------

    def _apply_reset_recipes(
        self,
        manifest: EnvironmentManifest,
        stack_handles: list[StackHandle],
        seeds: Mapping[str, SeedRef],
        trial_id: str,
    ) -> None:
        """Cycle every ``reset``-labelled service on every newly-materialised
        stack. Refusals lift to ``stage="reset_recipe"``.

        Matches today's :meth:`PerTrialRuntimeBackend._apply_reset_recipes`
        text: seed-missing and reset-recipe-error messages travel verbatim.
        """
        reset_dispatcher = self.dispatcher_registry.get("reset")
        if reset_dispatcher is None:
            return
        for stack_handle in stack_handles:
            for service_name, service_spec in manifest.services.items():
                if service_spec.isolation != "reset":
                    continue
                self._apply_reset_for_service(
                    service_name,
                    service_spec,
                    stack_handle,
                    seeds,
                    trial_id,
                    reset_dispatcher,
                )

    def _apply_reset_for_service(
        self,
        service_name: str,
        service_spec: ServiceSpec,
        stack_handle: StackHandle,
        seeds: Mapping[str, SeedRef],
        trial_id: str,
        reset_dispatcher: ServiceLifecycleDispatcher,
    ) -> None:
        try:
            reset_dispatcher.cycle(
                service_name,
                service_spec,
                stack_handle,
                self.materialiser,
                seeds=seeds,
            )
        except ProvisionError as exc:
            raise ProvisionError(
                trial_id=trial_id,
                stage="reset_recipe",
                reason=exc.reason,
            ) from exc


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _require_manifest(spec: TrialSpec) -> EnvironmentManifest:
    """Return ``spec.task.environment_manifest`` or raise the canonical
    provision-time refusal — a task with no manifest can't take the
    composer path (that shape is Case A shared-stack territory)."""
    manifest = spec.task.environment_manifest
    if manifest is None:
        raise ProvisionError(
            trial_id=spec.trial_id,
            stage="provision",
            reason=(
                "DefaultSubstrateComposer.provision_trial requires "
                "TaskDescription.environment_manifest; task did not declare one."
            ),
        )
    return manifest


def _refuse_reserved_prefix(manifest: EnvironmentManifest, trial_id: str) -> None:
    """Refuse a ``TOLOKAFORGE_``-prefixed key in ``stack_inputs``.

    Refuses the same reserved-prefix ``stack_inputs`` keys
    :meth:`PerTrialRuntimeBackend.provision` refuses, so both paths
    reject an identical manifest with identical text.
    """
    reserved = sorted(
        key for key in manifest.stack_inputs if key.startswith(TOLOKAFORGE_ENV_PREFIX)
    )
    if reserved:
        raise ProvisionError(
            trial_id=trial_id,
            stage="provision",
            reason=(
                f"stack_inputs key {reserved[0]!r} uses the reserved "
                f"{TOLOKAFORGE_ENV_PREFIX} prefix (engine-authored compose "
                "variables); rename or remove it from the manifest"
            ),
        )


def _validate_plan(plan: CompositionPlan) -> None:
    """Enforce INV-12: exactly one stack across the plan sets ``runner_service``.

    ADR-0044 § 5 INV-12 says the runner-credential injection point is
    plan-unique. Zero is legal only when the trial owns the runner via a
    trial-scope stack; the second call site (``provision_trial``) validates
    that pattern separately. ``_validate_plan`` fires on ``materialise_run``,
    so a plan with zero runners AND no trial-scope stack fails here.
    """
    runners = [decl for decl in plan if decl.runner_service is not None]
    if len(runners) >= 2:
        offending = sorted(decl.stack_id for decl in runners)
        raise ProvisionError(
            trial_id="",
            stage="materialise_run",
            reason=(
                f"INV-12: exactly one stack across the plan may set runner_service; "
                f"got {len(runners)} on stacks {offending!r}."
            ),
        )
    if not runners:
        has_trial_scope = any(decl.stack_scope == "trial" for decl in plan)
        if not has_trial_scope:
            raise ProvisionError(
                trial_id="",
                stage="materialise_run",
                reason=(
                    "INV-12: composition plan declares no runner_service and no "
                    "trial-scope stack; the run has no runner."
                ),
            )


def _resolve_env_endpoints(
    materialiser: ComposeMaterialiser,
    runner_handle: StackHandle,
    runner_host: str,
    runner_host_port: int,
    manifest: EnvironmentManifest,
) -> EnvEndpoints:
    """Assemble :class:`EnvEndpoints` via the materialiser's endpoint API.

    Substrate-agnostic counterpart to
    :func:`~tolokaforge.core.compose_materialisation.resolve_env_endpoints`:
    the ``runner_url`` is composed from the resolved runner host/port
    (no re-lookup), ``db_url`` and ``rag_url`` are best-effort — a
    missing service or unexposed port leaves them ``None``, mirroring
    today's flow.
    """
    db_service = manifest.db_service or DB_SERVICE_DEFAULT
    db_port = manifest.db_port or DB_SERVICE_PORT_DEFAULT
    db_endpoint = materialiser.resolve_endpoint(runner_handle, db_service, db_port)
    db_url = f"http://{db_endpoint[0]}:{db_endpoint[1]}" if db_endpoint is not None else None
    rag_url: str | None = None
    if manifest.rag_service is not None and manifest.rag_port is not None:
        rag_endpoint = materialiser.resolve_endpoint(
            runner_handle, manifest.rag_service, manifest.rag_port
        )
        if rag_endpoint is not None:
            rag_url = f"http://{rag_endpoint[0]}:{rag_endpoint[1]}"
    return EnvEndpoints(
        db_url=db_url,
        rag_url=rag_url,
        runner_url=f"http://{runner_host}:{runner_host_port}",
    )


def _readiness_targets(
    manifest: EnvironmentManifest,
    materialiser: ComposeMaterialiser,
    runner_handle: StackHandle,
    runner_host: str,
    runner_host_port: int,
    trial_id: str,
) -> dict[str, tuple[str, ResolvedEndpoint]]:
    """Build the readiness-gate probe map ``service -> (kind, endpoint)``.

    Runner is probed with ``grpc`` on its resolved host port; every
    service that declares a ``readiness`` spec is additionally probed
    by that spec's kind on its first published port. A declared-readiness
    service that exposes no resolvable port is a
    :class:`ProvisionError` with ``stage="provision"`` — the contract
    cannot be honoured, so a silent skip would mask the misconfiguration.
    """
    targets: dict[str, tuple[str, ResolvedEndpoint]] = {
        manifest.runner_service: (
            "grpc",
            ResolvedEndpoint(host=runner_host, port=runner_host_port),
        )
    }
    for service_name, service_spec in manifest.services.items():
        if service_spec.readiness is None:
            continue
        endpoint = _resolve_service_endpoint(materialiser, runner_handle, service_name)
        if endpoint is None:
            raise ProvisionError(
                trial_id=trial_id,
                stage="provision",
                reason=(
                    f"service {service_name!r} declares a "
                    f"{service_spec.readiness.kind!r} readiness contract but exposes "
                    "no resolvable published port to probe"
                ),
            )
        targets[service_name] = (service_spec.readiness.kind, endpoint)
    return targets


def _resolve_service_endpoint(
    materialiser: ComposeMaterialiser,
    stack_handle: StackHandle,
    service_name: str,
) -> ResolvedEndpoint | None:
    """Resolve a declared-readiness service's first published port.

    Discovers the service's container port through the compose-side
    scan on the built-in docker-compose handle, then routes the
    ``(container_port) -> (host, host_port)`` translation through
    :meth:`ComposeMaterialiser.resolve_endpoint`. Foreign handle
    families (K8s, remote) get ``None`` from the container-port scan
    and are free to satisfy readiness through their own surface when
    they register a materialiser.
    """
    from tolokaforge.core.compose_materialisation import first_published_port
    from tolokaforge.core.docker_compose_materialiser import _DockerComposeStackHandle

    if not isinstance(stack_handle, _DockerComposeStackHandle):
        return None
    try:
        container = stack_handle.compose.get_container(service_name=service_name)
    except Exception as exc:  # noqa: BLE001 — service not in stack; treat as unresolvable
        logger.debug(
            "DefaultSubstrateComposer: readiness service %r absent: %s",
            service_name,
            exc,
        )
        return None
    container_port = first_published_port(container)
    if container_port is None:
        return None
    endpoint = materialiser.resolve_endpoint(stack_handle, service_name, container_port)
    if endpoint is None:
        return None
    host, host_port = endpoint
    return ResolvedEndpoint(host=host, port=host_port)


def _run_readiness_gate(
    *,
    readiness_probe_loader: Callable[[str], ReadinessProbeFactory],
    services: dict[str, tuple[str, ResolvedEndpoint]],
    trial_id: str,
    timeout: float,
) -> None:
    """Probe every gated service; the first not-ready result raises.

    Services are probed in ``services`` insertion order — runner-first,
    because :func:`_readiness_targets` seeds the runner first. A broken
    runner surfaces before budget is spent on sidecars.
    """
    for service_name, (kind, endpoint) in services.items():
        probe = readiness_probe_loader(kind)()
        result = probe.probe(endpoint, timeout=timeout)
        if result.ok:
            continue
        raise ProvisionError(
            trial_id=trial_id,
            stage="provision",
            reason=(
                f"service {service_name!r} ({kind}) not ready at "
                f"{endpoint.host}:{endpoint.port} within {timeout}s: {result.detail}"
            ),
        )


def _run_scope_log_capture(
    log_capture: LogCaptureConfig | None,
) -> MaterialiseLogCapture | None:
    """Adapt a run-level :class:`LogCaptureConfig` to a
    :class:`MaterialiseLogCapture` writing under ``<output_root>/services/``.

    Returns ``None`` when capture is disabled at the run level.
    """
    if log_capture is None:
        return None
    return MaterialiseLogCapture(
        dest_dir=run_services_dir(log_capture.output_root),
        tail=log_capture.tail,
    )


def _trial_scope_log_capture(
    log_capture: LogCaptureConfig | None,
    trial_id: str,
) -> MaterialiseLogCapture | None:
    """Adapt to a per-trial :class:`MaterialiseLogCapture` writing under
    ``<output_root>/trials/<task_id>/<index>/services/``.

    Returns ``None`` when capture is disabled at the run level.
    """
    if log_capture is None:
        return None
    return MaterialiseLogCapture(
        dest_dir=trial_services_dir(log_capture.output_root, trial_id),
        tail=log_capture.tail,
    )


def _find_runner_owner(
    handles: list[StackHandle],
    decls: list[StackDecl],
) -> tuple[StackHandle | None, StackDecl | None]:
    """Return the ``(handle, decl)`` pair whose decl carries ``runner_service``.

    Callers pass paired lists (one entry per materialised stack in
    ``decls`` order); returns ``(None, None)`` when no decl in this scope
    owns the runner. INV-12 makes ≥ 1 an error at plan-validation time,
    so this helper never sees two.
    """
    for handle, decl in zip(handles, decls, strict=True):
        if decl.runner_service is not None:
            return handle, decl
    return None, None


def _teardown_handles_best_effort(
    materialiser: ComposeMaterialiser,
    handles: list[StackHandle],
) -> None:
    """Tear down each handle; log and continue on error.

    Used on materialise-time rollback and on run/trial teardown. Never
    raises past its own boundary — a failure to tear down one stack
    must not prevent siblings from coming down.
    """
    for handle in handles:
        try:
            materialiser.teardown(handle)
        except Exception:  # noqa: BLE001 — teardown must never mask sibling cleanup
            logger.exception(
                "DefaultSubstrateComposer: teardown failed for stack %r",
                getattr(handle, "stack_id", "<unknown>"),
            )
