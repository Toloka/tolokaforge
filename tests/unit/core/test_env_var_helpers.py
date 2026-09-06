"""Locks ``tolokaforge.core.env_var`` — env → default resolution + failure paths.

``parse_env_positive_float`` and ``parse_env_non_negative_int`` are shared
helpers used by the LLM client (for API-call timeouts) and by the
orchestrator (for the runner health-check budget). Both share the same
contract: missing env → ``default``; invalid or out-of-band value → log a
warning through the caller-provided logger and return ``default`` (never
raise — an operational override cannot take a running trial down).
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.env_var import parse_env_non_negative_int, parse_env_positive_float

pytestmark = pytest.mark.unit


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def warning(self, msg: str, **fields: Any) -> None:
        self.warnings.append((msg, fields))


# ---------------------------------------------------------------------------
# parse_env_positive_float
# ---------------------------------------------------------------------------


def test_positive_float_missing_env_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXAMPLE_FLOAT", raising=False)
    log = _RecordingLogger()
    assert parse_env_positive_float("EXAMPLE_FLOAT", default=42.5, logger=log) == 42.5
    assert log.warnings == []


def test_positive_float_valid_env_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_FLOAT", "12.75")
    log = _RecordingLogger()
    assert parse_env_positive_float("EXAMPLE_FLOAT", default=1.0, logger=log) == 12.75
    assert log.warnings == []


@pytest.mark.parametrize("value", ["0", "-3.2", "not-a-number", ""])
def test_positive_float_invalid_env_falls_back_and_logs(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EXAMPLE_FLOAT", value)
    log = _RecordingLogger()
    assert parse_env_positive_float("EXAMPLE_FLOAT", default=7.0, logger=log) == 7.0
    assert len(log.warnings) == 1
    _msg, fields = log.warnings[0]
    assert fields["env_var"] == "EXAMPLE_FLOAT"
    assert fields["value"] == value
    assert fields["default"] == 7.0


def test_positive_float_default_none_is_returnable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXAMPLE_FLOAT", raising=False)
    log = _RecordingLogger()
    assert parse_env_positive_float("EXAMPLE_FLOAT", default=None, logger=log) is None


# ---------------------------------------------------------------------------
# parse_env_non_negative_int
# ---------------------------------------------------------------------------


def test_non_negative_int_missing_env_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXAMPLE_INT", raising=False)
    log = _RecordingLogger()
    assert parse_env_non_negative_int("EXAMPLE_INT", default=3, logger=log) == 3
    assert log.warnings == []


def test_non_negative_int_zero_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_INT", "0")
    log = _RecordingLogger()
    assert parse_env_non_negative_int("EXAMPLE_INT", default=1, logger=log) == 0


@pytest.mark.parametrize("value", ["-1", "1.5", "not-an-int", ""])
def test_non_negative_int_invalid_env_falls_back_and_logs(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("EXAMPLE_INT", value)
    log = _RecordingLogger()
    assert parse_env_non_negative_int("EXAMPLE_INT", default=4, logger=log) == 4
    assert len(log.warnings) == 1
