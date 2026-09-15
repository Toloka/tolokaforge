"""Unit tests for :class:`PerTrialRuntimeBackend`'s ``expose_substrate`` seam.

Locks the fix for task-declared per-trial runners never registering the
runner's ``SubstrateService`` servicer: ``PerTrialRuntimeBackend`` must
thread its ``expose_substrate`` constructor field all the way to the
:class:`~tolokaforge.core.composition_runtime.MaterialiseContext` the
composer hands the materialiser, so
:func:`~tolokaforge.core.compose_materialisation.inject_substrate_env_into_runner`
fires for that trial's compose stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.composition_runtime import MaterialiseContext, StackHandle
from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.service_readiness import InMemoryServiceReadinessProbe
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.runner.models import StackScope, TaskDescription

pytestmark = pytest.mark.unit


_RUNNER_COMPOSE = (
    'services:\n  runner:\n    image: tolokaforge-runner:local\n    ports:\n      - "50051"\n'
)


@dataclass
class _FakeStackHandle:
    stack_id: str
    stack_scope: StackScope
    runner_service: str | None


@dataclass
class _RecordingMaterialiser:
    """No-docker :class:`ComposeMaterialiser` that snapshots every
    :class:`MaterialiseContext` it receives."""

    contexts: list[MaterialiseContext] = field(default_factory=list)

    def materialise(self, decl: Any, ctx: MaterialiseContext) -> StackHandle:
        self.contexts.append(ctx)
        return _FakeStackHandle(
            stack_id=decl.stack_id,
            stack_scope=decl.stack_scope,
            runner_service=decl.runner_service,
        )

    def resolve_endpoint(
        self, handle: StackHandle, service: str, container_port: int
    ) -> tuple[str, int] | None:
        del handle, service
        return ("127.0.0.1", 60000 + container_port)

    def get_containers(self, handle: StackHandle) -> list[Any]:
        del handle
        return []

    def capture_logs(
        self, handle: StackHandle, services: tuple[str, ...], dest_dir: Path, tail: int
    ) -> dict[str, int]:
        del handle, services, dest_dir, tail
        return {}

    def teardown(self, handle: StackHandle) -> None:
        del handle


class _FakeRunnerClient:
    def __init__(self, runner_address: str, events: Any = None) -> None:
        self.runner_address = runner_address
        self.events = events

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        del timeout, retry_interval

    def close(self) -> None:
        pass

    def health_check(self) -> bool:
        return True


def _fake_client_factory(runner_address: str, events: Any) -> _FakeRunnerClient:
    return _FakeRunnerClient(runner_address=runner_address, events=events)


def _always_ready_loader(kind: str) -> Any:
    del kind
    return lambda: InMemoryServiceReadinessProbe(ok=True)


def _make_backend(
    materialiser: _RecordingMaterialiser, *, expose_substrate: bool
) -> PerTrialRuntimeBackend:
    composer = DefaultSubstrateComposer(
        materialiser=materialiser,
        runner_client_factory=_fake_client_factory,
        readiness_probe_loader=_always_ready_loader,
    )
    return PerTrialRuntimeBackend(expose_substrate=expose_substrate, composer=composer)


def _trial_spec(compose_file: Path) -> TrialSpec:
    manifest = EnvironmentManifest(compose_file=compose_file, runner_service="runner")
    return TrialSpec(
        trial_id="task-1:0",
        run_id="run-a",
        task=TaskDescription(
            task_id="task-1",
            name="task-1",
            category="test",
            description="unit-test stub",
            adapter_type="native",
            system_prompt="",
            environment_manifest=manifest,
        ),
        agent_model_config=ModelConfig(provider="anthropic", name="stub"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:5432", runner_url="http://placeholder:50051"
        ),
    )


class TestExposeSubstratePropagation:
    def test_expose_substrate_true_reaches_materialise_context(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "environment.compose.yaml"
        compose_file.write_text(_RUNNER_COMPOSE)
        materialiser = _RecordingMaterialiser()
        backend = _make_backend(materialiser, expose_substrate=True)

        backend.provision(_trial_spec(compose_file))

        assert materialiser.contexts, "materialise() was never called"
        assert all(ctx.expose_substrate for ctx in materialiser.contexts)

    def test_expose_substrate_false_by_default(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "environment.compose.yaml"
        compose_file.write_text(_RUNNER_COMPOSE)
        materialiser = _RecordingMaterialiser()
        backend = _make_backend(materialiser, expose_substrate=False)

        backend.provision(_trial_spec(compose_file))

        assert materialiser.contexts, "materialise() was never called"
        assert not any(ctx.expose_substrate for ctx in materialiser.contexts)
