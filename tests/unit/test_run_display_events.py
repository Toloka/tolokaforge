"""Unit tests locking the :class:`RunDisplayEvents` engine seam.

The seam is the pluggable front-end attach point designated by ADR-0019.
It lives in :mod:`tolokaforge.core.run_display_events` so the engine can
import the Protocol without dragging any front-end dependency graph into
worker containers, the gRPC runner, or the cloud-runtime trial-plane.

These tests lock:

- The Protocol declares exactly the 9 lifecycle methods the engine will
  emit.
- Every method is kwarg-only (ADR-0011: field additions must not break
  positional callers).
- :data:`_NULL_EVENTS` / :class:`_NullRunDisplayEvents` are structural
  members of the Protocol and every method no-ops without raising when
  called with its documented kwargs.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    ContainerSnapshot,
    RunDisplayEvents,
    ServiceSnapshot,
    _NullRunDisplayEvents,
)

pytestmark = pytest.mark.unit


LIFECYCLE_METHODS: frozenset[str] = frozenset(
    {
        "run_started",
        "trial_started",
        "trial_progress",
        "trial_completed",
        "trial_failed",
        "judgment_scored",
        "run_finished",
        "phase_changed",
        "trial_provisioned",
    }
)


def test_protocol_declares_exactly_nine_lifecycle_methods() -> None:
    declared = {
        name
        for name in vars(RunDisplayEvents)
        if not name.startswith("_") and callable(vars(RunDisplayEvents)[name])
    }
    assert declared == LIFECYCLE_METHODS


@pytest.mark.parametrize("method_name", sorted(LIFECYCLE_METHODS))
def test_protocol_methods_are_kwarg_only(method_name: str) -> None:
    method = getattr(RunDisplayEvents, method_name)
    parameters = inspect.signature(method).parameters
    non_self = [p for name, p in parameters.items() if name != "self"]
    assert non_self, f"{method_name} declares no arguments beyond self"
    for param in non_self:
        kind = param.kind
        message = f"{method_name}.{param.name} must be keyword-only (ADR-0011)"
        assert kind is inspect.Parameter.KEYWORD_ONLY, message


def test_null_run_display_events_satisfies_protocol() -> None:
    assert isinstance(_NullRunDisplayEvents(), RunDisplayEvents)


def test_null_events_singleton_is_a_null_run_display_events() -> None:
    assert isinstance(_NULL_EVENTS, _NullRunDisplayEvents)
    assert isinstance(_NULL_EVENTS, RunDisplayEvents)


def test_null_events_calls_do_not_raise_with_documented_kwargs() -> None:
    services: list[ServiceSnapshot] = [
        {"name": "db", "status": "running", "ports": {5432: 55432}, "role": "engine"},
    ]
    containers: list[ContainerSnapshot] = [
        {
            "name": "trial-abc_db_1",
            "service": "db",
            "state": "running",
            "health": "healthy",
            "ports": {5432: 55433},
        },
    ]

    _NULL_EVENTS.run_started(total_trials=3, initial_completed=0)
    _NULL_EVENTS.phase_changed(phase="starting_services", detail=None, services=services)
    _NULL_EVENTS.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    _NULL_EVENTS.trial_provisioned(
        trial_id="a:0",
        containers=containers,
        endpoints={"db": "postgresql://localhost:55432/db"},
    )
    _NULL_EVENTS.trial_progress(
        trial_id="a:0",
        prompt_tokens_delta=10,
        completion_tokens_delta=5,
        cost_delta_usd=0.001,
    )
    _NULL_EVENTS.judgment_scored(trial_id="a:0", score=0.5, binary_pass=False)
    _NULL_EVENTS.trial_completed(trial_id="a:0", binary_pass=True, score=1.0)
    _NULL_EVENTS.trial_failed(trial_id="a:1", error="LLMApiTimeoutError", retryable=False)
    _NULL_EVENTS.run_finished(output_dir=Path("/tmp/output"))


def test_null_events_methods_accept_only_keyword_arguments() -> None:
    sink = _NullRunDisplayEvents()
    with pytest.raises(TypeError):
        sink.run_started(3, 0)  # type: ignore[call-arg]


def test_service_snapshot_shape_is_typed_dict() -> None:
    snapshot: ServiceSnapshot = {
        "name": "runner",
        "status": "running",
        "ports": {50051: 50051},
        "role": "engine",
    }
    assert set(snapshot.keys()) == {"name", "status", "ports", "role"}


def test_container_snapshot_shape_is_typed_dict() -> None:
    snapshot: ContainerSnapshot = {
        "name": "trial-abc_db_1",
        "service": "db",
        "state": "running",
        "health": None,
        "ports": {},
    }
    assert set(snapshot.keys()) == {"name", "service", "state", "health", "ports"}
