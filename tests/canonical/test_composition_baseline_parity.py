"""Composer output matches the frozen baseline byte-for-byte.

Locks the load-bearing invariant #1381 leans on: the built-in
``DefaultSubstrateComposer + DockerComposeMaterialiser + DISPATCHER_REGISTRY``
triple drives the observable outputs the frozen fixtures under
``fixtures/composition_parity_baseline/{scenario_a,scenario_b}/`` record
for the two single-stack shapes — Scenario A (SINGLE_RUN, run-scope
runner) and Scenario B (TRIAL_SCOPED_ONLY, trial-scope runner with a
reset service). The baseline is the eternal reference; CI never
regenerates it, and a legitimate shift lands in the same commit as the
code change that produced it.

Two scenarios drive :class:`DefaultSubstrateComposer` in-process
against an :class:`InertDockerCompose` stub factory and assert equality
on:

* the transformed compose file bytes on disk (network-policy transform
  + credential injection + docker-socket mount + trial-mode ``.env``),
* the sequence of driver-side calls the stub records
  (``__init__`` / ``start`` / ``get_containers`` at provision, ``stop``
  at teardown),
* the resolved :class:`EnvEndpoints`,
* the temp-dir basename slug prefix,
* the log-router component-id list, and
* (Scenario B) the reset-recipe invocation sequence — same seed / service
  pair in the same order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._docker_compose_stubs import InertDockerCompose, driver_state
from tests.canonical._factories import make_task_description
from tolokaforge.core.composition_runtime import RunCtx, RunSubstrate
from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer
from tolokaforge.core.docker_compose_materialiser import (
    DockerComposeMaterialiser,
    _DockerComposeStackHandle,
)
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.models.task_config import SeedRef
from tolokaforge.core.project_loader import _synthesise_composition_plan
from tolokaforge.core.run_display_events import _NULL_EVENTS
from tolokaforge.core.service_readiness import InMemoryServiceReadinessProbe
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import ResetSpec, ServiceSpec
from tolokaforge.runtime import reset_recipes as reset_recipes_module

pytestmark = pytest.mark.canonical


_FIXTURE = Path(__file__).parent / "fixtures" / "environment_manifest" / "safe_two_service.yaml"
_FIXTURE_RUNNER_SERVICE = "default"
_BASELINE_ROOT = Path(__file__).parent / "fixtures" / "composition_parity_baseline"
_SEED_KIND = "sql_dump"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _tuples_to_lists(value: Any) -> Any:
    """Recursively convert tuples to lists for JSON serialisation.

    :func:`driver_state` returns nested tuples for equality-check
    clarity; JSON round-trips them as lists either way, so both sides
    normalise before comparing.
    """
    if isinstance(value, tuple | list):
        return [_tuples_to_lists(v) for v in value]
    if isinstance(value, dict):
        return {k: _tuples_to_lists(v) for k, v in value.items()}
    return value


def _always_ready_loader(kind: str) -> Any:
    """Always-ready in-memory probe factory — the readiness gate never
    stalls on a real listener."""
    del kind
    return lambda: InMemoryServiceReadinessProbe(ok=True)


class _RecordingRunnerClient:
    """Non-network runner client used by the composer's factory seam."""

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
    """Non-docker :class:`RecipeDispatcher` — records every dispatch."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def apply(self, seed: SeedRef, service_name: str, compose: Any) -> None:
        del compose
        self.calls.append((seed.kind, service_name))


def _basename_slug_prefix(temp_dir: Path) -> str:
    return temp_dir.name.rsplit("-", 1)[0]


# ---------------------------------------------------------------------------
# Scenario A — SharedStackRuntimeBackend env_manifest / SINGLE_RUN plan
# ---------------------------------------------------------------------------


def _make_shared_manifest() -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_FIXTURE,
        runner_service=_FIXTURE_RUNNER_SERVICE,
        services={
            "db": ServiceSpec(isolation="shared"),
            "default": ServiceSpec(isolation="shared"),
        },
    )


def _run_composer_scenario_a(
    manifest: EnvironmentManifest,
    stub_sink: list[InertDockerCompose],
) -> tuple[DefaultSubstrateComposer, RunSubstrate, Path]:
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
    assert isinstance(handle, _DockerComposeStackHandle)
    return composer, run_sub, handle.temp_dir


def test_scenario_a_shared_stack_env_manifest_matches_baseline() -> None:
    """Scenario A — :meth:`DefaultSubstrateComposer.materialise_run` on
    a SINGLE_RUN plan produces the six observable outputs the frozen
    ``scenario_a`` fixture holds (compose bytes, driver-call sequence,
    endpoints, temp-dir basename slug, log-router component ids)."""
    baseline_dir = _BASELINE_ROOT / "scenario_a"
    stubs: list[InertDockerCompose] = []
    composer, run_sub, temp_dir = _run_composer_scenario_a(_make_shared_manifest(), stubs)
    try:
        assert (temp_dir / _FIXTURE.name).read_bytes() == (
            baseline_dir / "compose.yaml"
        ).read_bytes()
        assert _tuples_to_lists(driver_state(stubs[0])) == json.loads(
            (baseline_dir / "driver_state.json").read_text()
        )
        assert run_sub.endpoints is not None
        assert run_sub.endpoints.model_dump(mode="json") == json.loads(
            (baseline_dir / "endpoints.json").read_text()
        )
        assert _basename_slug_prefix(temp_dir) == (baseline_dir / "basename_prefix.txt").read_text()
        handle = run_sub.run_stack_handles[0]
        assert isinstance(handle, _DockerComposeStackHandle)
        assert [router.component_id for router in handle.log_routers] == json.loads(
            (baseline_dir / "log_components.json").read_text()
        )
    finally:
        composer.teardown_run(run_sub)

    assert stubs[0].calls[-1] == ("stop", (True,))
    assert not temp_dir.exists()


# ---------------------------------------------------------------------------
# Scenario B — PerTrialRuntimeBackend / TRIAL_SCOPED_ONLY plan
# ---------------------------------------------------------------------------


def _make_reset_manifest() -> EnvironmentManifest:
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


def _run_composer_scenario_b(
    manifest: EnvironmentManifest,
    seed: SeedRef,
    stub_sink: list[InertDockerCompose],
) -> tuple[DefaultSubstrateComposer, Any, Path]:
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
        mount_docker_socket=False,
        log_capture=None,
        events=_NULL_EVENTS,
    )
    spec = _make_trial_spec(manifest)
    env_handle = composer.provision_trial(plan, spec, empty_run_sub)
    trial_handle = env_handle.trial_stack_handles[0]
    assert isinstance(trial_handle, _DockerComposeStackHandle)
    return composer, env_handle, trial_handle.temp_dir


def test_scenario_b_per_trial_backend_matches_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario B — :meth:`DefaultSubstrateComposer.provision_trial` on
    a TRIAL_SCOPED_ONLY plan produces the same seven observable outputs
    the frozen ``scenario_b`` fixture recorded from today's
    ``PerTrialRuntimeBackend.provision`` inline flow.
    """
    baseline_dir = _BASELINE_ROOT / "scenario_b"
    stubs: list[InertDockerCompose] = []
    recipe = _RecordingRecipe()
    seed = _make_seed()

    monkeypatch.setitem(reset_recipes_module.RECIPE_REGISTRY, _SEED_KIND, recipe)
    composer, env_handle, temp_dir = _run_composer_scenario_b(_make_reset_manifest(), seed, stubs)
    try:
        assert (temp_dir / _FIXTURE.name).read_bytes() == (
            baseline_dir / "compose.yaml"
        ).read_bytes()
        assert (temp_dir / ".env").read_text() == (baseline_dir / "env.txt").read_text()
        assert _tuples_to_lists(driver_state(stubs[0])) == json.loads(
            (baseline_dir / "driver_state.json").read_text()
        )
        assert env_handle.trial_endpoints is not None
        assert env_handle.trial_endpoints.model_dump(mode="json") == json.loads(
            (baseline_dir / "endpoints.json").read_text()
        )
        assert _basename_slug_prefix(temp_dir) == (baseline_dir / "basename_prefix.txt").read_text()
        trial_handle = env_handle.trial_stack_handles[0]
        assert isinstance(trial_handle, _DockerComposeStackHandle)
        assert [router.component_id for router in trial_handle.log_routers] == json.loads(
            (baseline_dir / "log_components.json").read_text()
        )
        assert [list(call) for call in recipe.calls] == json.loads(
            (baseline_dir / "recipe_calls.json").read_text()
        )
    finally:
        composer.teardown_trial(env_handle)

    assert stubs[0].calls[-1] == ("stop", (True,))
    assert not temp_dir.exists()
