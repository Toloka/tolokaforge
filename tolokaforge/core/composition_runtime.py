"""Composition-plan adapter seams — Protocols + companion frozen dataclasses.

ADR-0044 splits the compose-mode runtime into three detachable adapter
families that a substrate composer stitches together:

* :class:`ComposeMaterialiser` — brings a single :class:`StackDecl` up as
  a live compose project and tears it down. Docker-compose is the built-in
  impl; K8s / remote materialisers are future adapters against the same
  Protocol.
* :class:`ServiceLifecycleDispatcher` — cycles one service between trials
  for a given :data:`~tolokaforge.runner.models.ServiceIsolation` label.
  One dispatcher per label; the composer looks up by label at cycle time.
* :class:`SubstrateComposer` — the sequencer. Owns the plan-shape
  invariants (INV-12), walks each stack in scope order, and delegates
  materialisation and between-trial cycling to the adapters above.

This module ships the interface contract only. Built-in implementations
live in sibling modules (``docker_compose_materialiser``,
``service_lifecycle_dispatchers``, ``default_substrate_composer``) and
are unrelated to the Protocol shape check that ``test_composition_runtime_protocols``
locks against this file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.models.task_config import SeedRef
from tolokaforge.core.run_display_events import ContainerSnapshot, RunDisplayEvents
from tolokaforge.core.runtime import EnvHandle
from tolokaforge.core.shared_stack_runtime import RunnerClient
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, NetworkPolicy, TrialSpec
from tolokaforge.runner.models import (
    PlanShape,
    ServiceIsolation,
    ServiceSpec,
    StackDecl,
    StackScope,
)

__all__ = [
    "ComposeMaterialiser",
    "ComposedEnvHandle",
    "CompositionPlan",
    "EnvHandle",
    "MaterialiseContext",
    "MaterialiseLogCapture",
    "PlanShape",
    "RunCtx",
    "RunSubstrate",
    "ServiceLifecycleDispatcher",
    "StackDecl",
    "StackHandle",
    "StackScope",
    "SubstrateComposer",
    "WriteComposeEnv",
]


CompositionPlan = list[StackDecl]
"""Ordered list of :class:`StackDecl` — the resolved plan a composer
consumes. Order is significant: run-scope stacks are materialised in
plan order, task and trial-scope stacks follow the same rule at their
respective brackets."""


# ---------------------------------------------------------------------------
# Companion values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialiseLogCapture:
    """Where a materialiser writes per-service compose logs on failure.

    A ``None`` sibling on :attr:`MaterialiseContext.log_capture` disables
    capture entirely; the materialiser then skips the fail-time log dump.
    """

    dest_dir: Path
    tail: int


@dataclass(frozen=True)
class WriteComposeEnv:
    """Directive to write a compose ``.env`` file for a trial-scope stack.

    Present iff the composer explicitly requests it — run-scope stacks
    today do not write ``.env`` files, only trial-scope stacks do (they
    seed the ``TOLOKAFORGE_TRIAL_ID`` variable + the ``stack_inputs`` map
    the compose file's ``${var}`` slots consume).
    """

    trial_id: str
    stack_inputs: Mapping[str, str]


@dataclass(frozen=True)
class MaterialiseContext:
    """Everything a :class:`ComposeMaterialiser` needs to bring one stack up.

    Assembled by the composer from the run/trial context; opaque to the
    materialiser beyond the fields declared here.
    """

    scope_key: str
    stack_id: str
    network_policy: NetworkPolicy
    limited_internet_allowlist: tuple[str, ...]
    restricted_services: frozenset[str]
    mount_docker_socket: bool
    log_capture: MaterialiseLogCapture | None
    write_compose_env: WriteComposeEnv | None
    events: RunDisplayEvents
    component_id_prefix: str


@dataclass(frozen=True)
class RunCtx:
    """Run-wide context handed to :meth:`SubstrateComposer.materialise_run`.

    :attr:`seeds` is threaded onto :class:`RunSubstrate` at materialise
    time so downstream methods (``provision_trial``,
    ``cycle_between_trials``) resolve ``reset``-labelled services against
    the same seed map without the composer's callers re-threading it on
    every call.
    """

    run_id: str
    manifest: EnvironmentManifest
    mount_docker_socket: bool
    log_capture: LogCaptureConfig | None
    events: RunDisplayEvents
    seeds: Mapping[str, SeedRef]


@dataclass
class RunSubstrate:
    """Live run-wide substrate state a composer accumulates.

    Mutable because task-scope stacks materialise lazily: the first
    :meth:`SubstrateComposer.provision_trial` that observes a new
    ``(task_id, stack_id)`` pair records the handle here, and every
    subsequent trial for that task reuses it. :attr:`runner_client` and
    :attr:`endpoints` are set iff a run-scope stack owns the runner —
    trial-scope-owned runners live on :class:`ComposedEnvHandle` instead.
    """

    run_id: str
    run_stack_handles: tuple[StackHandle, ...]
    task_stack_handles: dict[tuple[str, str], StackHandle]
    runner_client: RunnerClient | None
    endpoints: EnvEndpoints | None
    seeds: Mapping[str, SeedRef]


@dataclass(frozen=True)
class ComposedEnvHandle:
    """Per-trial handle a composer hands the backend after ``provision_trial``.

    Structurally satisfies :class:`~tolokaforge.core.runtime.EnvHandle`
    via :attr:`trial_id`. The ``trial_*`` fields are populated iff a
    trial-scope stack owns the runner; when a run-scope stack owns it
    the composer's ``runner_client_for`` / ``endpoints_for`` delegate
    to :class:`RunSubstrate` instead.
    """

    trial_id: str
    trial_stack_handles: tuple[StackHandle, ...]
    trial_endpoints: EnvEndpoints | None
    trial_runner_client: RunnerClient | None


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class StackHandle(Protocol):
    """Opaque handle for one live stack a :class:`ComposeMaterialiser` owns.

    Only the three metadata attributes are public; concrete materialiser
    implementations carry private state (compose object, temp dir, log
    routers). Downstream consumers pass the handle back to the same
    materialiser that produced it.
    """

    stack_id: str
    stack_scope: StackScope
    runner_service: str | None


@runtime_checkable
class ComposeMaterialiser(Protocol):
    """Brings one :class:`StackDecl` up as a live compose project."""

    name: str

    def materialise(self, decl: StackDecl, ctx: MaterialiseContext) -> StackHandle:
        """Bring the stack up. Raises
        :class:`~tolokaforge.core.runtime.ProvisionError` with
        ``stage="provision"`` after a best-effort cleanup on failure; the
        reason string names the failure (and, for a runner-service-not-
        exposed case, names the service + port)."""

    def resolve_endpoint(
        self, handle: StackHandle, service: str, container_port: int
    ) -> tuple[str, int] | None:
        """Return ``(host, host_port)`` for the published port, or ``None``
        when the service or the requested port is not exposed. Never
        raises for a missing service or port."""

    def get_containers(self, handle: StackHandle) -> list[ContainerSnapshot]:
        """Snapshot the stack's live containers for the display path.
        Logs and returns ``[]`` on docker CLI error; never raises."""

    def capture_logs(
        self,
        handle: StackHandle,
        services: tuple[str, ...],
        dest_dir: Path,
        tail: int,
    ) -> dict[str, int]:
        """Best-effort per-service log capture. Returns
        ``{service: bytes_written}``. Never raises."""

    def teardown(self, handle: StackHandle) -> None:
        """Stop routers, ``shutdown_compose``, remove the temp dir.
        Idempotent; never raises past its own ``try/finally``."""


@runtime_checkable
class ServiceLifecycleDispatcher(Protocol):
    """Cycles one service between trials for a given isolation label.

    One dispatcher per :data:`~tolokaforge.runner.models.ServiceIsolation`
    label; :meth:`SubstrateComposer.cycle_between_trials` resolves by
    ``service_spec.isolation`` at cycle time.
    """

    isolation: ClassVar[ServiceIsolation]

    def cycle(
        self,
        service_name: str,
        service_spec: ServiceSpec,
        stack_handle: StackHandle,
        materialiser: ComposeMaterialiser,
        *,
        seeds: Mapping[str, SeedRef],
    ) -> None:
        """Cycle the service. Raises
        :class:`~tolokaforge.core.runtime.ProvisionError` with
        ``stage="cycle"`` on failure — the reason names
        ``(stack_id, service_name, isolation)``. Raises ``TypeError`` if
        the handle is a family this dispatcher does not understand.
        Idempotent by contract."""


@runtime_checkable
class SubstrateComposer(Protocol):
    """Sequencer that stitches a :class:`ComposeMaterialiser` and a
    :class:`ServiceLifecycleDispatcher` registry together across the
    run / task / trial brackets.

    Owns the composition-plan invariants (INV-12) at
    :meth:`materialise_run` and the between-trial dispatch at
    :meth:`cycle_between_trials`. The backend consumes runner client and
    endpoints via :meth:`runner_client_for` / :meth:`endpoints_for` so
    the run-owned / trial-owned split stays behind the composer.
    """

    def materialise_run(self, plan: CompositionPlan, ctx: RunCtx) -> RunSubstrate:
        """Bring up every ``run``-scope stack in plan order; wire the
        run-level runner client + endpoints iff a run-scope stack owns
        the runner. Enforces INV-12 (exactly one stack across the plan
        sets ``runner_service``). Raises
        :class:`~tolokaforge.core.runtime.ProvisionError` with
        ``stage="materialise_run"``."""

    def provision_trial(
        self,
        plan: CompositionPlan,
        spec: TrialSpec,
        run_sub: RunSubstrate,
    ) -> ComposedEnvHandle:
        """Bring up task-scope stacks (lazily per ``(task_id, stack_id)``)
        and trial-scope stacks; apply reset recipes on newly-materialised
        stacks. Wires trial-level runner client + endpoints iff a
        trial-scope stack owns the runner. Raises
        :class:`~tolokaforge.core.runtime.ProvisionError` with
        ``stage="provision" | "reset_recipe"``."""

    def cycle_between_trials(
        self,
        run_sub: RunSubstrate,
        spec: TrialSpec,
    ) -> None:
        """Walk run+task-scope stacks; for each service look up the
        dispatcher by ``service_spec.isolation`` and call
        :meth:`ServiceLifecycleDispatcher.cycle`. Refuses (raises
        ``stage="cycle"``) when an isolation label has no registered
        dispatcher."""

    def teardown_trial(self, env_handle: ComposedEnvHandle) -> None:
        """Tear down trial-scope handles and close the trial runner
        client. Idempotent."""

    def teardown_run(self, run_sub: RunSubstrate) -> None:
        """Tear down task-scope then run-scope handles and close the run
        runner client. Idempotent."""

    def runner_client_for(
        self, run_sub: RunSubstrate, env_handle: ComposedEnvHandle
    ) -> RunnerClient:
        """Resolve the runner client. Returns
        :attr:`ComposedEnvHandle.trial_runner_client` when set, else
        :attr:`RunSubstrate.runner_client`. Raises ``RuntimeError``
        naming the plan shape if both are ``None``."""

    def endpoints_for(self, run_sub: RunSubstrate, env_handle: ComposedEnvHandle) -> EnvEndpoints:
        """Resolve endpoints. Same delegation logic as
        :meth:`runner_client_for`. Raises ``RuntimeError`` naming the
        plan shape if both are ``None``."""
