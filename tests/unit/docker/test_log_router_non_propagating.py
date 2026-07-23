"""Locks the non-propagating ``container`` logger contract.

``LogRouter`` pins ``container.propagate = False`` so a running router
never leaks its stdout/stderr to the root logger's handlers under any
display mode. A subscriber (e.g. :class:`LiveRunDisplay._LogSink`)
attaches a handler on the ``container`` parent logger to surface those
records through the panel; without the subscriber, records drop
silently.
"""

from __future__ import annotations

import logging

import pytest

from tolokaforge.docker.logging import LogRouter
from tolokaforge.dx.live_panel import LiveRunDisplay

pytestmark = pytest.mark.unit


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_container_parent_logger_has_propagate_false_after_router_init() -> None:
    LogRouter(container_name="init-check", container_id="cid-init")

    assert logging.getLogger("container").propagate is False


def test_container_logger_does_not_propagate_to_root() -> None:
    router = LogRouter(container_name="isolated", container_id="cid-iso")
    root = logging.getLogger()
    capture = _ListHandler()
    prior_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(capture)
    try:
        router._logger.setLevel(logging.INFO)
        router._logger.info("isolated line", extra=router._get_extra_fields())
    finally:
        root.removeHandler(capture)
        root.setLevel(prior_level)

    assert capture.records == []


def test_livedisplay_receives_container_logs_after_attach() -> None:
    router = LogRouter(
        container_name="paneled",
        container_id="cid-panel",
        component_id="engine/docker.service/paneled",
    )
    display = LiveRunDisplay()

    # Enter/exit without touching Rich Live's terminal: LiveRunDisplay's
    # __enter__ starts a Live context on stdout, so run the test under a
    # non-TTY sink by monkey-patching the Live context to a no-op.
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

    def _make_noop_live(*_args: object, **_kwargs: object) -> _NoopLive:
        return _NoopLive()

    class _NoopKeyboard:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __enter__(self) -> _NoopKeyboard:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    import tolokaforge.dx.live_panel as live_panel_module

    saved_make_live = live_panel_module.make_live
    saved_keyboard = live_panel_module._KeyboardListener
    live_panel_module.make_live = _make_noop_live  # type: ignore[assignment]
    live_panel_module._KeyboardListener = _NoopKeyboard  # type: ignore[assignment]

    try:
        with display:
            router._logger.setLevel(logging.INFO)
            router._logger.info(
                "hello from container",
                extra=router._get_extra_fields(),
            )
            tail = display._component_log_buffers.get("engine/docker.service/paneled")
            assert tail is not None
            entries = list(tail)
    finally:
        live_panel_module.make_live = saved_make_live  # type: ignore[assignment]
        live_panel_module._KeyboardListener = saved_keyboard  # type: ignore[assignment]

    assert len(entries) == 1
    _ts, level, message = entries[0]
    assert level == "INFO"
    assert message == "hello from container"


def test_livedisplay_detaches_container_sink_on_exit() -> None:
    LogRouter(container_name="cleanup", container_id="cid-cleanup")
    display = LiveRunDisplay()

    import tolokaforge.dx.live_panel as live_panel_module

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

    saved_make_live = live_panel_module.make_live
    saved_keyboard = live_panel_module._KeyboardListener
    live_panel_module.make_live = lambda *a, **kw: _NoopLive()  # type: ignore[assignment]
    live_panel_module._KeyboardListener = _NoopKeyboard  # type: ignore[assignment]

    container_logger = logging.getLogger("container")
    baseline = list(container_logger.handlers)

    try:
        with display:
            assert len(container_logger.handlers) == len(baseline) + 1
        after = list(container_logger.handlers)
    finally:
        live_panel_module.make_live = saved_make_live  # type: ignore[assignment]
        live_panel_module._KeyboardListener = saved_keyboard  # type: ignore[assignment]

    assert after == baseline
