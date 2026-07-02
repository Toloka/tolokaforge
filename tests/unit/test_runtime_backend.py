"""Unit tests for ``tolokaforge.core.runtime``.

Covers the in-memory backend's own shape and the call log dataclass.
Cross-implementation parity is in
``tests/canonical/test_runtime_backend_contract.py``.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.runtime import (
    InMemoryRuntimeBackend,
    RuntimeBackendCallLog,
)

pytestmark = pytest.mark.unit


class TestRuntimeBackendCallLog:
    def test_default_fields_are_empty(self) -> None:
        log = RuntimeBackendCallLog()
        assert log.connect_calls == []
        assert log.close_calls == 0
        assert log.health_check_calls == 0
        assert log.cleanup_trial_calls == []

    def test_equality_holds_for_identical_state(self) -> None:
        assert RuntimeBackendCallLog() == RuntimeBackendCallLog()

    def test_inequality_when_call_lists_diverge(self) -> None:
        a = RuntimeBackendCallLog()
        b = RuntimeBackendCallLog()
        a.cleanup_trial_calls.append("x:0")
        assert a != b


class TestInMemoryRuntimeBackendConstruction:
    def test_fresh_backend_has_a_call_log(self) -> None:
        backend = InMemoryRuntimeBackend()
        assert isinstance(backend.call_log, RuntimeBackendCallLog)

    def test_fresh_backend_call_log_is_empty(self) -> None:
        backend = InMemoryRuntimeBackend()
        assert backend.call_log.connect_calls == []
        assert backend.call_log.close_calls == 0

    def test_each_backend_has_independent_call_log(self) -> None:
        a = InMemoryRuntimeBackend()
        b = InMemoryRuntimeBackend()
        a.cleanup_trial("x:0")
        assert a.call_log.cleanup_trial_calls == ["x:0"]
        assert b.call_log.cleanup_trial_calls == []
