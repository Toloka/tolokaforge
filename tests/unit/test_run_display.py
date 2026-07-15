"""Unit tests locking :class:`LiveRunDisplay` and :class:`RunDisplayEvents`.

Every assertion here maps to a documented decision (D1–D11) in the plan
``docs/plans/2026-07-15-issue-285-b1-rich-live-progress-panel.md`` or to
the contract laid out in ``tolokaforge/dx/live_panel.py``.
"""

from __future__ import annotations

import io
import itertools
import logging
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tolokaforge.core.logging import _TOLOKAFORGE_ROOT_HANDLER_SENTINEL
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    RunDisplayEvents,
    _NullRunDisplayEvents,
)
from tolokaforge.dx._display import DisplayMode
from tolokaforge.dx.live_panel import (
    LiveRunDisplay,
    _BottomBarStats,
    _format_bottom_bar,
    _NoopDisplayCtx,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


def test_null_run_display_events_satisfies_protocol() -> None:
    assert isinstance(_NullRunDisplayEvents(), RunDisplayEvents)


def test_live_run_display_satisfies_protocol() -> None:
    assert isinstance(LiveRunDisplay(), RunDisplayEvents)


def test_protocol_declares_eight_lifecycle_methods() -> None:
    expected = {
        "run_started",
        "trial_started",
        "trial_progress",
        "trial_completed",
        "trial_failed",
        "judgment_scored",
        "run_finished",
        "phase_changed",
    }
    declared = {
        name
        for name in vars(RunDisplayEvents)
        if not name.startswith("_") and callable(vars(RunDisplayEvents)[name])
    }
    # `RunDisplayEvents` inherits from Protocol which contributes some dunders;
    # the visible surface must equal the eight lifecycle methods.
    assert declared == expected


def test_null_events_is_a_null_run_display_events_instance() -> None:
    assert isinstance(_NULL_EVENTS, _NullRunDisplayEvents)


# ---------------------------------------------------------------------------
# `for_mode` activation gate (D4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [DisplayMode.RICH, DisplayMode.FULL])
def test_for_mode_returns_live_display_for_active_modes(mode: DisplayMode) -> None:
    ctx = LiveRunDisplay.for_mode(mode)
    assert isinstance(ctx, LiveRunDisplay)


@pytest.mark.parametrize("mode", [DisplayMode.PLAIN, DisplayMode.LOG, DisplayMode.NONE])
def test_for_mode_returns_noop_ctx_for_passive_modes(mode: DisplayMode) -> None:
    ctx = LiveRunDisplay.for_mode(mode)
    assert isinstance(ctx, _NoopDisplayCtx)
    assert ctx.events is _NULL_EVENTS


def test_noop_ctx_supports_context_manager_shape() -> None:
    with LiveRunDisplay.for_mode(DisplayMode.PLAIN) as ctx:
        # Every Protocol method is a no-op — must not raise.
        ctx.events.run_started(total_trials=1, initial_completed=0)
        ctx.events.trial_started(trial_id="a:0", task_id="a", trial_index=0)
        ctx.events.trial_progress(
            trial_id="a:0",
            prompt_tokens_delta=10,
            completion_tokens_delta=5,
            cost_delta_usd=0.001,
        )
        ctx.events.trial_completed(trial_id="a:0", binary_pass=True, score=1.0)
        ctx.events.trial_failed(trial_id="a:0", error="oops", retryable=False)
        ctx.events.judgment_scored(trial_id="a:0", score=0.5, binary_pass=False)
        ctx.events.run_finished(output_dir=Path("/tmp/x"))


# ---------------------------------------------------------------------------
# Event-to-card state transitions
# ---------------------------------------------------------------------------


def test_run_started_seeds_counters() -> None:
    display = LiveRunDisplay()
    display.run_started(total_trials=50, initial_completed=0)
    assert display._total_trials == 50
    assert display._initial_completed == 0
    assert display._completed == 0
    assert display._run_start_ts is not None


def test_run_started_with_resume_sets_completed_head_start() -> None:
    display = LiveRunDisplay()
    display.run_started(total_trials=50, initial_completed=12)
    assert display._completed == 12
    assert display._initial_completed == 12


def test_trial_started_creates_running_card_and_focuses_it() -> None:
    display = LiveRunDisplay()
    display.run_started(total_trials=10, initial_completed=0)
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0)
    card = display._trials["a:0"]
    assert card.status == "running"
    assert card.last_event_kind == "started"
    assert card.task_id == "a"
    assert card.trial_index == 0
    assert display._running == 1
    assert display._focused_trial_id == "a:0"


def test_trial_progress_accumulates_and_does_not_bump_last_update_ts() -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0)
    ts_after_start = display._trials["a:0"].last_update_ts
    display.trial_progress(
        trial_id="a:0",
        prompt_tokens_delta=1200,
        completion_tokens_delta=340,
        cost_delta_usd=0.008,
    )
    card = display._trials["a:0"]
    assert card.prompt_tokens == 1200
    assert card.completion_tokens == 340
    assert card.cost_usd == pytest.approx(0.008)
    assert card.turn_count == 1
    assert card.last_event_kind == "progress"
    # D7 — trial_progress does NOT bump last_update_ts.
    assert card.last_update_ts == ts_after_start
    # Run-level cumulative counters bumped.
    assert display._prompt_tokens == 1200
    assert display._completion_tokens == 340
    assert display._total_cost_usd == pytest.approx(0.008)


def test_trial_completed_updates_status_and_bumps_last_update_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0)
    ts_after_start = display._trials["a:0"].last_update_ts
    _install_incrementing_now(monkeypatch, start=ts_after_start + timedelta(seconds=1))
    display.trial_completed(trial_id="a:0", binary_pass=True, score=0.85)
    card = display._trials["a:0"]
    assert card.status == "completed"
    assert card.binary_pass is True
    assert card.score == 0.85
    assert card.last_event_kind == "completed"
    assert card.last_update_ts > ts_after_start
    assert display._running == 0
    assert display._completed == 1
    assert display._focused_trial_id == "a:0"


def test_trial_failed_updates_status(monkeypatch: pytest.MonkeyPatch) -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0)
    ts_after_start = display._trials["b:0"].last_update_ts
    _install_incrementing_now(monkeypatch, start=ts_after_start + timedelta(seconds=1))
    display.trial_failed(trial_id="b:0", error="LLMApiTimeoutError", retryable=False)
    card = display._trials["b:0"]
    assert card.status == "failed"
    assert card.error == "LLMApiTimeoutError"
    assert card.last_event_kind == "failed"
    assert card.last_update_ts > ts_after_start
    assert display._failed == 1
    assert display._running == 0


def test_judgment_scored_updates_score_and_focuses_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0)
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0)
    assert display._focused_trial_id == "b:0"
    ts_after_start = display._trials["a:0"].last_update_ts
    _install_incrementing_now(monkeypatch, start=ts_after_start + timedelta(seconds=2))
    display.judgment_scored(trial_id="a:0", score=0.7, binary_pass=True)
    card = display._trials["a:0"]
    assert card.score == 0.7
    assert card.binary_pass is True
    assert card.last_event_kind == "judged"
    assert card.last_update_ts > ts_after_start
    assert display._focused_trial_id == "a:0"


def test_run_finished_marks_display_finished() -> None:
    display = LiveRunDisplay()
    display.run_finished(output_dir=Path("/tmp/run"))
    assert display._finished is True


def test_trial_progress_before_trial_started_lazily_creates_card() -> None:
    display = LiveRunDisplay()
    display.trial_progress(
        trial_id="ghost:0",
        prompt_tokens_delta=10,
        completion_tokens_delta=5,
        cost_delta_usd=0.001,
    )
    assert "ghost:0" in display._trials
    card = display._trials["ghost:0"]
    assert card.status == "running"
    assert card.prompt_tokens == 10


# ---------------------------------------------------------------------------
# Kwarg-only enforcement
# ---------------------------------------------------------------------------


def test_trial_started_is_kwarg_only() -> None:
    display = LiveRunDisplay()
    with pytest.raises(TypeError):
        display.trial_started("x:0", "x", 0)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Focus follow — D7
# ---------------------------------------------------------------------------


def _install_incrementing_now(
    monkeypatch: pytest.MonkeyPatch, *, start: datetime | None = None
) -> None:
    """Patch ``_run_display._now`` with a strictly-increasing factory."""
    base = start if start is not None else datetime(2026, 7, 15, 12, 0, 0)
    counter = itertools.count(0)

    def fake_now() -> datetime:
        return base + timedelta(microseconds=next(counter))

    monkeypatch.setattr("tolokaforge.dx.live_panel._now", fake_now)


def test_focus_does_not_alternate_under_interleaved_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0)
    assert display._focused_trial_id == "a:0"

    # Fire 100 interleaved trial_progress events across two trials.
    for i in range(100):
        target = "a:0" if i % 2 == 0 else "b:0"
        display.trial_progress(
            trial_id=target,
            prompt_tokens_delta=1,
            completion_tokens_delta=1,
            cost_delta_usd=0.0,
        )
        assert (
            display._focused_trial_id == "a:0"
        ), f"trial_progress on iteration {i} unexpectedly moved focus"

    # Lifecycle event on b:0 moves focus.
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0)
    assert display._focused_trial_id == "b:0"


def test_focus_stays_on_just_completed_trial_while_others_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0)
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0)
    assert display._focused_trial_id == "b:0"

    display.trial_completed(trial_id="a:0", binary_pass=True, score=1.0)
    assert display._focused_trial_id == "a:0"

    # trial_progress on b:0 must NOT preempt.
    display.trial_progress(
        trial_id="b:0",
        prompt_tokens_delta=100,
        completion_tokens_delta=50,
        cost_delta_usd=0.005,
    )
    assert display._focused_trial_id == "a:0"

    display.trial_completed(trial_id="b:0", binary_pass=False, score=0.0)
    assert display._focused_trial_id == "b:0"


# ---------------------------------------------------------------------------
# Concurrent-write atomicity — D8
# ---------------------------------------------------------------------------


def test_trial_progress_is_atomic_under_concurrent_workers() -> None:
    display = LiveRunDisplay()
    worker_count = 12
    iterations = 1000

    def worker(idx: int) -> None:
        trial_id = f"t{idx}:0"
        for _ in range(iterations):
            display.trial_progress(
                trial_id=trial_id,
                prompt_tokens_delta=1,
                completion_tokens_delta=1,
                cost_delta_usd=0.001,
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert display._prompt_tokens == worker_count * iterations
    assert display._completion_tokens == worker_count * iterations
    assert display._total_cost_usd == pytest.approx(worker_count * iterations * 0.001, abs=1e-9)
    # Every per-trial counter must also be exact.
    for i in range(worker_count):
        card = display._trials[f"t{i}:0"]
        assert card.prompt_tokens == iterations
        assert card.completion_tokens == iterations
        assert card.turn_count == iterations


def test_trial_started_and_completed_are_atomic_under_concurrent_workers() -> None:
    display = LiveRunDisplay()
    worker_count = 12

    def worker(idx: int) -> None:
        trial_id = f"t{idx}:0"
        display.trial_started(trial_id=trial_id, task_id=f"t{idx}", trial_index=0)
        display.trial_completed(trial_id=trial_id, binary_pass=True, score=1.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert display._completed == worker_count
    assert display._running == 0
    assert len(display._trials) == worker_count


# ---------------------------------------------------------------------------
# Trial-row window trimming — D10
# ---------------------------------------------------------------------------


def test_visible_cards_returns_all_when_under_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(max_trial_rows=20)
    for i in range(5):
        display.trial_started(trial_id=f"t{i}:0", task_id=f"t{i}", trial_index=0)
    visible = display._visible_cards()
    assert len(visible) == 5


def test_visible_cards_trims_oldest_completed_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(max_trial_rows=20)
    for i in range(25):
        display.trial_completed(trial_id=f"t{i}:0", binary_pass=True, score=1.0)

    visible = display._visible_cards()
    assert len(visible) == 20
    visible_ids = {c.trial_id for c in visible}
    # The five oldest (t0..t4) scrolled off — they still exist in `_trials`
    # but are not in the visible set.
    dropped = {f"t{i}:0" for i in range(5)}
    assert visible_ids.isdisjoint(dropped)
    # State is preserved in `_trials` even for dropped cards.
    for tid in dropped:
        assert tid in display._trials


def test_visible_cards_always_includes_all_running(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(max_trial_rows=5)
    # 3 running trials.
    for i in range(3):
        display.trial_started(trial_id=f"r{i}:0", task_id=f"r{i}", trial_index=0)
    # 10 completed trials — window is 5, running gets 3 slots so completed
    # gets 2, and the 8 oldest completed scroll off.
    for i in range(10):
        display.trial_completed(trial_id=f"c{i}:0", binary_pass=True, score=1.0)

    visible = display._visible_cards()
    assert len(visible) == 5
    running_visible = {c.trial_id for c in visible if c.status == "running"}
    assert running_visible == {"r0:0", "r1:0", "r2:0"}


# ---------------------------------------------------------------------------
# Bottom-bar formatter — D6
# ---------------------------------------------------------------------------


def test_format_bottom_bar_locked_example() -> None:
    stats = _BottomBarStats(
        completed=142,
        total=500,
        running=12,
        cost_usd=0.87,
        prompt_tokens=41200,
        completion_tokens=6800,
        failed=3,
        eta_seconds=194,
    )
    assert _format_bottom_bar(stats) == (
        "142/500 · 12 running · $0.87 · in 41.2k / out 6.8k tok · fail 3 · eta 03:14"
    )


@pytest.mark.parametrize(
    ("cost", "expected_segment"),
    [
        (0.0, "$0.00"),
        (0.003, "$<0.01"),
        (0.87, "$0.87"),
        (12.5, "$12.50"),
    ],
)
def test_format_bottom_bar_cost_edge_cases(cost: float, expected_segment: str) -> None:
    stats = _BottomBarStats(
        completed=0,
        total=1,
        running=0,
        cost_usd=cost,
        prompt_tokens=0,
        completion_tokens=0,
        failed=0,
        eta_seconds=None,
    )
    assert f" · {expected_segment} · " in _format_bottom_bar(stats)


@pytest.mark.parametrize(
    ("prompt_tokens", "expected_prompt_segment"),
    [
        (1234, "in 1234 /"),
        (41200, "in 41.2k /"),
        (6800, "in 6.8k /"),
    ],
)
def test_format_bottom_bar_token_edge_cases(
    prompt_tokens: int, expected_prompt_segment: str
) -> None:
    stats = _BottomBarStats(
        completed=0,
        total=1,
        running=0,
        cost_usd=0.87,
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        failed=0,
        eta_seconds=None,
    )
    assert expected_prompt_segment in _format_bottom_bar(stats)


@pytest.mark.parametrize(
    ("eta_seconds", "expected_segment"),
    [
        (None, "eta n/a"),
        (17, "eta 00:17"),
        (194, "eta 03:14"),
        (11400, "eta 03:10:00"),
    ],
)
def test_format_bottom_bar_eta_edge_cases(eta_seconds: float | None, expected_segment: str) -> None:
    stats = _BottomBarStats(
        completed=0,
        total=1,
        running=0,
        cost_usd=0.87,
        prompt_tokens=0,
        completion_tokens=0,
        failed=0,
        eta_seconds=eta_seconds,
    )
    assert _format_bottom_bar(stats).endswith(expected_segment)


# ---------------------------------------------------------------------------
# __enter__ / __exit__ — D5 sentinel-handler stream re-point
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_root_handlers() -> object:
    """Snapshot root-logger handlers and restore them after the test.

    ``configure_root_logging`` (A3) leaves at most one sentinel-tagged handler
    installed; other tests in the suite may have left additional ones behind
    as ambient state. Snapshotting the full handler list and restoring on
    teardown isolates each test's mutations from the ambient state.
    """
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    try:
        yield None
    finally:
        root.handlers = saved


def test_enter_repoints_sentinel_handler_stream_and_exit_restores(
    _clean_root_handlers: object,
) -> None:
    fake_stream = io.StringIO()
    handler = logging.StreamHandler(fake_stream)
    setattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    logging.getLogger().addHandler(handler)

    assert handler.stream is fake_stream
    display = LiveRunDisplay(refresh_per_second=1000)
    with display:
        assert handler.stream is sys.stderr
    assert handler.stream is fake_stream


def test_enter_exit_is_idempotent_across_fresh_displays(
    _clean_root_handlers: object,
) -> None:
    fake_stream = io.StringIO()
    handler = logging.StreamHandler(fake_stream)
    setattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    logging.getLogger().addHandler(handler)

    with LiveRunDisplay(refresh_per_second=1000):
        assert handler.stream is sys.stderr
    assert handler.stream is fake_stream
    with LiveRunDisplay(refresh_per_second=1000):
        assert handler.stream is sys.stderr
    assert handler.stream is fake_stream


def test_enter_exit_swaps_every_sentinel_handler(_clean_root_handlers: object) -> None:
    """Multiple sentinel handlers (e.g. embedder mutation) all get re-pointed."""
    stream_a, stream_b = io.StringIO(), io.StringIO()
    handler_a = logging.StreamHandler(stream_a)
    handler_b = logging.StreamHandler(stream_b)
    setattr(handler_a, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    setattr(handler_b, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    logging.getLogger().addHandler(handler_a)
    logging.getLogger().addHandler(handler_b)

    with LiveRunDisplay(refresh_per_second=1000):
        assert handler_a.stream is sys.stderr
        assert handler_b.stream is sys.stderr
    assert handler_a.stream is stream_a
    assert handler_b.stream is stream_b


def test_enter_exit_without_sentinel_handler_is_a_noop(
    _clean_root_handlers: object,
) -> None:
    with LiveRunDisplay(refresh_per_second=1000) as display:
        assert display._saved_log_streams == []


def test_events_push_updated_layout_to_live() -> None:
    """Regression lock — events must re-render the panel, not freeze at startup.

    Rich's Layout binds child renderables at construction; without an explicit
    Live.update(...) after each event, the panel visually freezes at the
    initial "empty" state. This test installs a stub Live on the display
    (avoiding Rich's auto-refresh thread) and asserts each event handler
    triggers exactly one Live.update call with a freshly-built layout.
    """

    class _StubLive:
        def __init__(self) -> None:
            self.updates: list[object] = []

        def update(self, renderable: object, *, refresh: bool = False) -> None:
            self.updates.append(renderable)

    display = LiveRunDisplay()
    stub = _StubLive()
    display._live = stub  # type: ignore[assignment]

    display.run_started(total_trials=3, initial_completed=0)
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0)
    display.trial_progress(
        trial_id="a:0",
        prompt_tokens_delta=100,
        completion_tokens_delta=50,
        cost_delta_usd=0.01,
    )
    display.trial_completed(trial_id="a:0", binary_pass=True, score=1.0)
    display.trial_failed(trial_id="b:0", error="boom", retryable=False)
    display.judgment_scored(trial_id="a:0", score=0.9, binary_pass=True)
    display.run_finished(output_dir=Path("/tmp/x"))

    assert len(stub.updates) == 7, f"expected one Live.update per event; got {len(stub.updates)}"


def test_refresh_live_locked_is_noop_when_live_is_none() -> None:
    """When Live is not active (no _live instance), events must still update
    internal state without raising."""
    display = LiveRunDisplay()
    assert display._live is None
    # No events should raise; internal state should update.
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0)
    assert display._trials["a:0"].status == "running"
