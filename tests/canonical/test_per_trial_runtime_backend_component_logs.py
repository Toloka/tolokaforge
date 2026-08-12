"""Canonical tests locking ``PerTrialRuntimeBackend``'s per-trial
compose ``LogRouter`` wiring.

Every container that a per-trial compose stack brings up must have a
``LogRouter`` attached whose ``component_id`` matches what the display
publishes for per-trial containers
(``trial/<trial_id>/container/<service>``). Router teardown must precede
``shutdown_compose`` so the streaming threads exit before their
underlying docker log streams are severed. A provision failure that
happens before the router-build step (compose-up or reset-recipe) must
leave no routers running because none were constructed on that path.

Materialisation runs for real here, credential injection included, so
``_pin_fake_secrets`` pins the manager whose payload reaches the compose file.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._factories import make_task_description
from tolokaforge.core import per_trial_runtime as per_trial_runtime_module
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend, _LocalEnvHandle
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.service_readiness import InMemoryServiceReadinessProbe, ServiceReadinessProbe
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest, TrialSpec
from tolokaforge.docker.logging import LogRouterError
from tolokaforge.runner.models import ResetSpec, ServiceSpec

pytestmark = pytest.mark.canonical


@pytest.fixture(autouse=True)
def _pin_fake_secrets(installed_fake_secrets: dict[str, str]) -> None:
    """Every test here materialises a compose file for real, and materialisation
    injects the process ``SecretManager``'s payload into the runner service.
    Unpinned, the suite would write the host's own credentials into temp compose
    files and take the empty-vs-populated branch by machine."""


_FIXTURES = Path(__file__).parent / "fixtures" / "environment_manifest"


def _ready_loader(kind: str) -> Callable[[], ServiceReadinessProbe]:
    """Readiness-probe loader seam yielding an always-ready in-memory probe, so
    provision's host-side readiness gate passes without a live listener."""
    del kind
    return lambda: InMemoryServiceReadinessProbe(ok=True)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeComposeContainer:
    """Minimal ``ComposeContainer`` stand-in for ``.get_containers()``.

    Only exposes the attributes ``PerTrialRuntimeBackend`` reads when
    constructing a ``LogRouter`` per container.
    """

    def __init__(
        self, *, ID: str, Name: str, Service: str
    ) -> None:  # noqa: N803 — mirrors ComposeContainer field names
        self.ID = ID
        self.Name = Name
        self.Service = Service


class _RecordingRouter:
    """Spy ``LogRouter`` that records lifecycle timestamps.

    Stands in for the real router so tests can assert both construction
    (component id per container) and teardown ordering (every router
    stops before ``shutdown_compose``).
    """

    def __init__(
        self,
        *,
        container_name: str,
        container_id: str,
        component_id: str | None = None,
        events: list[tuple[str, str, float]] | None = None,
        **_kwargs: Any,
    ) -> None:
        self.container_name = container_name
        self.container_id = container_id
        self.component_id = component_id
        self.started = False
        self.stopped = False
        self.start_ts: float | None = None
        self.stop_ts: float | None = None
        self._events = events

    def start(self) -> None:
        self.started = True
        self.start_ts = time.monotonic()
        if self._events is not None:
            self._events.append(("router.start", self.container_name, self.start_ts))

    def stop(self, timeout_s: float = 5.0) -> None:  # noqa: ARG002 — Protocol conformance
        self.stopped = True
        self.stop_ts = time.monotonic()
        if self._events is not None:
            self._events.append(("router.stop", self.container_name, self.stop_ts))


class _FakeCompose:
    """Stand-in for ``testcontainers.compose.DockerCompose``.

    Exposes the seams :class:`PerTrialRuntimeBackend` touches on the
    provisioning path plus ``get_containers`` so the router-attach step
    can enumerate a declared set of containers.
    """

    def __init__(
        self,
        context: str,
        compose_file_name: str,
        pull: bool = False,
        build: bool = False,
        wait: bool = True,
    ) -> None:
        self.context = context
        self.compose_file_name = compose_file_name
        self.pull = pull
        self.build = build
        self.wait = wait
        self.started = False
        self.stopped = False
        self.exposed_services: dict[str, dict[int, int]] = {
            "default": {50051: 50100},
            "db-service": {8000: 58000},
        }
        self.containers: list[_FakeComposeContainer] = []

    def start(self) -> None:
        self.started = True

    def stop(self, down: bool = True) -> None:
        self.stopped = True

    def get_service_host_and_port(
        self, service_name: str, port: int
    ) -> tuple[str | None, int | None]:
        service = self.exposed_services.get(service_name)
        if service is None:
            raise KeyError(service_name)
        host_port = service.get(port)
        if host_port is None:
            raise ValueError(f"{service_name}: port {port} not exposed")
        return ("127.0.0.1", host_port)

    def get_container(self, service_name: str) -> Any:
        if service_name not in self.exposed_services:
            raise KeyError(service_name)
        return _FakeContainer(self.exposed_services[service_name])

    def get_containers(self) -> list[_FakeComposeContainer]:
        return list(self.containers)


class _FakeContainer:
    def __init__(self, ports: dict[int, int]) -> None:
        self.Publishers = [
            _FakePublisher(TargetPort=cp, PublishedPort=hp) for cp, hp in ports.items()
        ]


class _FakePublisher:
    def __init__(self, TargetPort: int, PublishedPort: int) -> None:  # noqa: N803
        self.TargetPort = TargetPort
        self.PublishedPort = PublishedPort


class _FakeRunnerClient:
    """Minimal ``GrpcRunnerClient`` stand-in with a close() spy."""

    def __init__(self, runner_address: str) -> None:
        self.runner_address = runner_address
        self.closed = False

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_recording_router(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, str, float]] | None = None,
    fail_for_service: str | None = None,
) -> list[_RecordingRouter]:
    """Install ``_RecordingRouter`` in place of the real ``LogRouter``.

    Optionally raises ``LogRouterError`` when constructing a router for
    ``fail_for_service`` — used to lock the log-and-continue contract.
    """
    created: list[_RecordingRouter] = []

    def factory(**kwargs: Any) -> _RecordingRouter:
        component_id = kwargs.get("component_id")
        if fail_for_service is not None:
            expected_suffix = f"/container/{fail_for_service}"
            if isinstance(component_id, str) and component_id.endswith(expected_suffix):
                raise LogRouterError("create", kwargs.get("container_name", "?"), "boom")
        router = _RecordingRouter(events=events, **kwargs)
        created.append(router)
        return router

    monkeypatch.setattr(per_trial_runtime_module, "LogRouter", factory)
    return created


def _install_fake_compose_with_containers(
    monkeypatch: pytest.MonkeyPatch,
    containers_by_context: dict[str, list[_FakeComposeContainer]] | None = None,
    default_containers: list[_FakeComposeContainer] | None = None,
) -> list[_FakeCompose]:
    """Patch ``DockerCompose`` with a factory that seeds a fresh
    :class:`_FakeCompose`'s ``containers`` list per invocation.

    ``containers_by_context`` keys off the compose context directory so a
    two-trial concurrency test can seed distinct containers per trial;
    ``default_containers`` is the fallback when no context match is found.
    """
    created: list[_FakeCompose] = []
    ctx_map = containers_by_context or {}
    fallback = list(default_containers or [])

    def factory(**kwargs: Any) -> _FakeCompose:
        compose = _FakeCompose(**kwargs)
        for key, value in ctx_map.items():
            if key in compose.context:
                compose.containers = list(value)
                break
        else:
            compose.containers = list(fallback)
        created.append(compose)
        return compose

    monkeypatch.setattr(per_trial_runtime_module, "DockerCompose", factory)
    monkeypatch.setattr(per_trial_runtime_module, "GrpcRunnerClient", _FakeRunnerClient)
    return created


def _make_trial_spec(
    trial_id: str = "task-1:0",
    compose_file: Path | None = None,
    services: dict[str, ServiceSpec] | None = None,
    manifest: EnvironmentManifest | None = None,
) -> TrialSpec:
    if manifest is not None:
        pass
    elif compose_file is None:
        manifest = None
    elif services is None:
        manifest = EnvironmentManifest(compose_file=compose_file)
    else:
        manifest = EnvironmentManifest(compose_file=compose_file, services=services)
    return TrialSpec(
        trial_id=trial_id,
        run_id="run_component_logs",
        task=make_task_description(
            task_id="task-1",
            name="probe",
            category="general",
            description="Per-trial component-log wiring test",
            environment_manifest=manifest,
        ),
        agent_model_config=ModelConfig(name="claude-sonnet-4-6", provider="anthropic"),
        env_endpoints=EnvEndpoints(
            db_url="http://placeholder:5432",
            runner_url="http://placeholder:50051",
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_provision_attaches_one_router_per_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every compose container gets a ``LogRouter`` whose component id
    matches ``trial/<trial_id>/container/<service>`` — the same namespace
    ``_container_to_component`` publishes, so status row and log tail
    land on the same component id."""
    _install_fake_compose_with_containers(
        monkeypatch,
        default_containers=[
            _FakeComposeContainer(ID="cid-default", Name="proj-default-1", Service="default"),
            _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db"),
        ],
    )
    created = _install_recording_router(monkeypatch)

    backend = PerTrialRuntimeBackend(readiness_probe_loader=_ready_loader)
    spec = _make_trial_spec(trial_id="task-1:0", compose_file=_FIXTURES / "safe_two_service.yaml")
    handle = backend.provision(spec)

    assert isinstance(handle, _LocalEnvHandle)
    assert len(created) == 2
    assert len(handle.log_routers) == 2
    by_component = {r.component_id: r for r in created}
    assert set(by_component.keys()) == {
        "trial/task-1:0/container/default",
        "trial/task-1:0/container/db",
    }
    assert by_component["trial/task-1:0/container/default"].container_id == "cid-default"
    assert by_component["trial/task-1:0/container/db"].container_id == "cid-db"
    assert all(r.started for r in created)


def test_teardown_stops_routers_before_shutdown_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teardown order: every router's ``stop`` timestamp precedes
    ``shutdown_compose``'s call timestamp."""
    _install_fake_compose_with_containers(
        monkeypatch,
        default_containers=[
            _FakeComposeContainer(ID="cid-default", Name="proj-default-1", Service="default"),
            _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db"),
        ],
    )
    events: list[tuple[str, str, float]] = []
    created = _install_recording_router(monkeypatch, events=events)

    shutdown_ts: list[float] = []

    def recording_shutdown(_compose: Any) -> None:
        shutdown_ts.append(time.monotonic())

    monkeypatch.setattr(per_trial_runtime_module, "shutdown_compose", recording_shutdown)

    backend = PerTrialRuntimeBackend(readiness_probe_loader=_ready_loader)
    spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
    handle = backend.provision(spec)
    backend.teardown(handle)

    assert len(shutdown_ts) == 1
    assert len(created) == 2
    for router in created:
        assert router.stopped
        assert router.stop_ts is not None
        assert (
            router.stop_ts < shutdown_ts[0]
        ), f"router {router.container_name!r} stopped after shutdown_compose"


def test_reset_recipe_failure_leaves_no_routers_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provision failure at the reset-recipe stage happens BEFORE the
    router-build step, so no routers were ever constructed on that path
    and there is nothing to stop."""
    _install_fake_compose_with_containers(
        monkeypatch,
        default_containers=[
            _FakeComposeContainer(ID="cid-default", Name="proj-default-1", Service="default"),
        ],
    )
    created = _install_recording_router(monkeypatch)

    backend = PerTrialRuntimeBackend(readiness_probe_loader=_ready_loader)
    # Named seed absent from registry → reset-recipe stage failure.
    spec = _make_trial_spec(
        compose_file=_FIXTURES / "safe_two_service.yaml",
        services={"db": ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline"))},
    )

    with pytest.raises(ProvisionError) as exc:
        backend.provision(spec)

    assert exc.value.stage == "reset_recipe"
    assert created == []
    assert spec.trial_id not in backend._clients


def test_concurrent_trials_get_independent_routers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two independent trials produce routers with distinct component
    ids — one namespace per trial, no cross-trial mixing."""
    _install_fake_compose_with_containers(
        monkeypatch,
        containers_by_context={
            "task-1_0": [
                _FakeComposeContainer(
                    ID="cid-a-default", Name="proj-a-default-1", Service="default"
                ),
            ],
            "task-1_1": [
                _FakeComposeContainer(
                    ID="cid-b-default", Name="proj-b-default-1", Service="default"
                ),
            ],
        },
    )
    created = _install_recording_router(monkeypatch)

    backend = PerTrialRuntimeBackend(readiness_probe_loader=_ready_loader)
    spec_a = _make_trial_spec(trial_id="task-1:0", compose_file=_FIXTURES / "safe_two_service.yaml")
    spec_b = _make_trial_spec(trial_id="task-1:1", compose_file=_FIXTURES / "safe_two_service.yaml")
    handle_a = backend.provision(spec_a)
    handle_b = backend.provision(spec_b)

    assert isinstance(handle_a, _LocalEnvHandle)
    assert isinstance(handle_b, _LocalEnvHandle)
    assert {r.component_id for r in handle_a.log_routers} == {
        "trial/task-1:0/container/default",
    }
    assert {r.component_id for r in handle_b.log_routers} == {
        "trial/task-1:1/container/default",
    }
    # Routers from the two trials share no component id.
    a_ids = {r.component_id for r in handle_a.log_routers}
    b_ids = {r.component_id for r in handle_b.log_routers}
    assert a_ids.isdisjoint(b_ids)
    # And container ids stay separate too.
    assert {r.container_id for r in handle_a.log_routers} == {"cid-a-default"}
    assert {r.container_id for r in handle_b.log_routers} == {"cid-b-default"}
    # All routers ended up on their respective handles.
    assert set(created) == set(handle_a.log_routers) | set(handle_b.log_routers)


def test_provision_logs_and_continues_on_router_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A per-container router construction failure must not abort
    provisioning — the compose stack is already up. Other routers are
    still built and the failure is logged."""
    _install_fake_compose_with_containers(
        monkeypatch,
        default_containers=[
            _FakeComposeContainer(ID="cid-default", Name="proj-default-1", Service="default"),
            _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db"),
        ],
    )
    created = _install_recording_router(monkeypatch, fail_for_service="default")

    backend = PerTrialRuntimeBackend(readiness_probe_loader=_ready_loader)
    spec = _make_trial_spec(trial_id="task-1:0", compose_file=_FIXTURES / "safe_two_service.yaml")

    with caplog.at_level("ERROR"):
        handle = backend.provision(spec)

    assert isinstance(handle, _LocalEnvHandle)
    # ``default`` router raised at construction → not present. ``db`` router
    # was still built.
    assert len(created) == 1
    assert created[0].component_id == "trial/task-1:0/container/db"
    assert handle.log_routers == (created[0],)
    assert any(
        "failed to attach log router" in record.getMessage().lower() for record in caplog.records
    )


def test_container_without_id_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``ComposeContainer`` with an empty ``ID`` cannot be attached to
    (there is no docker container id to stream from); the wiring loop
    skips it silently rather than raising."""
    _install_fake_compose_with_containers(
        monkeypatch,
        default_containers=[
            _FakeComposeContainer(ID="", Name="proj-default-1", Service="default"),
            _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db"),
        ],
    )
    created = _install_recording_router(monkeypatch)

    backend = PerTrialRuntimeBackend(readiness_probe_loader=_ready_loader)
    spec = _make_trial_spec(trial_id="task-1:0", compose_file=_FIXTURES / "safe_two_service.yaml")
    handle = backend.provision(spec)

    assert isinstance(handle, _LocalEnvHandle)
    assert len(created) == 1
    assert created[0].component_id == "trial/task-1:0/container/db"
    assert handle.log_routers == (created[0],)


def test_teardown_swallows_router_stop_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A router-stop error must not mask compose teardown — the same
    fail-safe discipline as the existing client-close ``try/except``."""
    _install_fake_compose_with_containers(
        monkeypatch,
        default_containers=[
            _FakeComposeContainer(ID="cid-default", Name="proj-default-1", Service="default"),
        ],
    )
    created = _install_recording_router(monkeypatch)

    shutdown_calls: list[bool] = []
    monkeypatch.setattr(
        per_trial_runtime_module,
        "shutdown_compose",
        lambda _compose: shutdown_calls.append(True),
    )

    backend = PerTrialRuntimeBackend(readiness_probe_loader=_ready_loader)
    spec = _make_trial_spec(compose_file=_FIXTURES / "safe_two_service.yaml")
    handle = backend.provision(spec)

    def raising_stop(timeout_s: float = 5.0) -> None:  # noqa: ARG001
        raise RuntimeError("router stop boom")

    created[0].stop = raising_stop  # type: ignore[method-assign]

    backend.teardown(handle)

    assert shutdown_calls == [True]
