"""Canonical tests locking ``SharedStackRuntimeBackend``'s task-declared
compose stack ``LogRouter`` wiring.

Every container the task-declared compose stack brings up must have a
``LogRouter`` attached whose ``component_id`` matches what the display
publishes for engine services (``engine/docker.service/<service>``).
Router teardown must precede ``shutdown_compose`` so the streaming
thread stops before its underlying docker log stream is severed.

Built-in-shared-stack mode (``env_manifest`` is ``None``) never enters
``_materialise_manifest``, so ``close()`` must handle an empty router
list as a no-op.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest
from tolokaforge.docker.logging import LogRouterError

pytestmark = pytest.mark.canonical


def _make_manifest(tmp_path: Path) -> EnvironmentManifest:
    """Author a minimal valid manifest whose compose file exists on disk."""
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
    return EnvironmentManifest(compose_file=compose_file, runner_service="runner")


class _FakeComposeContainer:
    """Minimal ``ComposeContainer`` stand-in for ``.get_containers()``.

    Only exposes the attributes ``SharedStackRuntimeBackend`` reads when
    constructing a ``LogRouter`` per container.
    """

    def __init__(
        self, *, ID: str, Name: str, Service: str
    ) -> None:  # noqa: N803 — matches ComposeContainer field names
        self.ID = ID
        self.Name = Name
        self.Service = Service


class _RecordingRouter:
    """Spy ``LogRouter`` that records lifecycle timestamps.

    Stands in for the real router so the test can assert both construction
    (component id per container) and teardown ordering (every router stops
    before ``shutdown_compose``).
    """

    instances: list[_RecordingRouter] = []

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


def _patch_materialise(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compose: MagicMock,
    endpoints: EnvEndpoints,
    runner_endpoint: tuple[str, int] | None = ("localhost", 60051),
) -> None:
    """Patch the compose-materialisation seams so ``connect()`` runs without docker."""
    from tolokaforge.core import shared_stack_runtime as module

    monkeypatch.setattr(module, "DockerCompose", lambda **_: compose)
    monkeypatch.setattr(module, "copy_compose_context", lambda src, dst: None)
    monkeypatch.setattr(module, "apply_network_policy_to_compose_file", lambda *a, **k: None)
    monkeypatch.setattr(module, "inject_runner_credentials", lambda *a, **k: None)
    monkeypatch.setattr(module, "resolve_runner_endpoint", lambda *a, **k: runner_endpoint)
    monkeypatch.setattr(module, "resolve_env_endpoints", lambda *a, **k: endpoints)
    monkeypatch.setattr(module, "GrpcRunnerClient", lambda **_: MagicMock())


def _install_recording_router(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, str, float]] | None = None,
    fail_for_service: str | None = None,
) -> list[_RecordingRouter]:
    """Install ``_RecordingRouter`` in place of the real ``LogRouter``.

    Optionally raises a ``LogRouterError`` when constructing a router for
    ``fail_for_service`` — used to lock the log-and-continue contract.
    """
    from tolokaforge.core import shared_stack_runtime as module

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


def test_materialise_attaches_one_router_per_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every compose container gets a ``LogRouter`` whose component id
    matches ``engine/docker.service/<service>`` — same namespace the
    built-in stack uses, so a task compose declaring a ``runner`` service
    lands on the same row regardless of the deployment mode."""
    manifest = _make_manifest(tmp_path)
    backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="cid-runner", Name="proj-runner-1", Service="runner"),
        _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db-service"),
    ]
    endpoints = EnvEndpoints(
        db_url="http://localhost:65432",
        rag_url=None,
        runner_url="http://localhost:60051",
    )
    _patch_materialise(monkeypatch, compose=fake_compose, endpoints=endpoints)
    created = _install_recording_router(monkeypatch)

    backend.connect()

    assert len(created) == 2
    assert len(backend._compose_log_routers) == 2
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
    """Teardown order: every router's ``stop`` timestamp precedes
    ``shutdown_compose``'s call timestamp."""
    manifest = _make_manifest(tmp_path)
    backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="cid-runner", Name="proj-runner-1", Service="runner"),
        _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db-service"),
    ]
    endpoints = EnvEndpoints(db_url="http://x", rag_url=None, runner_url="http://y")
    events: list[tuple[str, str, float]] = []
    _patch_materialise(monkeypatch, compose=fake_compose, endpoints=endpoints)
    created = _install_recording_router(monkeypatch, events=events)

    shutdown_ts: list[float] = []

    def recording_shutdown(_compose: Any) -> None:
        shutdown_ts.append(time.monotonic())

    from tolokaforge.core import shared_stack_runtime as module

    monkeypatch.setattr(module, "shutdown_compose", recording_shutdown)

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
    # State cleared so a follow-up close() is a no-op.
    assert backend._compose_log_routers == []


def test_close_is_noop_when_router_list_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Built-in-shared-stack mode never materialises the manifest — the
    router list stays empty. ``close()`` must handle that as a no-op and
    must not attempt to call ``.stop()`` on nothing."""
    # Built-in mode: no env_manifest, so _materialise_manifest is never called.
    backend = SharedStackRuntimeBackend()
    assert backend._compose_log_routers == []

    # A second close() after the first must also be a no-op — locks
    # idempotency of the router-teardown block.
    backend.close()
    backend.close()
    assert backend._compose_log_routers == []

    # Also cover the env_manifest branch when close() runs before
    # connect() (no materialisation happened yet).
    manifest = _make_manifest(tmp_path)
    deferred = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")
    assert deferred._compose_log_routers == []
    deferred.close()
    assert deferred._compose_log_routers == []


def test_materialise_logs_and_continues_on_router_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A per-container router construction failure must not abort
    provisioning — the compose stack is already up. Other routers are
    still built and the failure is logged."""
    manifest = _make_manifest(tmp_path)
    backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="cid-runner", Name="proj-runner-1", Service="runner"),
        _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db-service"),
    ]
    endpoints = EnvEndpoints(db_url="http://x", rag_url=None, runner_url="http://y")
    _patch_materialise(monkeypatch, compose=fake_compose, endpoints=endpoints)
    created = _install_recording_router(monkeypatch, fail_for_service="runner")

    with caplog.at_level("ERROR"):
        backend.connect()

    # runner router raised at construction → not present. db-service router
    # was still built.
    assert len(created) == 1
    assert created[0].component_id == "engine/docker.service/db-service"
    assert backend._compose_log_routers == [created[0]]
    assert any(
        "failed to attach log router" in record.getMessage().lower() for record in caplog.records
    )


def test_container_without_id_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A ``ComposeContainer`` with an empty ``ID`` cannot be attached to
    (there is no docker container id to stream from); the wiring loop
    skips it silently rather than raising."""
    manifest = _make_manifest(tmp_path)
    backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="", Name="proj-runner-1", Service="runner"),
        _FakeComposeContainer(ID="cid-db", Name="proj-db-1", Service="db-service"),
    ]
    endpoints = EnvEndpoints(db_url="http://x", rag_url=None, runner_url="http://y")
    _patch_materialise(monkeypatch, compose=fake_compose, endpoints=endpoints)
    created = _install_recording_router(monkeypatch)

    backend.connect()

    assert len(created) == 1
    assert created[0].component_id == "engine/docker.service/db-service"


def test_close_swallows_router_stop_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A router-stop error must not mask compose teardown — the same
    fail-safe discipline the existing ``try/finally`` uses for the
    runner-client close."""
    manifest = _make_manifest(tmp_path)
    backend = SharedStackRuntimeBackend(env_manifest=manifest, run_id="run-x")

    fake_compose = MagicMock()
    fake_compose.get_containers.return_value = [
        _FakeComposeContainer(ID="cid-runner", Name="proj-runner-1", Service="runner"),
    ]
    endpoints = EnvEndpoints(db_url="http://x", rag_url=None, runner_url="http://y")
    _patch_materialise(monkeypatch, compose=fake_compose, endpoints=endpoints)
    created = _install_recording_router(monkeypatch)

    from tolokaforge.core import shared_stack_runtime as module

    shutdown_calls: list[bool] = []
    monkeypatch.setattr(
        module,
        "shutdown_compose",
        lambda _compose: shutdown_calls.append(True),
    )

    backend.connect()

    # Poison the sole router's stop() to raise; compose teardown must
    # still run and the router list must still be cleared.
    def raising_stop(timeout_s: float = 5.0) -> None:  # noqa: ARG001
        raise RuntimeError("router stop boom")

    created[0].stop = raising_stop  # type: ignore[method-assign]

    backend.close()

    assert shutdown_calls == [True]
    assert backend._compose_log_routers == []
