"""Canonical tests locking ``EngineStack``'s per-service ``LogRouter`` wiring.

Every service materialised by :meth:`EngineStack._start_service` — fresh
create OR reuse of an already-running container — must attach a
``LogRouter`` whose ``component_id`` equals what the panel's
``ServiceSnapshot → ComponentSnapshot`` adapter publishes for the same
service (``engine/docker.service/<svc.name>``). Router teardown must
precede docker container teardown so the panel's per-component tail
stops before the underlying stream is severed.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.docker.container import ContainerStatus
from tolokaforge.docker.image import Image
from tolokaforge.docker.stack import EngineStack, ServiceDefinition

pytestmark = pytest.mark.canonical


class _CallLog:
    """Ordered log of significant lifecycle events across spy objects."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def record(self, kind: str, subject: str) -> None:
        self.events.append((kind, subject))


class _SpyLogRouter:
    """Stand-in for :class:`LogRouter` that records lifecycle calls.

    Mirrors the subset of the router surface :meth:`EngineStack._start_service`
    depends on: ``start``, ``stop``, ``is_running``, ``component_id``.
    """

    def __init__(
        self,
        *,
        component_id: str | None,
        container_name: str,
        call_log: _CallLog,
    ) -> None:
        self.component_id = component_id
        self.container_name = container_name
        self._call_log = call_log
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True
        self._call_log.record("router.start", self.container_name)

    def stop(self, timeout_s: float = 5.0) -> None:
        self._running = False
        self._call_log.record("router.stop", self.container_name)


class _SpyContainer:
    """Stand-in for :class:`Container` that mimics real router-wiring semantics.

    Reproduces two invariants of the real container:

    - :meth:`start` with ``log_router`` attaches the router and starts it.
    - :meth:`stop` stops the attached router BEFORE the docker container
      is asked to stop (this is the ordering ``_start_service`` relies on).
    - :meth:`attach_log_router` starts the router on already-running
      containers and refuses re-attach while another is running.
    """

    def __init__(
        self,
        *,
        name: str,
        image_tag: str,
        container_id: str,
        call_log: _CallLog,
    ) -> None:
        self.name = name
        self.image_tag = image_tag
        self.container_id = container_id
        self.current_status = ContainerStatus.CREATED
        self._log_router: _SpyLogRouter | None = None
        self._call_log = call_log
        self.attach_calls: list[_SpyLogRouter] = []
        self.start_calls: list[_SpyLogRouter | None] = []

    def start(
        self,
        log_router: _SpyLogRouter | None = None,
        trial_id: str | None = None,
        log_file: str | None = None,
    ) -> None:
        self.start_calls.append(log_router)
        self.current_status = ContainerStatus.RUNNING
        if log_router is not None:
            self._log_router = log_router
            log_router.start()

    def stop(self, timeout_s: float = 10.0) -> None:
        if self._log_router is not None:
            self._log_router.stop()
            self._log_router = None
        self._call_log.record("container_docker.stop", self.name)
        self.current_status = ContainerStatus.STOPPED

    def attach_log_router(self, log_router: _SpyLogRouter) -> None:
        if self._log_router is not None and self._log_router.is_running:
            raise RuntimeError(f"A LogRouter is already attached and running on '{self.name}'")
        self.attach_calls.append(log_router)
        self._log_router = log_router
        log_router.start()

    def destroy(self, *, remove_volumes: bool = False) -> None:
        pass


def _fake_image(name: str) -> Image:
    return Image(
        name=name,
        tag="deadbeef",
        image_id="dummy",
        dockerfile=f"tolokaforge/docker/dockerfiles/{name}.Dockerfile",
        context=".",
        context_hash="deadbeef",
    )


def _make_stack_with_service(svc_name: str) -> tuple[EngineStack, ServiceDefinition]:
    svc = ServiceDefinition(
        name=svc_name,
        image_name=f"tolokaforge-{svc_name}",
        dockerfile=f"tolokaforge/docker/dockerfiles/{svc_name}.Dockerfile",
    )
    stack = EngineStack()
    stack.add_service(svc)
    stack._images[svc.name] = _fake_image(f"tolokaforge-{svc_name}")
    return stack, svc


def _patch_router_factory(monkeypatch: pytest.MonkeyPatch, call_log: _CallLog) -> None:
    from tolokaforge.docker import stack as stack_mod

    def fake_for_container(cls: Any, container: Any, **kwargs: Any) -> _SpyLogRouter:
        return _SpyLogRouter(
            component_id=kwargs.get("component_id"),
            container_name=container.name,
            call_log=call_log,
        )

    monkeypatch.setattr(
        stack_mod.LogRouter,
        "for_container",
        classmethod(fake_for_container),
    )


def test_start_service_wires_component_id_on_fresh_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh container path attaches a LogRouter with the canonical component id."""
    call_log = _CallLog()
    stack, svc = _make_stack_with_service("db-service")

    spy_container = _SpyContainer(
        name=f"{stack.prefix}-{svc.name}",
        image_tag=stack._images[svc.name].full_tag,
        container_id="cid-fresh",
        call_log=call_log,
    )

    from tolokaforge.docker import stack as stack_mod

    monkeypatch.setattr(
        stack_mod.Container,
        "create",
        classmethod(lambda *args, **kwargs: spy_container),
    )
    monkeypatch.setattr(
        EngineStack,
        "_try_reuse_existing",
        lambda self, container_name, svc: None,
    )
    _patch_router_factory(monkeypatch, call_log)

    stack._start_service(svc, wait=False)

    assert len(spy_container.start_calls) == 1
    router = spy_container.start_calls[0]
    assert router is not None
    assert router.component_id == "engine/docker.service/db-service"
    assert router.is_running


def test_start_service_wires_component_id_on_reused_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reused container path attaches a LogRouter via ``attach_log_router``."""
    call_log = _CallLog()
    stack, svc = _make_stack_with_service("runner")

    spy_existing = _SpyContainer(
        name=f"{stack.prefix}-{svc.name}",
        image_tag=stack._images[svc.name].full_tag,
        container_id="cid-reuse",
        call_log=call_log,
    )
    spy_existing.current_status = ContainerStatus.RUNNING

    from tolokaforge.docker import stack as stack_mod

    monkeypatch.setattr(
        EngineStack,
        "_try_reuse_existing",
        lambda self, container_name, svc: spy_existing,
    )
    # ``_start_service`` reuses a running container only when the image behind
    # it matches the one the stack holds, and reads that identity off the
    # daemon. Report the stack's own image, so the probe answers for the fake
    # container the reuse stub above just handed back.
    running_image = stack._images[svc.name]
    monkeypatch.setattr(
        stack,
        "_inspect_running_image",
        lambda container_name: ([running_image.full_tag], running_image.image_id),
    )
    monkeypatch.setattr(
        stack_mod.Container,
        "create",
        classmethod(
            lambda *args, **kwargs: pytest.fail(
                "Container.create must not be called on the reuse path"
            )
        ),
    )
    _patch_router_factory(monkeypatch, call_log)

    stack._start_service(svc, wait=False)

    assert len(spy_existing.attach_calls) == 1
    attached = spy_existing.attach_calls[0]
    assert attached.component_id == "engine/docker.service/runner"
    assert attached.is_running
    assert not spy_existing.start_calls, "reuse path must not invoke Container.start"


def test_stop_all_stops_routers_before_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop_all`` teardown stops each router BEFORE its docker container."""
    call_log = _CallLog()
    stack, svc = _make_stack_with_service("db-service")

    spy_container = _SpyContainer(
        name=f"{stack.prefix}-{svc.name}",
        image_tag=stack._images[svc.name].full_tag,
        container_id="cid-fresh",
        call_log=call_log,
    )

    from tolokaforge.docker import stack as stack_mod

    monkeypatch.setattr(
        stack_mod.Container,
        "create",
        classmethod(lambda *args, **kwargs: spy_container),
    )
    monkeypatch.setattr(
        EngineStack,
        "_try_reuse_existing",
        lambda self, container_name, svc: None,
    )
    _patch_router_factory(monkeypatch, call_log)

    stack._start_service(svc, wait=False)
    stack.stop_all()

    events = call_log.events
    router_stop_idx = next(
        i for i, e in enumerate(events) if e == ("router.stop", spy_container.name)
    )
    docker_stop_idx = next(
        i for i, e in enumerate(events) if e == ("container_docker.stop", spy_container.name)
    )
    assert router_stop_idx < docker_stop_idx
