"""Parity contract — composer triple matches today's inline flows byte-for-byte.

Locks the load-bearing invariant #1381 leans on: the built-in
``DefaultSubstrateComposer + DockerComposeMaterialiser + DISPATCHER_REGISTRY``
triple drives the same observable outputs today's
:meth:`SharedStackRuntimeBackend._materialise_manifest` (Scenario A) and
:meth:`PerTrialRuntimeBackend.provision` (Scenario B) do for their
single-stack shapes. Two scenarios run each flow in-process against the
same :class:`InertDockerCompose` stub factory and assert equality on:

* the transformed compose file bytes on disk (network-policy transform +
  credential injection + docker-socket mount + trial-mode ``.env``),
* the sequence of driver-side calls the stub records
  (``__init__`` / ``start`` / ``get_containers`` at provision, ``stop``
  at teardown),
* the resolved :class:`EnvEndpoints`,
* the temp-dir basename slug prefix, and
* (Scenario B) the reset-recipe invocation sequence — same seed / service
  triple in the same order.

Path 1 (backend baseline) substitutes the stub via
``monkeypatch.setattr`` on the modules'
:data:`~testcontainers.compose.DockerCompose` and
:data:`GrpcRunnerClient` symbols; that is a test-only monkeypatch, not a
production factory-seam edit on either backend (which is deferred to
#1381). Path 2 (composer) constructs the composer with the same stub
factory injected via :attr:`DockerComposeMaterialiser.docker_compose_factory`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.canonical._docker_compose_stubs import InertDockerCompose, driver_state
from tests.canonical._factories import make_task_description
from tolokaforge.core import per_trial_runtime as per_trial_runtime_module
from tolokaforge.core import shared_stack_runtime as shared_stack_runtime_module
from tolokaforge.core.composition_runtime import RunCtx, RunSubstrate
from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer
from tolokaforge.core.docker_compose_materialiser import (
    DockerComposeMaterialiser,
    _DockerComposeStackHandle,
)
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.models.task_config import SeedRef
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend, _LocalEnvHandle
from tolokaforge.core.project_loader import _synthesise_composition_plan
from tolokaforge.core.run_display_events import _NULL_EVENTS
from tolokaforge.core.service_readiness import InMemoryServiceReadinessProbe
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import ResetSpec, ServiceSpec
from tolokaforge.runtime import reset_recipes as reset_recipes_module

pytestmark = pytest.mark.canonical


_FIXTURE = Path(__file__).parent / "fixtures" / "environment_manifest" / "safe_two_service.yaml"
_FIXTURE_RUNNER_SERVICE = "default"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _always_ready_loader(kind: str) -> Any:
    """Readiness-probe loader that hands back an always-ready in-memory
    probe. Same shape both scenarios use so the readiness gate never
    stalls on a real listener."""
    del kind
    return lambda: InMemoryServiceReadinessProbe(ok=True)


class _RecordingRunnerClient:
    """Non-network :class:`GrpcRunnerClient` stand-in.

    Records constructor kwargs and ``close`` so the teardown-parity
    checks can see the client was addressed identically on both paths.
    """

    def __init__(self, runner_address: str, *, events: Any = None) -> None:
        self.runner_address = runner_address
        self.events = events
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        del timeout, retry_interval

    def health_check(self) -> bool:
        return True


class _RecordingRecipe:
    """Non-docker :class:`RecipeDispatcher` — records every dispatch.

    Installed into :data:`RECIPE_REGISTRY` for the seed kind Scenario B
    uses; both the backend baseline (``dispatch(seed, service, compose)``)
    and the composer's :class:`ResetDispatcher.cycle` reach the same
    registry entry, so the two paths' recorded call lists compare
    directly.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, InertDockerCompose]] = []

    def apply(self, seed: SeedRef, service_name: str, compose: Any) -> None:
        self.calls.append((seed.kind, service_name, compose))


def _basename_slug_prefix(temp_dir: Path) -> str:
    """Return the temp-dir basename with its random mkdtemp suffix
    stripped, so two mktemp runs on the same slug compare equal.

    :func:`tempfile.mkdtemp` appends an 8-char random suffix; the slug
    prefix is everything up to the last ``-``.
    """
    return temp_dir.name.rsplit("-", 1)[0]


# ---------------------------------------------------------------------------
# Scenario A — SharedStackRuntimeBackend env_manifest mode / SINGLE_RUN plan
# ---------------------------------------------------------------------------


def _make_shared_manifest() -> EnvironmentManifest:
    """All-``shared`` two-service manifest — synthesises a SINGLE_RUN plan
    with one run-scope stack owning the runner."""
    return EnvironmentManifest(
        compose_file=_FIXTURE,
        runner_service=_FIXTURE_RUNNER_SERVICE,
        services={
            "db": ServiceSpec(isolation="shared"),
            "default": ServiceSpec(isolation="shared"),
        },
    )


def _run_backend_scenario_a(
    monkeypatch: pytest.MonkeyPatch,
    manifest: EnvironmentManifest,
    stub_sink: list[InertDockerCompose],
) -> tuple[SharedStackRuntimeBackend, Path]:
    """Drive today's inline flow — construct the backend with monkey-
    patched ``DockerCompose`` and ``GrpcRunnerClient`` and materialise.

    Returns the backend and its temp dir so the caller can extract the
    compose bytes and endpoints, then drive ``close()`` for the teardown-
    parity check.
    """

    def factory(**kwargs: Any) -> InertDockerCompose:
        stub = InertDockerCompose(**kwargs)
        stub_sink.append(stub)
        return stub

    monkeypatch.setattr(shared_stack_runtime_module, "DockerCompose", factory)
    monkeypatch.setattr(shared_stack_runtime_module, "GrpcRunnerClient", _RecordingRunnerClient)
    backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-a")
    backend._materialise_manifest()
    assert backend._temp_dir is not None  # narrowed by materialise
    return backend, backend._temp_dir


def _run_composer_scenario_a(
    manifest: EnvironmentManifest,
    stub_sink: list[InertDockerCompose],
) -> tuple[DefaultSubstrateComposer, RunSubstrate, Path]:
    """Drive the composer path with the same stub factory injected into
    :class:`DockerComposeMaterialiser`.

    Returns ``(composer, run_sub, temp_dir)`` — mirroring the backend
    helper's shape so the parity assertions compare like with like.
    """

    def factory(**kwargs: Any) -> InertDockerCompose:
        stub = InertDockerCompose(**kwargs)
        stub_sink.append(stub)
        return stub

    materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)
    composer = DefaultSubstrateComposer(
        materialiser=materialiser,
        runner_client_factory=lambda addr, events: _RecordingRunnerClient(addr, events=events),
    )
    _synthesise_composition_plan(manifest, {})
    plan = list(manifest.stacks)
    ctx = RunCtx(
        run_id="run-a",
        manifest=manifest,
        mount_docker_socket=False,
        log_capture=None,
        events=_NULL_EVENTS,
        seeds={},
    )
    run_sub = composer.materialise_run(plan, ctx)
    handle = run_sub.run_stack_handles[0]
    assert isinstance(handle, _DockerComposeStackHandle)  # narrows for temp_dir read
    return composer, run_sub, handle.temp_dir


def test_scenario_a_shared_stack_env_manifest_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario A — a SINGLE_RUN plan reaches identical observable state
    through today's ``SharedStackRuntimeBackend._materialise_manifest``
    and through :meth:`DefaultSubstrateComposer.materialise_run`:

    compose file bytes, driver-call sequence, resolved endpoints,
    temp-dir basename shape, and the compose-side teardown call sequence.
    """
    baseline_stubs: list[InertDockerCompose] = []
    composer_stubs: list[InertDockerCompose] = []

    backend, baseline_temp_dir = _run_backend_scenario_a(
        monkeypatch, _make_shared_manifest(), baseline_stubs
    )
    composer, run_sub, composer_temp_dir = _run_composer_scenario_a(
        _make_shared_manifest(), composer_stubs
    )
    try:
        baseline_bytes = (baseline_temp_dir / _FIXTURE.name).read_bytes()
        composer_bytes = (composer_temp_dir / _FIXTURE.name).read_bytes()
        assert composer_bytes == baseline_bytes

        assert driver_state(composer_stubs[0]) == driver_state(baseline_stubs[0])
        assert run_sub.endpoints == backend._endpoints

        assert _basename_slug_prefix(composer_temp_dir) == _basename_slug_prefix(baseline_temp_dir)
    finally:
        backend.close()
        composer.teardown_run(run_sub)

    # Teardown parity — both paths must drive the stub's ``stop(down=True)``
    # exactly once during their close/teardown_run sequence, and remove
    # their temp dirs.
    assert baseline_stubs[0].calls[-1] == ("stop", (True,))
    assert composer_stubs[0].calls[-1] == ("stop", (True,))
    assert not baseline_temp_dir.exists()
    assert not composer_temp_dir.exists()


# ---------------------------------------------------------------------------
# Scenario B — PerTrialRuntimeBackend / TRIAL_SCOPED_ONLY plan
# ---------------------------------------------------------------------------


_SEED_KIND = "sql_dump"


def _make_reset_manifest() -> EnvironmentManifest:
    """Two-service manifest whose ``db`` service is labelled ``reset``.

    :attr:`requires_per_trial` returns True (one non-``shared`` service)
    so :func:`_synthesise_composition_plan` produces a TRIAL_SCOPED_ONLY
    plan.
    """
    return EnvironmentManifest(
        compose_file=_FIXTURE,
        runner_service=_FIXTURE_RUNNER_SERVICE,
        services={
            "db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
            "default": ServiceSpec(isolation="shared"),
        },
    )


def _make_seed() -> SeedRef:
    return SeedRef.model_validate(
        {"path": "seed.sql", "kind": _SEED_KIND, "digest": "sha256:" + "0" * 64}
    )


def _make_trial_spec(manifest: EnvironmentManifest) -> TrialSpec:
    return TrialSpec(
        trial_id="task-1:0",
        run_id="run-a",
        task=make_task_description(task_id="task-1", environment_manifest=manifest),
        agent_model_config=ModelConfig(provider="anthropic", name="stub"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:5432",
            runner_url="http://placeholder:50051",
        ),
    )


def _run_backend_scenario_b(
    monkeypatch: pytest.MonkeyPatch,
    manifest: EnvironmentManifest,
    seed: SeedRef,
    stub_sink: list[InertDockerCompose],
) -> tuple[PerTrialRuntimeBackend, _LocalEnvHandle]:
    """Drive today's inline flow — construct the backend with monkey-
    patched ``DockerCompose`` and ``GrpcRunnerClient`` and provision.

    The backend's ``mount_docker_socket`` is left at its ``False``
    default so the composer's ``mount_docker_socket=False`` for
    trial-scope stacks compares equal.
    """

    def factory(**kwargs: Any) -> InertDockerCompose:
        stub = InertDockerCompose(**kwargs)
        stub_sink.append(stub)
        return stub

    monkeypatch.setattr(per_trial_runtime_module, "DockerCompose", factory)
    monkeypatch.setattr(per_trial_runtime_module, "GrpcRunnerClient", _RecordingRunnerClient)
    backend = PerTrialRuntimeBackend(
        seeds={"baseline": seed},
        readiness_probe_loader=_always_ready_loader,
    )
    spec = _make_trial_spec(manifest)
    handle = backend.provision(spec)
    assert isinstance(handle, _LocalEnvHandle)
    return backend, handle


def _run_composer_scenario_b(
    manifest: EnvironmentManifest,
    seed: SeedRef,
    stub_sink: list[InertDockerCompose],
) -> tuple[DefaultSubstrateComposer, Any, Path]:
    """Drive the composer path.

    Returns ``(composer, env_handle, temp_dir)`` so the caller can read
    the trial stack's compose bytes and drive teardown.
    """

    def factory(**kwargs: Any) -> InertDockerCompose:
        stub = InertDockerCompose(**kwargs)
        stub_sink.append(stub)
        return stub

    materialiser = DockerComposeMaterialiser(docker_compose_factory=factory)
    composer = DefaultSubstrateComposer(
        materialiser=materialiser,
        runner_client_factory=lambda addr, events: _RecordingRunnerClient(addr, events=events),
        readiness_probe_loader=_always_ready_loader,
    )
    _synthesise_composition_plan(manifest, {})
    plan = list(manifest.stacks)
    empty_run_sub = RunSubstrate(
        run_id="run-a",
        run_stack_handles=(),
        task_stack_handles={},
        runner_client=None,
        endpoints=None,
        seeds={"baseline": seed},
    )
    spec = _make_trial_spec(manifest)
    env_handle = composer.provision_trial(plan, spec, empty_run_sub)
    trial_handle = env_handle.trial_stack_handles[0]
    assert isinstance(trial_handle, _DockerComposeStackHandle)
    return composer, env_handle, trial_handle.temp_dir


def test_scenario_b_per_trial_backend_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario B — a TRIAL_SCOPED_ONLY plan reaches identical observable
    state through :meth:`PerTrialRuntimeBackend.provision` and through
    :meth:`DefaultSubstrateComposer.provision_trial`:

    compose file bytes, ``.env`` contents, driver-call sequence, resolved
    endpoints, temp-dir basename shape, reset-recipe invocation order,
    and the compose-side teardown call sequence.
    """
    baseline_stubs: list[InertDockerCompose] = []
    composer_stubs: list[InertDockerCompose] = []
    baseline_recipe = _RecordingRecipe()
    composer_recipe = _RecordingRecipe()
    seed = _make_seed()

    monkeypatch.setitem(reset_recipes_module.RECIPE_REGISTRY, _SEED_KIND, baseline_recipe)
    backend, baseline_handle = _run_backend_scenario_b(
        monkeypatch, _make_reset_manifest(), seed, baseline_stubs
    )
    baseline_temp_dir = baseline_handle.temp_dir
    baseline_endpoints = baseline_handle.endpoints
    baseline_bytes = (baseline_temp_dir / _FIXTURE.name).read_bytes()
    baseline_env = (baseline_temp_dir / ".env").read_text()

    monkeypatch.setitem(reset_recipes_module.RECIPE_REGISTRY, _SEED_KIND, composer_recipe)
    composer, env_handle, composer_temp_dir = _run_composer_scenario_b(
        _make_reset_manifest(), seed, composer_stubs
    )

    try:
        composer_bytes = (composer_temp_dir / _FIXTURE.name).read_bytes()
        composer_env = (composer_temp_dir / ".env").read_text()

        assert composer_bytes == baseline_bytes
        assert composer_env == baseline_env
        assert driver_state(composer_stubs[0]) == driver_state(baseline_stubs[0])
        assert env_handle.trial_endpoints == baseline_endpoints
        assert _basename_slug_prefix(composer_temp_dir) == _basename_slug_prefix(baseline_temp_dir)
        # Reset-recipe invocation parity — same seed kind and service
        # name in the same order. The ``compose`` third tuple entry is
        # each path's own stub instance and is not compared across
        # paths; asserting ``is`` against the recorded stub proves the
        # dispatch received the newly-materialised stack's handle.
        assert [(k, s) for k, s, _ in baseline_recipe.calls] == [("sql_dump", "db")]
        assert [(k, s) for k, s, _ in composer_recipe.calls] == [("sql_dump", "db")]
        assert baseline_recipe.calls[0][2] is baseline_stubs[0]
        assert composer_recipe.calls[0][2] is composer_stubs[0]
    finally:
        backend.teardown(baseline_handle)
        composer.teardown_trial(env_handle)

    assert baseline_stubs[0].calls[-1] == ("stop", (True,))
    assert composer_stubs[0].calls[-1] == ("stop", (True,))
    assert not baseline_temp_dir.exists()
    assert not composer_temp_dir.exists()
