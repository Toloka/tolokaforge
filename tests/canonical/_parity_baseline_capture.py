"""Frozen baseline capture for the composition-plan parity contract.

Runs today's inline compose-mode flows —
:meth:`SharedStackRuntimeBackend._materialise_manifest` (Scenario A) and
:meth:`PerTrialRuntimeBackend.provision` (Scenario B) — against the
shared :class:`InertDockerCompose` stub, snapshots the observable
outputs to a checked-in fixture, and hands the fixture to the parity
test (``test_composition_baseline_parity.py``) as the eternal "what
today produced" reference the composer must keep matching after the
inline flows are removed.

Not a test module — the pytest collector ignores the underscore
prefix. :func:`regenerate_baseline` is invoked manually at
commit-authoring time to refresh the fixture files under
``tests/canonical/fixtures/composition_parity_baseline/``. Running the
capture in CI would silently re-baseline the parity lock, so keep it
out of the automated pipelines.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tests.canonical._docker_compose_stubs import InertDockerCompose, driver_state
from tests.canonical._factories import make_task_description
from tests.conftest import FAKE_CONTAINER_SECRETS
from tests.utils.secret_state import secret_manager_state_restored
from tolokaforge.core import per_trial_runtime as per_trial_runtime_module
from tolokaforge.core import shared_stack_runtime as shared_stack_runtime_module
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.models.task_config import SeedRef
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend, _LocalEnvHandle
from tolokaforge.core.service_readiness import InMemoryServiceReadinessProbe
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import ResetSpec, ServiceSpec
from tolokaforge.runtime import reset_recipes as reset_recipes_module
from tolokaforge.secrets import SecretManager, init_default_from
from tolokaforge.secrets import log_filter as log_filter_module

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "composition_parity_baseline"
_MANIFEST_FIXTURE = (
    Path(__file__).parent / "fixtures" / "environment_manifest" / "safe_two_service.yaml"
)
_RUNNER_SERVICE = "default"
_SEED_KIND = "sql_dump"


# ---------------------------------------------------------------------------
# Doubles — private to the capture helper
# ---------------------------------------------------------------------------


class _RecordingRunnerClient:
    """Non-network :class:`GrpcRunnerClient` stand-in.

    Same shape the parity test uses so the inline flow's
    ``self.runner_client = GrpcRunnerClient(...)`` reaches a benign
    recorder and the ``close`` in ``_close``/``teardown`` path is a
    no-op.
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
    """Non-docker :class:`RecipeDispatcher` — records every dispatch."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def apply(self, seed: SeedRef, service_name: str, compose: Any) -> None:
        del compose
        self.calls.append((seed.kind, service_name))


def _always_ready_loader(kind: str) -> Any:
    """Readiness-probe loader returning an always-ready in-memory probe.

    Both scenarios call the loader through the inline flow's
    ``self.readiness_probe_loader``; substituting here keeps the
    readiness gate from stalling on a real listener.
    """
    del kind
    return lambda: InMemoryServiceReadinessProbe(ok=True)


# ---------------------------------------------------------------------------
# Stub-install context managers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _stub_shared_stack_symbols(
    stub_sink: list[InertDockerCompose],
) -> Iterator[None]:
    """Install the two module-level symbols the inline shared-stack flow
    reads (:data:`DockerCompose`, :data:`GrpcRunnerClient`) with the
    recording stubs, and restore both on exit."""
    orig_compose = shared_stack_runtime_module.DockerCompose
    orig_runner_client = shared_stack_runtime_module.GrpcRunnerClient

    def factory(**kwargs: Any) -> InertDockerCompose:
        stub = InertDockerCompose(**kwargs)
        stub_sink.append(stub)
        return stub

    shared_stack_runtime_module.DockerCompose = factory  # type: ignore[misc]
    shared_stack_runtime_module.GrpcRunnerClient = _RecordingRunnerClient  # type: ignore[assignment]
    try:
        yield
    finally:
        shared_stack_runtime_module.DockerCompose = orig_compose  # type: ignore[misc]
        shared_stack_runtime_module.GrpcRunnerClient = orig_runner_client  # type: ignore[assignment]


@contextlib.contextmanager
def _stub_per_trial_symbols(
    stub_sink: list[InertDockerCompose],
) -> Iterator[None]:
    """Install the two module-level symbols the inline per-trial flow
    reads (:data:`DockerCompose`, :data:`GrpcRunnerClient`) with the
    recording stubs, and restore both on exit."""
    orig_compose = per_trial_runtime_module.DockerCompose
    orig_runner_client = per_trial_runtime_module.GrpcRunnerClient

    def factory(**kwargs: Any) -> InertDockerCompose:
        stub = InertDockerCompose(**kwargs)
        stub_sink.append(stub)
        return stub

    per_trial_runtime_module.DockerCompose = factory  # type: ignore[misc]
    per_trial_runtime_module.GrpcRunnerClient = _RecordingRunnerClient  # type: ignore[assignment]
    try:
        yield
    finally:
        per_trial_runtime_module.DockerCompose = orig_compose  # type: ignore[misc]
        per_trial_runtime_module.GrpcRunnerClient = orig_runner_client  # type: ignore[assignment]


@contextlib.contextmanager
def _install_recipe(seed_kind: str, recipe: _RecordingRecipe) -> Iterator[None]:
    """Temporarily register a :class:`_RecordingRecipe` in
    :data:`RECIPE_REGISTRY` for ``seed_kind`` and restore on exit."""
    prior = reset_recipes_module.RECIPE_REGISTRY.get(seed_kind)
    reset_recipes_module.RECIPE_REGISTRY[seed_kind] = recipe  # type: ignore[assignment]
    try:
        yield
    finally:
        if prior is None:
            reset_recipes_module.RECIPE_REGISTRY.pop(seed_kind, None)
        else:
            reset_recipes_module.RECIPE_REGISTRY[seed_kind] = prior


# ---------------------------------------------------------------------------
# Scenario A — SharedStackRuntimeBackend env_manifest mode
# ---------------------------------------------------------------------------


def _make_shared_manifest() -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_MANIFEST_FIXTURE,
        runner_service=_RUNNER_SERVICE,
        services={
            "db": ServiceSpec(isolation="shared"),
            "default": ServiceSpec(isolation="shared"),
        },
    )


def _capture_scenario_a(out_dir: Path) -> None:
    """Drive today's inline shared-stack materialise and snapshot the
    six observable outputs to ``out_dir``."""
    stubs: list[InertDockerCompose] = []
    manifest = _make_shared_manifest()
    with _stub_shared_stack_symbols(stubs):
        backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-a")
        backend._materialise_manifest()
        try:
            assert backend._temp_dir is not None
            assert backend._endpoints is not None
            temp_dir = backend._temp_dir
            compose_bytes = (temp_dir / _MANIFEST_FIXTURE.name).read_bytes()
            basename_prefix = temp_dir.name.rsplit("-", 1)[0]
            state = driver_state(stubs[0])
            endpoints = backend._endpoints.model_dump(mode="json")
            log_components = [router.component_id for router in backend._compose_log_routers]
        finally:
            backend.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compose.yaml").write_bytes(compose_bytes)
    (out_dir / "driver_state.json").write_text(json.dumps(_tuples_to_lists(state), indent=2))
    (out_dir / "endpoints.json").write_text(json.dumps(endpoints, indent=2))
    (out_dir / "basename_prefix.txt").write_text(basename_prefix)
    (out_dir / "log_components.json").write_text(json.dumps(log_components, indent=2))


# ---------------------------------------------------------------------------
# Scenario B — PerTrialRuntimeBackend
# ---------------------------------------------------------------------------


def _make_reset_manifest() -> EnvironmentManifest:
    return EnvironmentManifest(
        compose_file=_MANIFEST_FIXTURE,
        runner_service=_RUNNER_SERVICE,
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


def _capture_scenario_b(out_dir: Path) -> None:
    """Drive today's inline per-trial provision and snapshot the seven
    observable outputs (six + reset-recipe sequence) to ``out_dir``."""
    stubs: list[InertDockerCompose] = []
    recipe = _RecordingRecipe()
    manifest = _make_reset_manifest()
    seed = _make_seed()
    with _stub_per_trial_symbols(stubs), _install_recipe(_SEED_KIND, recipe):
        backend = PerTrialRuntimeBackend(
            seeds={"baseline": seed},
            readiness_probe_loader=_always_ready_loader,
        )
        spec = _make_trial_spec(manifest)
        handle = backend.provision(spec)
        assert isinstance(handle, _LocalEnvHandle)
        try:
            temp_dir = handle.temp_dir
            compose_bytes = (temp_dir / _MANIFEST_FIXTURE.name).read_bytes()
            env_text = (temp_dir / ".env").read_text()
            basename_prefix = temp_dir.name.rsplit("-", 1)[0]
            state = driver_state(stubs[0])
            endpoints = handle.endpoints.model_dump(mode="json")
            log_components = [router.component_id for router in handle.log_routers]
            recipe_calls = list(recipe.calls)
        finally:
            backend.teardown(handle)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compose.yaml").write_bytes(compose_bytes)
    (out_dir / "env.txt").write_text(env_text)
    (out_dir / "driver_state.json").write_text(json.dumps(_tuples_to_lists(state), indent=2))
    (out_dir / "endpoints.json").write_text(json.dumps(endpoints, indent=2))
    (out_dir / "basename_prefix.txt").write_text(basename_prefix)
    (out_dir / "log_components.json").write_text(json.dumps(log_components, indent=2))
    (out_dir / "recipe_calls.json").write_text(
        json.dumps([list(call) for call in recipe_calls], indent=2)
    )


# ---------------------------------------------------------------------------
# JSON normalisation
# ---------------------------------------------------------------------------


def _tuples_to_lists(value: Any) -> Any:
    """Recursively convert tuples to lists for JSON serialisation.

    :func:`driver_state` returns nested tuples so the composer-side
    equality check has no ambiguity; JSON round-trips them as lists
    either way, so both sides normalise to lists before comparing.
    """
    if isinstance(value, tuple | list):
        return [_tuples_to_lists(v) for v in value]
    if isinstance(value, dict):
        return {k: _tuples_to_lists(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def regenerate_baseline(fixture_root: Path | None = None) -> None:
    """Regenerate the parity baseline fixture files.

    Run manually at commit-authoring time when a legitimate change to
    :meth:`SharedStackRuntimeBackend._materialise_manifest` or
    :meth:`PerTrialRuntimeBackend.provision` intentionally shifts the
    observable output. Every regenerated file must land in the same
    commit as the code change so the parity lock stays truthful.

    Runs under the same pinned :class:`SecretManager`
    :func:`tests.canonical.conftest._pin_fake_secrets` installs for
    every canonical test, so the captured compose file carries the
    deterministic :data:`FAKE_CONTAINER_SECRETS` payload rather than
    whatever secrets the invoking shell happens to have set — the
    committed fixture never contains real credentials.
    """
    root = fixture_root or _FIXTURE_ROOT
    with secret_manager_state_restored():
        init_default_from(SecretManager.from_dict(dict(FAKE_CONTAINER_SECRETS)))
        log_filter_module._cached_manager = None
        log_filter_module._cached_values = frozenset()
        _capture_scenario_a(root / "scenario_a")
        _capture_scenario_b(root / "scenario_b")


if __name__ == "__main__":
    regenerate_baseline()
