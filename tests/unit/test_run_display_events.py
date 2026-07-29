"""Unit tests locking the :class:`RunDisplayEvents` engine seam.

The seam lives in :mod:`tolokaforge.core.run_display_events` so the
engine can import the Protocol without dragging any front-end
dependency graph into worker containers, the gRPC runner, or the
cloud-runtime trial-plane.

These tests lock:

- The Protocol declares exactly the 12 lifecycle methods the engine
  emits: 9 trial/run boundary events plus the in-flight LLM-call trio
  (``llm_call_started`` / ``llm_call_finished`` /
  ``llm_retry_scheduled``).
- Every method is kwarg-only (ADR-0011: field additions must not break
  positional callers).
- :data:`_NULL_EVENTS` / :class:`_NullRunDisplayEvents` are structural
  members of the Protocol and every method no-ops without raising when
  called with its documented kwargs — including the widened
  ``trial_started`` model-identity fields.
- :class:`LLMCallObservation` is a frozen dataclass carrying the seam
  reference + call identity (``trial_id`` + ``role``) that
  ``LLMClient.generate`` will thread through per call.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    ContainerSnapshot,
    LLMCallObservation,
    RateLimitProbeStats,
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
        "llm_call_started",
        "llm_call_finished",
        "llm_retry_scheduled",
        "component_registered",
        "component_status_changed",
        "component_log_appended",
        "component_unregistered",
    }
)


def test_protocol_declares_expected_lifecycle_and_component_methods() -> None:
    declared = {
        name
        for name in vars(RunDisplayEvents)
        if not name.startswith("_") and callable(vars(RunDisplayEvents)[name])
    }
    assert declared == LIFECYCLE_METHODS
    assert len(LIFECYCLE_METHODS) == 16


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


def test_trial_started_accepts_optional_model_identity_kwargs() -> None:
    """``trial_started`` carries ``agent_model`` / ``user_model`` as
    optional-defaulted kwargs so the Rich display can label per-role
    LLM calls without a second lookup — the orchestrator populates them
    from the ``ModelConfig`` in scope at the emission site."""
    signature = inspect.signature(RunDisplayEvents.trial_started)
    params = signature.parameters
    assert "agent_model" in params
    assert "user_model" in params
    assert params["agent_model"].annotation == "str | None"
    assert params["user_model"].annotation == "str | None"
    assert params["agent_model"].default is None
    assert params["user_model"].default is None


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
    _NULL_EVENTS.trial_started(
        trial_id="a:0",
        task_id="a",
        trial_index=0,
        total_index=0,
        agent_model="openai/gpt-4",
        user_model="openai/gpt-4o-mini",
    )
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
    _NULL_EVENTS.llm_call_started(
        trial_id="a:0", role="agent", provider="openai", model="gpt-4", attempt=1
    )
    _NULL_EVENTS.llm_call_finished(
        trial_id="a:0",
        role="agent",
        provider="openai",
        model="gpt-4",
        attempt=1,
        duration_s=0.42,
        error=None,
    )
    _NULL_EVENTS.llm_retry_scheduled(
        trial_id="a:0",
        role="agent",
        provider="openai",
        model="gpt-4",
        attempt=1,
        next_attempt_in_s=4.0,
        reason="Timeout while calling gpt-4",
    )


def test_null_events_trial_started_accepts_legacy_call_without_model_kwargs() -> None:
    """The two new ``trial_started`` model-identity kwargs default to
    ``None`` so any caller that predates the widening keeps working."""
    _NULL_EVENTS.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)


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


def test_llm_call_observation_is_frozen_dataclass_bundling_seam_and_identity() -> None:
    """The per-call context threaded into ``LLMClient.generate`` is a
    frozen dataclass so its bindings never change in flight while a trial's
    worker thread hands it to the client — the client reads
    ``(events, trial_id, role)`` and forwards to the seam, and accumulates
    into the ``probe_stats`` the trial owns."""
    assert dataclasses.is_dataclass(LLMCallObservation)
    assert LLMCallObservation.__dataclass_params__.frozen is True

    field_names = {f.name for f in dataclasses.fields(LLMCallObservation)}
    assert field_names == {"events", "trial_id", "role", "probe_stats"}

    observation = LLMCallObservation(events=_NULL_EVENTS, trial_id="a:0", role="agent")
    assert observation.probe_stats is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        observation.trial_id = "b:0"  # type: ignore[misc]


def test_rate_limit_probe_stats_accumulates_counts_waits_and_window() -> None:
    """Probe accounting sums retries + wait and keeps the first / last 429
    timestamps, so a trial's metrics carry the window the probe was blocked."""
    stats = RateLimitProbeStats()
    assert (stats.retries, stats.wait_s, stats.first_ts, stats.last_ts) == (0, 0.0, None, None)
    assert stats.by_role_model == {}

    stats.record_retry(role="agent", model="openrouter/m", wait_s=15.0, ts=100.0)
    stats.record_retry(role="agent", model="openrouter/m", wait_s=15.0, ts=130.0)

    assert stats.retries == 2
    assert stats.wait_s == 30.0
    assert stats.first_ts == 100.0
    assert stats.last_ts == 130.0


def test_rate_limit_probe_stats_keys_buckets_by_role_and_model() -> None:
    """A trial's roles are different models, so their 429s never share a bucket;
    the flat fields stay the sum across buckets."""
    stats = RateLimitProbeStats()

    stats.record_retry(role="agent", model="openrouter/agent-model", wait_s=15.0, ts=100.0)
    stats.record_retry(role="agent", model="openrouter/agent-model", wait_s=15.0, ts=110.0)
    stats.record_retry(role="user", model="openrouter/user-model", wait_s=5.0, ts=120.0)

    agent = stats.by_role_model[("agent", "openrouter/agent-model")]
    user = stats.by_role_model[("user", "openrouter/user-model")]
    assert (agent.retries, agent.wait_s, agent.first_ts, agent.last_ts) == (2, 30.0, 100.0, 110.0)
    assert (user.retries, user.wait_s, user.first_ts, user.last_ts) == (1, 5.0, 120.0, 120.0)
    assert stats.retries == agent.retries + user.retries == 3
    assert stats.wait_s == agent.wait_s + user.wait_s == 35.0


def test_rate_limit_probe_stats_requires_an_attribution() -> None:
    """``role`` / ``model`` are keyword-required so no 429 can land in an
    unattributable bucket — every call site already knows both."""
    with pytest.raises(TypeError):
        RateLimitProbeStats().record_retry(wait_s=15.0, ts=100.0)  # type: ignore[call-arg]
