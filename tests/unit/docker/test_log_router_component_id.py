"""Locks ``LogRouter``'s opt-in ``component_id`` extras contract.

The field must be additive: when unset, emitted records match the
pre-existing extras set byte-for-byte; when set, every record carries
``record.component_id`` so a display's ``_LogSink`` can route the record
into that component's tail buffer.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.docker.logging import LogRouter

pytestmark = pytest.mark.unit


def _make_router(**overrides: object) -> LogRouter:
    defaults: dict[str, object] = {
        "container_name": "svc",
        "container_id": "abc123",
    }
    defaults.update(overrides)
    return LogRouter(**defaults)  # type: ignore[arg-type]


def test_extras_omit_component_id_by_default() -> None:
    router = _make_router()

    extra = router._get_extra_fields()

    assert "component_id" not in extra
    assert extra == {"container_name": "svc"}


def test_extras_omit_component_id_when_explicitly_none() -> None:
    router = _make_router(component_id=None, trial_id="trial-42")

    extra = router._get_extra_fields()

    assert "component_id" not in extra
    assert extra == {"container_name": "svc", "trial_id": "trial-42"}


def test_extras_include_component_id_when_set() -> None:
    router = _make_router(
        component_id="engine/docker.service/runner",
        trial_id="trial-42",
    )

    extra = router._get_extra_fields()

    assert extra["component_id"] == "engine/docker.service/runner"
    assert extra == {
        "container_name": "svc",
        "trial_id": "trial-42",
        "component_id": "engine/docker.service/runner",
    }


def test_for_container_passes_component_id_through() -> None:
    container = MagicMock()
    container.container_id = "cid-1"
    container.name = "runner"

    router = LogRouter.for_container(
        container,
        component_id="engine/docker.service/runner",
    )

    assert router.component_id == "engine/docker.service/runner"
    assert router._get_extra_fields()["component_id"] == "engine/docker.service/runner"


def test_stream_forwards_component_id_on_emitted_records() -> None:
    router = _make_router(
        container_name="runner-stream",
        component_id="engine/docker.service/runner-stream",
    )
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture(level=logging.DEBUG)
    stream_logger = logging.getLogger("container.runner-stream")
    prior_level = stream_logger.level
    stream_logger.setLevel(logging.DEBUG)
    stream_logger.addHandler(capture)

    fake_container = MagicMock()
    fake_container.logs.return_value = iter([b"first line\n", b"second line\n"])
    fake_client = MagicMock()
    fake_client.containers.get.return_value = fake_container

    try:
        with patch("docker.from_env", return_value=fake_client):
            router._stream_logs()
    finally:
        stream_logger.removeHandler(capture)
        stream_logger.setLevel(prior_level)

    assert len(records) == 2
    for record in records:
        assert getattr(record, "component_id", None) == ("engine/docker.service/runner-stream")
        assert record.levelno == router.log_level


def test_stream_omits_component_id_attribute_when_field_is_none() -> None:
    router = _make_router(container_name="runner-plain")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture(level=logging.DEBUG)
    stream_logger = logging.getLogger("container.runner-plain")
    prior_level = stream_logger.level
    stream_logger.setLevel(logging.DEBUG)
    stream_logger.addHandler(capture)

    fake_container = MagicMock()
    fake_container.logs.return_value = iter([b"only line\n"])
    fake_client = MagicMock()
    fake_client.containers.get.return_value = fake_container

    try:
        with patch("docker.from_env", return_value=fake_client):
            router._stream_logs()
    finally:
        stream_logger.removeHandler(capture)
        stream_logger.setLevel(prior_level)

    assert len(records) == 1
    assert "component_id" not in records[0].__dict__
