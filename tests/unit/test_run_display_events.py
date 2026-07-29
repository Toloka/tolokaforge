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
- :class:`RateLimitProbeStats` accumulates both censuses of rate-limit
  probe throughput — 429s and successful calls — per ``(role, model)``
  and per fixed-width **absolute-time** window, with a bounded bucket
  count whose drop policy keeps the retained series a contiguous prefix.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest

from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    DEFAULT_PROBE_BUCKET_WIDTH_S,
    DEFAULT_PROBE_MAX_BUCKETS,
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
        # Deliberately missing role/model — the raise is what is under test.
        RateLimitProbeStats().record_retry(wait_s=15.0, ts=100.0)  # type: ignore[call-arg]


def test_rate_limit_probe_stats_requires_an_attribution_for_successes() -> None:
    """Same for the success side: an unattributed success would make per-model
    goodput unrecoverable, which is the whole point of recording it."""
    with pytest.raises(TypeError):
        # Deliberately missing role/model — the raise is what is under test.
        RateLimitProbeStats().record_success(  # type: ignore[call-arg]
            duration_s=1.0, prompt_tokens=1, completion_tokens=1, ts=100.0
        )


# ---------------------------------------------------------------------------
# Goodput: the SUCCESS side of throughput
# ---------------------------------------------------------------------------


_EPOCH = 1_700_000_000.0
"""An epoch second that is deliberately NOT a multiple of the bucket width.

``1_700_000_000 // 30 * 30 == 1_699_999_980``, so any test that accidentally
anchored a bucket on this timestamp instead of on the epoch grid shows up as a
wrong ``bucket_start``.
"""


def test_probe_stats_accumulates_the_success_side() -> None:
    """Goodput needs successful calls, their duration and their tokens; the 429
    census alone is schedule-dependent and, for some providers, silent."""
    stats = RateLimitProbeStats()
    assert (stats.successes, stats.success_duration_s) == (0, 0.0)
    assert (stats.prompt_tokens, stats.completion_tokens) == (0, 0)
    assert stats.by_bucket == {}
    assert stats.dropped_buckets == 0

    stats.record_success(
        role="agent", model="m", duration_s=2.5, prompt_tokens=100, completion_tokens=10, ts=_EPOCH
    )
    stats.record_success(
        role="agent",
        model="m",
        duration_s=3.5,
        prompt_tokens=200,
        completion_tokens=20,
        ts=_EPOCH + 1,
    )

    assert stats.successes == 2
    assert stats.success_duration_s == 6.0
    assert (stats.prompt_tokens, stats.completion_tokens) == (300, 30)
    # The 429 window is NOT moved by successes: rate_limit_first_ts keeps
    # meaning "when was this trial first throttled".
    assert (stats.retries, stats.wait_s, stats.first_ts, stats.last_ts) == (0, 0.0, None, None)


def test_probe_stats_keeps_success_counters_separate_per_role_and_model() -> None:
    """The asymmetric mixed case. The agent is the measured model and the user
    simulator is an unrelated one, so 3-vs-1 calls and 4x-different token
    profiles must stay apart. A shared bucket reports one blended row and the
    per-model goodput this feature exists for becomes unrecoverable."""
    stats = RateLimitProbeStats()
    for i in range(3):
        stats.record_success(
            role="agent",
            model="openrouter/agent-model",
            duration_s=10.0,
            prompt_tokens=369_857,
            completion_tokens=500,
            ts=_EPOCH + i,
        )
    stats.record_success(
        role="user",
        model="openrouter/user-model",
        duration_s=1.0,
        prompt_tokens=89_984,
        completion_tokens=40,
        ts=_EPOCH + 3,
    )

    agent = stats.by_role_model[("agent", "openrouter/agent-model")]
    user = stats.by_role_model[("user", "openrouter/user-model")]
    assert (agent.successes, agent.success_duration_s) == (3, 30.0)
    assert (agent.prompt_tokens, agent.completion_tokens) == (1_109_571, 1500)
    assert (user.successes, user.success_duration_s) == (1, 1.0)
    assert (user.prompt_tokens, user.completion_tokens) == (89_984, 40)
    # A blended counter would report 4 successes / 1_199_555 prompt tokens
    # against whichever model happened to be recorded.
    assert stats.successes == agent.successes + user.successes == 4
    assert stats.prompt_tokens == agent.prompt_tokens + user.prompt_tokens == 1_199_555


def test_probe_stats_keeps_the_two_censuses_on_one_row() -> None:
    """One ``(role, model)`` row carries the served and the rejected side, so a
    consumer reads goodput and the 429 count for the same model together."""
    stats = RateLimitProbeStats()
    stats.record_success(
        role="agent", model="m", duration_s=4.0, prompt_tokens=10, completion_tokens=1, ts=_EPOCH
    )
    stats.record_retry(role="agent", model="m", wait_s=15.0, ts=_EPOCH + 1)

    row = stats.by_role_model[("agent", "m")]
    assert (row.successes, row.success_duration_s) == (1, 4.0)
    assert (row.retries, row.wait_s) == (1, 15.0)
    assert row.first_ts == row.last_ts == _EPOCH + 1


class TestAbsoluteTimeBucketAlignment:
    """The bucket boundary is derived from the Unix epoch, never from run start.

    That is the only reason windows produced by seven simultaneous run legs — in
    seven separate processes, potentially on seven machines — can be summed
    window by window into a global throughput number.
    """

    def test_bucket_start_floors_onto_the_epoch_grid(self) -> None:
        stats = RateLimitProbeStats(bucket_width_s=30)

        # 1_700_000_000 is not on the grid; 1_699_999_980 is.
        assert stats.bucket_start(_EPOCH) == 1_699_999_980
        assert stats.bucket_start(1_699_999_980.0) == 1_699_999_980
        assert stats.bucket_start(1_700_000_009.999) == 1_699_999_980
        assert stats.bucket_start(1_700_000_010.0) == 1_700_000_010

    def test_every_bucket_start_is_a_multiple_of_the_width(self) -> None:
        """The invariant a cross-leg join relies on: starts come from one global
        grid, so two legs cannot land on interleaved boundaries."""
        stats = RateLimitProbeStats(bucket_width_s=30)
        for offset in (0.0, 0.4, 7.0, 29.999, 30.0, 61.5, 3600.0):
            assert stats.bucket_start(_EPOCH + offset) % 30 == 0

    def test_two_independent_stats_objects_agree_on_the_window(self) -> None:
        """Two run legs are two processes with two accumulators. Same instant,
        same window key — which is what makes the per-leg series joinable."""
        leg_a = RateLimitProbeStats(bucket_width_s=30)
        leg_b = RateLimitProbeStats(bucket_width_s=30)
        instant = _EPOCH + 17.25

        leg_a.record_success(
            role="agent",
            model="m",
            duration_s=1.0,
            prompt_tokens=5,
            completion_tokens=1,
            ts=instant,
        )
        leg_b.record_success(
            role="agent",
            model="m",
            duration_s=2.0,
            prompt_tokens=7,
            completion_tokens=2,
            ts=instant,
        )

        assert list(leg_a.by_bucket) == list(leg_b.by_bucket) == [("agent", "m", 1_700_000_010)]

    def test_the_boundary_cannot_depend_on_when_the_run_started(self) -> None:
        """The negative control. Two legs launched 15 s apart see the same instant
        at different offsets from their own start, so a run-start-relative grid
        puts it in different windows (30 vs 0 here) and the legs cannot be summed.

        The accumulator has no run-start or creation-time state at all — every
        field is a counter or a config knob — so ``bucket_start`` is a pure
        function of ``(ts, bucket_width_s)`` and cannot drift into that bug.
        """
        instant = _EPOCH + 40.0
        leg_a_start, leg_b_start = _EPOCH, _EPOCH + 15.0

        relative_a = int((instant - leg_a_start) // 30) * 30
        relative_b = int((instant - leg_b_start) // 30) * 30
        assert (relative_a, relative_b) == (30, 0)

        field_names = {f.name for f in dataclasses.fields(RateLimitProbeStats)}
        assert not {name for name in field_names if "start" in name or "created" in name}
        assert RateLimitProbeStats(bucket_width_s=30).bucket_start(instant) == RateLimitProbeStats(
            bucket_width_s=30
        ).bucket_start(instant)

    def test_successive_calls_land_in_successive_windows(self) -> None:
        """Non-stationarity is only visible if calls separated by more than a
        window width are recorded separately — measured goodput decays at a
        constant offered concurrency (``docs/OUTPUT_FORMAT.md`` § Field
        observations)."""
        stats = RateLimitProbeStats(bucket_width_s=30)
        for i in range(3):
            stats.record_success(
                role="agent",
                model="m",
                duration_s=1.0,
                prompt_tokens=10,
                completion_tokens=1,
                ts=_EPOCH + i * 30,
            )

        starts = sorted(start for (_role, _model, start) in stats.by_bucket)
        assert starts == [1_699_999_980, 1_700_000_010, 1_700_000_040]
        assert all(counters.successes == 1 for counters in stats.by_bucket.values())
        # The cumulative total is unchanged — the windows are an extra view, not
        # a replacement.
        assert stats.successes == 3

    def test_buckets_are_keyed_by_role_and_model_as_well_as_time(self) -> None:
        """Two roles active in the same window keep separate rows, so a leg's
        agent throughput is never inflated by its simulator's."""
        stats = RateLimitProbeStats(bucket_width_s=30)
        stats.record_success(
            role="agent",
            model="a",
            duration_s=9.0,
            prompt_tokens=900,
            completion_tokens=90,
            ts=_EPOCH,
        )
        stats.record_success(
            role="user",
            model="u",
            duration_s=1.0,
            prompt_tokens=100,
            completion_tokens=10,
            ts=_EPOCH,
        )

        assert sorted(stats.by_bucket) == [
            ("agent", "a", 1_699_999_980),
            ("user", "u", 1_699_999_980),
        ]
        assert stats.by_bucket[("agent", "a", 1_699_999_980)].prompt_tokens == 900
        assert stats.by_bucket[("user", "u", 1_699_999_980)].prompt_tokens == 100

    def test_retries_and_successes_share_a_window(self) -> None:
        """The served and rejected sides of one interval sit on one row, which is
        what makes a rejection *rate* per window computable."""
        stats = RateLimitProbeStats(bucket_width_s=30)
        stats.record_success(
            role="agent",
            model="m",
            duration_s=2.0,
            prompt_tokens=10,
            completion_tokens=1,
            ts=_EPOCH,
        )
        stats.record_retry(role="agent", model="m", wait_s=15.0, ts=_EPOCH + 5)

        (window,) = stats.by_bucket.values()
        assert (window.successes, window.retries) == (1, 1)

    def test_the_width_is_configurable(self) -> None:
        stats = RateLimitProbeStats(bucket_width_s=120)
        assert stats.bucket_start(_EPOCH) == 1_699_999_920
        assert stats.bucket_start(_EPOCH) % 120 == 0

    def test_the_defaults_are_the_documented_ones(self) -> None:
        stats = RateLimitProbeStats()
        assert (stats.bucket_width_s, stats.max_buckets) == (
            DEFAULT_PROBE_BUCKET_WIDTH_S,
            DEFAULT_PROBE_MAX_BUCKETS,
        )
        assert DEFAULT_PROBE_BUCKET_WIDTH_S == 30
        # 480 windows per (role, model) for a 4 h run at 30 s; the cap has to
        # sit far above that or a legal run would truncate.
        assert DEFAULT_PROBE_MAX_BUCKETS >= 2 * (4 * 3600 // DEFAULT_PROBE_BUCKET_WIDTH_S)

    def test_the_documented_capacity_is_per_series_not_per_trial(self) -> None:
        """A bucket is one ``(role, model, window)`` row, so the two-role default
        consumes two rows per window and reaches the cap in HALF the wall time a
        single-role trial would. The docs state both figures."""
        single_role_h = DEFAULT_PROBE_MAX_BUCKETS * DEFAULT_PROBE_BUCKET_WIDTH_S / 3600
        two_role_h = single_role_h / 2

        assert round(single_role_h, 1) == 34.1
        assert round(two_role_h, 1) == 17.1


class TestBucketCapDropPolicy:
    """Memory is bounded, and the truncation is never silent.

    Refusing to open a *new* window (rather than evicting an old one) keeps the
    retained series a contiguous prefix in absolute time. A series with a hole
    would let a cross-leg window-by-window sum silently undercount.
    """

    def _stats(self) -> RateLimitProbeStats:
        return RateLimitProbeStats(bucket_width_s=30, max_buckets=2)

    def _success(self, stats: RateLimitProbeStats, ts: float) -> None:
        stats.record_success(
            role="agent", model="m", duration_s=1.0, prompt_tokens=10, completion_tokens=1, ts=ts
        )

    def test_recording_into_an_existing_window_is_never_refused(self) -> None:
        stats = self._stats()
        self._success(stats, _EPOCH)
        self._success(stats, _EPOCH + 30)
        # Cap reached, but both of these land in windows that already exist.
        self._success(stats, _EPOCH + 1)
        self._success(stats, _EPOCH + 31)

        assert len(stats.by_bucket) == 2
        assert stats.dropped_buckets == 0
        assert sum(w.successes for w in stats.by_bucket.values()) == 4

    def test_a_new_window_past_the_cap_is_dropped_and_counted(self) -> None:
        stats = self._stats()
        self._success(stats, _EPOCH)
        self._success(stats, _EPOCH + 30)
        self._success(stats, _EPOCH + 60)

        assert len(stats.by_bucket) == 2
        assert stats.dropped_buckets == 1

    def test_the_retained_series_is_a_contiguous_prefix(self) -> None:
        stats = self._stats()
        for i in range(6):
            self._success(stats, _EPOCH + i * 30)

        starts = sorted(start for (_role, _model, start) in stats.by_bucket)
        assert starts == [1_699_999_980, 1_700_000_010]
        # Earliest windows kept, later ones dropped — no hole in the middle.
        assert stats.dropped_buckets == 4

    def test_dropped_totals_still_reach_the_flat_and_per_model_counters(self) -> None:
        """A dropped window loses the *time resolution* of those calls, never the
        calls themselves — the cumulative records stay complete."""
        stats = self._stats()
        for i in range(6):
            self._success(stats, _EPOCH + i * 30)

        assert stats.successes == 6
        assert stats.prompt_tokens == 60
        assert stats.by_role_model[("agent", "m")].successes == 6
        assert sum(w.successes for w in stats.by_bucket.values()) == 2

    def test_repeated_drops_inside_one_window_count_once(self) -> None:
        """``dropped_buckets`` counts refused ``(role, model, window)`` *rows*,
        not refused recordings, so the number reads as "how many series entries
        are missing"."""
        stats = self._stats()
        self._success(stats, _EPOCH)
        self._success(stats, _EPOCH + 30)
        # All three fall in the window [1_700_000_040, 1_700_000_070).
        for offset in (60.0, 61.0, 65.0):
            self._success(stats, _EPOCH + offset)

        assert stats.dropped_buckets == 1
        assert stats.successes == 5

    def test_a_dropped_window_is_counted_per_role_and_model(self) -> None:
        """The counter's unit, pinned: ONE lost window across TWO roles is **2**,
        because two ``(role, model, window)`` rows were refused.

        That is the same unit as ``max_buckets`` (a cap on
        ``len(by_bucket)``), which is why the count is rows rather than windows —
        ``dropped_buckets + len(by_bucket)`` is then commensurable, and a consumer
        filtering the series to one role gets a number in its own unit. Divide by
        the role count to read it as windows.
        """
        stats = self._stats()
        self._success(stats, _EPOCH)
        self._success(stats, _EPOCH + 30)
        distinct_windows_lost = 1
        roles = (("agent", "m"), ("user", "u"))
        for role, model in roles:
            stats.record_success(
                role=role,  # type: ignore[arg-type]  # LLMCallRole literal, from the table
                model=model,
                duration_s=1.0,
                prompt_tokens=1,
                completion_tokens=1,
                ts=_EPOCH + 60,
            )

        assert stats.dropped_buckets == len(roles) * distinct_windows_lost == 2

    def test_the_cap_is_global_so_one_role_can_starve_the_other(self) -> None:
        """``max_buckets`` bounds ``len(by_bucket)`` across every series, not each
        series, so a high-volume role can consume the whole budget and leave a
        low-volume role with no window rows at all — while its cumulative
        ``by_role_model`` row stays complete. ``dropped_buckets`` says how many
        rows were refused, never which series lost them."""
        stats = RateLimitProbeStats(bucket_width_s=30, max_buckets=3)
        for i in range(3):
            self._success(stats, _EPOCH + i * 30)
        stats.record_success(
            role="user", model="u", duration_s=1.0, prompt_tokens=7, completion_tokens=1, ts=_EPOCH
        )

        assert {role for (role, _model, _start) in stats.by_bucket} == {"agent"}
        assert stats.by_role_model[("user", "u")].successes == 1
        assert stats.by_role_model[("user", "u")].prompt_tokens == 7
        assert stats.dropped_buckets == 1

    def test_a_dropped_429_window_is_counted_too(self) -> None:
        stats = self._stats()
        self._success(stats, _EPOCH)
        self._success(stats, _EPOCH + 30)
        stats.record_retry(role="agent", model="m", wait_s=15.0, ts=_EPOCH + 60)

        assert stats.dropped_buckets == 1
        # The 429 itself is still fully accounted for.
        assert (stats.retries, stats.wait_s) == (1, 15.0)
        assert stats.by_role_model[("agent", "m")].retries == 1
