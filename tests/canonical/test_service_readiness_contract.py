"""Contract for the ``ServiceReadinessProbe`` seam and its entry-point registry.

Pins the Protocol method surface + signature, the ``ResolvedEndpoint`` /
``ReadinessResult`` value shapes, the ``InMemoryServiceReadinessProbe`` call-log
and failure-knob semantics, and the fail-loud policy of the fourth registry
group — reusing the loader's ``_clear_discovery_cache`` with injected fake entry
points so the group's policy is exercised without an installed plug-in.
"""

from __future__ import annotations

import importlib.metadata
import inspect
from typing import get_type_hints

import pytest

from tolokaforge.core import plugin_registry
from tolokaforge.core.plugin_registry import (
    SERVICE_READINESS_PROBES_GROUP,
    DuplicateRegistrationError,
    UnknownImplementationError,
    available_readiness_probes,
    load_readiness_probe,
)
from tolokaforge.core.service_readiness import (
    InMemoryServiceReadinessProbe,
    ReadinessResult,
    ResolvedEndpoint,
    ServiceReadinessProbe,
)

pytestmark = pytest.mark.canonical


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    def __init__(
        self,
        name: str,
        *,
        factory: object = None,
        load_error: Exception | None = None,
        dist: str = "pkg",
    ) -> None:
        self.name = name
        self.dist = _FakeDist(dist)
        self._factory = factory
        self._load_error = load_error

    def load(self) -> object:
        if self._load_error is not None:
            raise self._load_error
        return self._factory


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_registry._clear_discovery_cache()
    yield
    plugin_registry._clear_discovery_cache()


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch, entry_points: list[_FakeEntryPoint]
) -> None:
    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        return list(entry_points) if group == SERVICE_READINESS_PROBES_GROUP else []

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


# --- Protocol + value shapes -------------------------------------------------


def test_protocol_is_runtime_checkable_over_the_fixture() -> None:
    assert isinstance(InMemoryServiceReadinessProbe(), ServiceReadinessProbe)


def test_probe_method_signature() -> None:
    sig = inspect.signature(ServiceReadinessProbe.probe)
    assert list(sig.parameters) == ["self", "endpoint", "timeout"]
    assert sig.parameters["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.return_annotation == "ReadinessResult"


def test_resolved_endpoint_field_shape() -> None:
    assert get_type_hints(ResolvedEndpoint) == {"host": str, "port": int}
    endpoint = ResolvedEndpoint(host="127.0.0.1", port=50051)
    with pytest.raises(AttributeError):
        endpoint.host = "other"  # type: ignore[misc]


def test_readiness_result_field_shape() -> None:
    assert get_type_hints(ReadinessResult) == {
        "ok": bool,
        "latency_s": float,
        "detail": str | None,
    }
    result = ReadinessResult(ok=True, latency_s=0.01)
    assert result.detail is None
    with pytest.raises(AttributeError):
        result.ok = False  # type: ignore[misc]


# --- InMemoryServiceReadinessProbe semantics ---------------------------------


def test_in_memory_probe_records_every_call() -> None:
    probe = InMemoryServiceReadinessProbe()
    endpoint = ResolvedEndpoint(host="svc", port=8080)

    probe.probe(endpoint, timeout=2.5)

    assert len(probe.call_log.calls) == 1
    call = probe.call_log.calls[0]
    assert call.endpoint == endpoint
    assert call.timeout == 2.5


def test_in_memory_probe_defaults_to_ok() -> None:
    result = InMemoryServiceReadinessProbe().probe(ResolvedEndpoint("svc", 1), timeout=1.0)
    assert result == ReadinessResult(ok=True, latency_s=0.0, detail=None)


def test_in_memory_probe_ok_shorthand_fails() -> None:
    result = InMemoryServiceReadinessProbe(ok=False).probe(ResolvedEndpoint("svc", 1), timeout=1.0)
    assert result.ok is False


def test_in_memory_probe_fail_detail_knob() -> None:
    probe = InMemoryServiceReadinessProbe(fail_detail="unreachable")
    result = probe.probe(ResolvedEndpoint("svc", 1), timeout=1.0)
    assert result.ok is False
    assert result.detail == "unreachable"


def test_in_memory_probe_result_knob_takes_precedence() -> None:
    forced = ReadinessResult(ok=False, latency_s=9.0, detail="forced")
    probe = InMemoryServiceReadinessProbe(ok=True, result=forced, fail_detail="ignored")
    assert probe.probe(ResolvedEndpoint("svc", 1), timeout=1.0) is forced


# --- Fail-loud registry contract for the fourth group ------------------------


def test_unknown_kind_lists_known_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_points(
        monkeypatch,
        [_FakeEntryPoint("grpc", factory=object()), _FakeEntryPoint("http", factory=object())],
    )

    with pytest.raises(UnknownImplementationError) as excinfo:
        load_readiness_probe("missing")

    assert excinfo.value.known == ["grpc", "http"]
    assert SERVICE_READINESS_PROBES_GROUP in str(excinfo.value)


def test_duplicate_kind_fails_naming_both_distributions(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_entry_points(
        monkeypatch,
        [
            _FakeEntryPoint("grpc", factory=object(), dist="first-pkg"),
            _FakeEntryPoint("grpc", factory=object(), dist="second-pkg"),
        ],
    )

    with pytest.raises(DuplicateRegistrationError) as excinfo:
        load_readiness_probe("grpc")
    assert excinfo.value.distributions == ("first-pkg", "second-pkg")

    with pytest.raises(DuplicateRegistrationError):
        available_readiness_probes()


def test_broken_import_propagates_and_spares_healthy_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_factory = object()
    boom = ImportError("missing dependency")
    _install_entry_points(
        monkeypatch,
        [
            _FakeEntryPoint("tcp", factory=good_factory),
            _FakeEntryPoint("grpc", load_error=boom),
        ],
    )

    assert load_readiness_probe("tcp") is good_factory
    with pytest.raises(ImportError):
        load_readiness_probe("grpc")
