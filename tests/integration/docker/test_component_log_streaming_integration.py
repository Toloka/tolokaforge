"""End-to-end coverage for :class:`LogRouter` -> component-tail routing.

Exercises the full log-streaming stack against a real Docker daemon:

* start a lightweight ``alpine`` container that emits a deterministic
  stdout line (``HELLO-<uuid>``) and then idles;
* attach a :class:`LogRouter` with a known ``component_id``;
* under a :class:`LiveRunDisplay` context (Rich Live and the keyboard
  listener are stubbed out so the test has no TTY requirement), poll
  ``_component_log_buffers[component_id]`` until the emitted line lands
  or the 5 s deadline expires;
* stop the router and assert its background thread joins within 5 s.

Docker cleanup runs unconditionally in ``finally``.

Run this opt-in::

    scripts/with_env.sh uv run pytest \
        tests/integration/docker/test_component_log_streaming_integration.py \
        -m integration -v
"""

from __future__ import annotations

import logging
import time
import uuid

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.core.run_display_events import build_component_id
from tolokaforge.docker.logging import LogRouter
from tolokaforge.dx.live_panel import LiveRunDisplay

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.requires_docker]

_ALPINE_IMAGE = "alpine:3.20"
_LINE_ARRIVAL_TIMEOUT_S = 5.0
_THREAD_JOIN_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 0.05


class _NoopLive:
    console = type(
        "_C",
        (),
        {"print": staticmethod(lambda *a, **k: None)},
    )()

    def __enter__(self) -> _NoopLive:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _NoopKeyboard:
    def __init__(self, *_a: object, **_kw: object) -> None:
        pass

    def __enter__(self) -> _NoopKeyboard:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
def test_container_stdout_reaches_component_tail_and_router_thread_exits() -> None:
    import docker

    client = docker.from_env()
    marker = f"HELLO-{uuid.uuid4()}"
    component_id = build_component_id(
        "engine", "docker.service", "test-component-log-streaming-integ"
    )

    container = client.containers.run(
        _ALPINE_IMAGE,
        command=["sh", "-c", f"echo {marker}; sleep 30"],
        detach=True,
        remove=False,
        name=f"tolokaforge-log-streaming-{uuid.uuid4().hex[:8]}",
    )
    router: LogRouter | None = None

    import tolokaforge.dx.live_panel as live_panel_module

    saved_make_live = live_panel_module.make_live
    saved_keyboard = live_panel_module._KeyboardListener
    live_panel_module.make_live = lambda *a, **kw: _NoopLive()  # type: ignore[assignment]
    live_panel_module._KeyboardListener = _NoopKeyboard  # type: ignore[assignment]

    display = LiveRunDisplay()

    try:
        router = LogRouter(
            container_name=container.name or "unknown",
            container_id=container.id,
            log_level=logging.INFO,
            component_id=component_id,
        )

        with display:
            router.start()
            deadline = time.monotonic() + _LINE_ARRIVAL_TIMEOUT_S
            observed: list[tuple[float, str, str]] = []
            while time.monotonic() < deadline:
                tail = display._component_log_buffers.get(component_id)
                if tail is not None:
                    observed = [entry for entry in tail if marker in entry[2]]
                    if observed:
                        break
                time.sleep(_POLL_INTERVAL_S)

            assert observed, (
                f"marker {marker!r} did not reach component tail "
                f"{component_id!r} within {_LINE_ARRIVAL_TIMEOUT_S}s"
            )
            _ts, level, message = observed[0]
            assert level == "INFO"
            assert marker in message
    finally:
        try:
            if router is not None:
                thread = router._thread
                router.stop(timeout_s=_THREAD_JOIN_TIMEOUT_S)
                if thread is not None:
                    thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
                    thread_alive = thread.is_alive()
                    assert not thread_alive, "LogRouter thread did not exit after stop()"
        finally:
            live_panel_module.make_live = saved_make_live  # type: ignore[assignment]
            live_panel_module._KeyboardListener = saved_keyboard  # type: ignore[assignment]
            try:
                container.stop(timeout=1)
            except Exception:
                pass
            try:
                container.remove(force=True)
            except Exception:
                pass
