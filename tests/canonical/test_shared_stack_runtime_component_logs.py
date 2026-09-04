"""Canonical tests locking log-router wiring on
:class:`SharedStackRuntimeBackend`'s env_manifest path.

Every container the run-scope compose stack brings up carries a
:class:`LogRouter` on the private ``_DockerComposeStackHandle`` the
materialiser produces (post-refactor: the router set lives on the
handle, not on the backend). The component id matches what the display
publishes for engine services (``engine/docker.service/<service>``).
Router teardown must precede compose shutdown so the streaming thread
stops before its underlying docker log stream is severed.

Built-in-stack mode (``env_manifest`` is ``None``) never materialises
the run substrate, so ``close()`` must be a no-op for the run-scope
router set.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.docker_compose_materialiser import _DockerComposeStackHandle
from tolokaforge.core.project_loader import _synthesise_composition_plan
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.docker.logging import LogRouterError
from tolokaforge.runner.models import ServiceSpec

pytestmark = pytest.mark.canonical


def _make_manifest(tmp_path: Path) -> EnvironmentManifest:
    """Author a minimal valid manifest whose compose file exists on disk.

    All services labelled ``shared`` so ``_synthesise_composition_plan``
    infers the SINGLE_RUN plan shape (one run-scope stack owning the
    runner) the component-logs contract exercises.
    """
    compose_file = tmp_path / "environment.compose.yaml"
    compose_file.write_text(
        "services:\n"
        "  runner:\n"
        "    image: tolokaforge-runner:local\n"
        "    ports:\n"
        '      - "50051"\n'
        "  db-service:\n"
        "    image: tolokaforge-db-service:local\n"
        "    ports:\n"
        '      - "8000"\n'
    )
    manifest = EnvironmentManifest(
        compose_file=compose_file,
        runner_service="runner",
        services={
            "runner": ServiceSpec(isolation="shared"),
            "db-service": ServiceSpec(isolation="shared"),
        },
    )
    _synthesise_composition_plan(manifest, {})
    return manifest


class _FakeComposeContainer:
    """Minimal ``ComposeContainer`` stand-in for ``.get_containers()``.

    Only exposes the attributes the materialiser reads when constructing
    a :class:`LogRouter` per container.
    """

    def __init__(
        self, *, ID: str, Name: str, Service: str
    ) -> None:  # noqa: N803 — matches ComposeContainer field names
        self.ID = ID
        self.Name = Name
        self.Service = Service


class _RecordingRouter:
    """Spy ``LogRouter`` that records lifecycle timestamps.

    Stands in for the real router so the test can assert both
    construction (component id per container) and teardown ordering
    (every router stops before compose shutdown).
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


def _install_recording_router(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, str, float]] | None = None,
    fail_for_service: str | None = None,
) -> list[_RecordingRouter]:
    """Install :class:`_RecordingRouter` in place of the real
    ``LogRouter`` on the materialiser module.

    Optionally raises a :class:`LogRouterError` when constructing a
    router for ``fail_for_service`` — used to lock the log-and-continue
    contract.
    """
    from tolokaforge.core import docker_compose_materialiser as module

    created: list[_RecordingRouter] = []

    def factory(**kwargs: Any) -> _RecordingRouter:
        component_id = kwargs.get("component_id")
        if fail_for_service is not None and component_id == (
            f"engine/docker.service/{fail_for_service}"
        ):
            raise LogRouterError("create", kwargs.get("container_name", "?"), "boom")
        router = _RecordingRouter(events=events, **kwargs)
        created.append(router)
        return router

    monkeypatch.setattr(module, "LogRouter", factory)
    return created


def _install_fake_compose_factory(
    monkeypatch: pytest.MonkeyPatch,
    fake_compose: MagicMock,
) -> None:
    """Route the materialiser's docker-compose factory through a fake so
    the test never touches a real docker daemon."""
    from tolokaforge.core import docker_compose_materialiser as module

    monkeypatch.setattr(module, "DockerCompose", lambda **_: fake_compose)


def _patch_resolvers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the materialiser's endpoint resolvers at the test's
    published-port stub. The default port sentinel ``60051`` mirrors
    what the pre-refactor tests used."""
    from tolokaforge.core import docker_compose_materialiser as module

    monkeypatch.setattr(module, "resolve_host_port", lambda *_a, **_kw: ("localhost", 60051))


def _connect_run_scope(
    monkeypatch: pytest.MonkeyPatch,
    manifest: EnvironmentManifest,
    fake_compose: MagicMock,
) -> SharedStackRuntimeBackend:
    """Wire the backend end-to-end for a run-scope-materialise scenario.

    Returns a backend whose ``connect()`` succeeds against a stubbed
    docker daemon; the composer's runner-client factory is replaced with
    a MagicMock so the run-scope client connect is a no-op.
    """
    from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer
    from tolokaforge.core.docker_compose_materialiser import DockerComposeMaterialiser

    _install_fake_compose_factory(monkeypatch, fake_compose)
    _patch_resolvers(monkeypatch)
    composer = DefaultSubstrateComposer(
        materialiser=DockerComposeMaterialiser(docker_compose_factory=lambda **_: fake_compose),
        runner_client_factory=lambda _addr, _events: MagicMock(),
    )
    backend = SharedStackRuntimeBackend(
        env_manifest=manifest,
        run_id="run-x",
        composer=composer,
    )
    return backend


def _run_scope_handle(backend: SharedStackRuntimeBackend) -> _DockerComposeStackHandle:
    """Return the sole run-scope :class:`_DockerComposeStackHandle` after connect()."""
    assert backend._run_substrate is not None
    handle = backend._run_substrate.run_stack_handles[0]
    assert isinstance(handle, _DockerComposeStackHandle)
    return handle


def test_materialise_attaches_one_router_per_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every compose container gets a ``LogRouter`` whose component id
    matches ``engine/docker.service/<service>`` — same namespace the
    built-in stack uses, so a task compose declaring a ``runner``
    service lands on the same row regardless of the deployment mode.
    """
    manifest = _make_manifest(tmp_path)

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="cid-runner", Name="proj-runner-1", Service="runner"),
        _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db-service"),
    ]
    created = _install_recording_router(monkeypatch)
    backend = _connect_run_scope(monkeypatch, manifest, fake_compose)

    backend.connect()

    assert len(created) == 2
    handle = _run_scope_handle(backend)
    assert len(handle.log_routers) == 2
    by_component = {r.component_id: r for r in created}
    assert set(by_component.keys()) == {
        "engine/docker.service/runner",
        "engine/docker.service/db-service",
    }
    assert by_component["engine/docker.service/runner"].container_id == "cid-runner"
    assert by_component["engine/docker.service/db-service"].container_id == "cid-db"
    assert all(r.started for r in created)


def test_close_stops_routers_before_shutdown_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Teardown order: every router's ``stop`` timestamp precedes the
    materialiser's ``shutdown_compose`` call timestamp."""
    manifest = _make_manifest(tmp_path)

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="cid-runner", Name="proj-runner-1", Service="runner"),
        _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db-service"),
    ]
    events: list[tuple[str, str, float]] = []
    created = _install_recording_router(monkeypatch, events=events)

    from tolokaforge.core import docker_compose_materialiser as module

    shutdown_ts: list[float] = []
    monkeypatch.setattr(
        module, "shutdown_compose", lambda _compose: shutdown_ts.append(time.monotonic())
    )

    backend = _connect_run_scope(monkeypatch, manifest, fake_compose)
    backend.connect()
    backend.close()

    assert len(shutdown_ts) == 1
    assert len(created) == 2
    for router in created:
        assert router.stopped
        assert router.stop_ts is not None
        assert (
            router.stop_ts < shutdown_ts[0]
        ), f"router {router.container_name!r} stopped after shutdown_compose"
    assert backend._run_substrate is None


def test_close_is_noop_when_no_run_substrate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Built-in-stack mode never materialises the run substrate — the
    router set is empty by construction. ``close()`` must handle that
    without touching the composer's ``teardown_run``."""
    del monkeypatch  # not used; the two scenarios are structural
    backend = SharedStackRuntimeBackend()

    backend.close()
    backend.close()  # second call must not raise either

    manifest = _make_manifest(tmp_path)
    deferred = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")
    assert deferred._run_substrate is None
    deferred.close()
    assert deferred._run_substrate is None


def test_materialise_logs_and_continues_on_router_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A per-container router construction failure must not abort
    provisioning — the compose stack is already up. Other routers are
    still built and the failure is logged."""
    manifest = _make_manifest(tmp_path)

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="cid-runner", Name="proj-runner-1", Service="runner"),
        _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db-service"),
    ]
    created = _install_recording_router(monkeypatch, fail_for_service="runner")
    backend = _connect_run_scope(monkeypatch, manifest, fake_compose)

    with caplog.at_level("ERROR"):
        backend.connect()

    # runner router raised at construction → not present. db-service
    # router was still built.
    assert len(created) == 1
    assert created[0].component_id == "engine/docker.service/db-service"
    handle = _run_scope_handle(backend)
    assert handle.log_routers == (created[0],)
    assert any(
        "failed to attach log router" in record.getMessage().lower() for record in caplog.records
    )


def test_container_without_id_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ``ComposeContainer`` with an empty ``ID`` cannot be attached to
    (there is no docker container id to stream from); the wiring loop
    skips it silently rather than raising."""
    manifest = _make_manifest(tmp_path)

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="", Name="proj-runner-1", Service="runner"),
        _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db-service"),
    ]
    created = _install_recording_router(monkeypatch)
    backend = _connect_run_scope(monkeypatch, manifest, fake_compose)

    backend.connect()

    assert len(created) == 1
    assert created[0].component_id == "engine/docker.service/db-service"


def test_close_swallows_router_stop_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A router-stop error must not mask compose teardown — the same
    fail-safe discipline the materialiser applies internally."""
    manifest = _make_manifest(tmp_path)

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="cid-runner", Name="proj-runner-1", Service="runner"),
    ]
    created = _install_recording_router(monkeypatch)

    from tolokaforge.core import docker_compose_materialiser as module

    shutdown_calls: list[bool] = []
    monkeypatch.setattr(module, "shutdown_compose", lambda _compose: shutdown_calls.append(True))

    backend = _connect_run_scope(monkeypatch, manifest, fake_compose)
    backend.connect()

    def raising_stop(timeout_s: float = 5.0) -> None:  # noqa: ARG001
        raise RuntimeError("router stop boom")

    created[0].stop = raising_stop  # type: ignore[method-assign]

    backend.close()

    assert shutdown_calls == [True]
    assert backend._run_substrate is None
