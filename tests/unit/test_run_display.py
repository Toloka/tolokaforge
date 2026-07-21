"""Unit tests locking :class:`LiveRunDisplay` and :class:`RunDisplayEvents`.

Every assertion here maps to a documented decision (D1–D11) in the plan
``docs/plans/2026-07-15-issue-285-b1-rich-live-progress-panel.md`` or to
the contract laid out in ``tolokaforge/dx/live_panel.py``.
"""

from __future__ import annotations

import io
import itertools
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
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


def test_protocol_declares_twelve_lifecycle_methods() -> None:
    expected = {
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
    }
    declared = {
        name
        for name in vars(RunDisplayEvents)
        if not name.startswith("_") and callable(vars(RunDisplayEvents)[name])
    }
    # `RunDisplayEvents` inherits from Protocol which contributes some dunders;
    # the visible surface must equal the twelve lifecycle methods.
    assert declared == expected


def test_null_events_is_a_null_run_display_events_instance() -> None:
    assert isinstance(_NULL_EVENTS, _NullRunDisplayEvents)


# ---------------------------------------------------------------------------
# `for_mode` activation gate (D4)
# ---------------------------------------------------------------------------


def test_for_mode_rich_returns_live_display() -> None:
    ctx = LiveRunDisplay.for_mode(DisplayMode.RICH)
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
        ctx.events.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
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
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    card = display._trials["a:0"]
    assert card.status == "running"
    assert card.last_event_kind == "started"
    assert card.task_id == "a"
    assert card.trial_index == 0
    assert display._running == 1
    assert display._focused_trial_id == "a:0"


def test_trial_progress_accumulates_and_does_not_bump_last_update_ts() -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
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
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
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
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0, total_index=0)
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
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0, total_index=0)
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
# In-flight LLM state transitions
# ---------------------------------------------------------------------------


def test_trial_started_stores_agent_and_user_model_on_card() -> None:
    display = LiveRunDisplay()
    display.trial_started(
        trial_id="a:0",
        task_id="a",
        trial_index=0,
        total_index=0,
        agent_model="openrouter/anthropic/claude-sonnet-4-6",
        user_model="openrouter/openai/gpt-5.4",
    )
    card = display._trials["a:0"]
    assert card.agent_model == "openrouter/anthropic/claude-sonnet-4-6"
    assert card.user_model == "openrouter/openai/gpt-5.4"


def test_llm_call_started_sets_role_provider_and_start_ts_and_clears_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    # Seed retry_state so we can assert it clears.
    display._trials["a:0"].llm_retry_state = (2, 8.0, "seeded")
    display.llm_call_started(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
    )
    card = display._trials["a:0"]
    assert card.llm_role == "agent"
    assert card.llm_provider_model == "openrouter/anthropic/claude-sonnet-4-6"
    assert card.llm_call_start_ts is not None
    assert card.llm_retry_state is None


def test_llm_retry_scheduled_sets_tuple_state() -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.llm_retry_scheduled(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=2,
        next_attempt_in_s=8.0,
        reason="APIConnectionError: read timeout",
    )
    assert display._trials["a:0"].llm_retry_state == (
        2,
        8.0,
        "APIConnectionError: read timeout",
    )


def test_llm_call_finished_clears_all_four_llm_fields() -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.llm_call_started(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
    )
    display.llm_retry_scheduled(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
        next_attempt_in_s=4.0,
        reason="timeout",
    )
    display.llm_call_finished(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=2,
        duration_s=1.2,
        error=None,
    )
    card = display._trials["a:0"]
    assert card.llm_role is None
    assert card.llm_provider_model is None
    assert card.llm_call_start_ts is None
    assert card.llm_retry_state is None


def test_trial_completed_clears_stale_in_flight_llm_state() -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.llm_call_started(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
    )
    display.trial_completed(trial_id="a:0", binary_pass=True, score=1.0)
    card = display._trials["a:0"]
    assert card.llm_role is None
    assert card.llm_provider_model is None
    assert card.llm_call_start_ts is None
    assert card.llm_retry_state is None


def test_trial_failed_clears_stale_in_flight_llm_state() -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.llm_retry_scheduled(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=3,
        next_attempt_in_s=32.0,
        reason="APIConnectionError",
    )
    display.trial_failed(trial_id="a:0", error="LLMApiTimeoutError", retryable=False)
    card = display._trials["a:0"]
    assert card.llm_role is None
    assert card.llm_provider_model is None
    assert card.llm_call_start_ts is None
    assert card.llm_retry_state is None


def test_llm_call_started_lazy_creates_card_when_trial_unknown() -> None:
    """Handler must not raise when the seam fires before ``trial_started``."""
    display = LiveRunDisplay()
    display.llm_call_started(
        trial_id="ghost:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
    )
    assert "ghost:0" in display._trials
    assert display._trials["ghost:0"].llm_role == "agent"


@pytest.mark.parametrize(
    "method",
    [
        "llm_call_started",
        "llm_call_finished",
        "llm_retry_scheduled",
    ],
)
def test_llm_call_handlers_are_kwarg_only(method: str) -> None:
    display = LiveRunDisplay()
    with pytest.raises(TypeError):
        getattr(display, method)("a:0")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Focused pane rendering — waiting / retry line + model identity header
# ---------------------------------------------------------------------------


def _render_right_pane_text(display: LiveRunDisplay, *, width: int = 120) -> str:
    from rich.console import Console

    console = Console(width=width, force_terminal=True, color_system="truecolor", record=True)
    console.print(display._render_right_pane())
    return console.export_text()


def test_focused_pane_renders_waiting_line_with_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``llm_call_started``, the focused pane shows ``⏳ waiting on
    {role}: {provider}/{model} — {elapsed:.1f}s`` using the delta between
    the render-time clock and ``llm_call_start_ts``."""
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    # Two-value clock installed AFTER trial_started so its default-factory
    # `_now()` for `last_update_ts` isn't affected. First value seeds
    # `llm_call_start_ts`; second is the render-time `_now()` used to
    # compute elapsed. Delta is 3.2s.
    ts_start = datetime(2026, 7, 16, 12, 0, 0)
    values = iter([ts_start, ts_start + timedelta(seconds=3, milliseconds=200)])
    monkeypatch.setattr("tolokaforge.dx.live_panel._now", lambda: next(values))
    display.llm_call_started(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
    )
    body = _render_right_pane_text(display)
    assert "waiting on agent: openrouter/anthropic/claude-sonnet-4-6" in body
    assert "3.2s" in body


def test_focused_pane_renders_retry_line_and_hides_waiting() -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.llm_call_started(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
    )
    display.llm_retry_scheduled(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=2,
        next_attempt_in_s=8.0,
        reason="APIConnectionError",
    )
    body = _render_right_pane_text(display)
    assert "retry 2/5 after 8s (APIConnectionError)" in body
    assert "waiting on" not in body


def test_focused_pane_omits_call_line_after_finished() -> None:
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.llm_call_started(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
    )
    display.llm_call_finished(
        trial_id="a:0",
        role="agent",
        provider="openrouter",
        model="anthropic/claude-sonnet-4-6",
        attempt=1,
        duration_s=0.4,
        error=None,
    )
    body = _render_right_pane_text(display)
    assert "waiting on" not in body
    assert "retry" not in body


def test_focused_pane_renders_agent_model_header_when_set() -> None:
    display = LiveRunDisplay()
    display.trial_started(
        trial_id="a:0",
        task_id="a",
        trial_index=0,
        total_index=0,
        agent_model="openrouter/anthropic/claude-sonnet-4-6",
    )
    body = _render_right_pane_text(display)
    assert "model: openrouter/anthropic/claude-sonnet-4-6" in body


def test_focused_pane_absent_model_header_when_agent_model_unset() -> None:
    """When ``trial_started`` is called without ``agent_model``, the focused
    pane omits the model header line — the invariant that keeps steady-state
    goldens byte-identical."""
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    body = _render_right_pane_text(display)
    assert "model:" not in body


# ---------------------------------------------------------------------------
# Kwarg-only enforcement
# ---------------------------------------------------------------------------


def test_trial_started_is_kwarg_only() -> None:
    display = LiveRunDisplay()
    with pytest.raises(TypeError):
        display.trial_started("x:0", "x", 0, 0)  # type: ignore[misc]


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
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
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
        msg = f"trial_progress on iteration {i} unexpectedly moved focus"
        assert display._focused_trial_id == "a:0", msg

    # Lifecycle event on b:0 moves focus.
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0, total_index=0)
    assert display._focused_trial_id == "b:0"


def test_focus_stays_on_just_completed_trial_while_others_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay()
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0, total_index=0)
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
        display.trial_started(trial_id=trial_id, task_id=f"t{idx}", trial_index=0, total_index=0)
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
        display.trial_started(trial_id=f"t{i}:0", task_id=f"t{i}", trial_index=0, total_index=0)
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
        display.trial_started(trial_id=f"r{i}:0", task_id=f"r{i}", trial_index=0, total_index=0)
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
# __enter__ / __exit__ — sentinel handler is replaced with a `_LogSink`
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


def test_enter_replaces_sentinel_handler_and_exit_restores(
    _clean_root_handlers: object,
) -> None:
    from tolokaforge.dx.live_panel import _LogSink

    fake_stream = io.StringIO()
    handler = logging.StreamHandler(fake_stream)
    setattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    root = logging.getLogger()
    root.addHandler(handler)

    display = LiveRunDisplay(refresh_per_second=1000)
    with display:
        assert handler not in root.handlers
        sinks = [h for h in root.handlers if isinstance(h, _LogSink)]
        assert len(sinks) == 1
    assert handler in root.handlers
    assert not [h for h in root.handlers if isinstance(h, _LogSink)]


def test_enter_exit_is_idempotent_across_fresh_displays(
    _clean_root_handlers: object,
) -> None:
    from tolokaforge.dx.live_panel import _LogSink

    fake_stream = io.StringIO()
    handler = logging.StreamHandler(fake_stream)
    setattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    root = logging.getLogger()
    root.addHandler(handler)

    with LiveRunDisplay(refresh_per_second=1000):
        assert handler not in root.handlers
    assert handler in root.handlers
    with LiveRunDisplay(refresh_per_second=1000):
        assert handler not in root.handlers
        assert any(isinstance(h, _LogSink) for h in root.handlers)
    assert handler in root.handlers
    assert not [h for h in root.handlers if isinstance(h, _LogSink)]


def test_enter_exit_swaps_every_sentinel_handler(_clean_root_handlers: object) -> None:
    """Multiple sentinel handlers (e.g. embedder mutation) all get replaced."""
    from tolokaforge.dx.live_panel import _LogSink

    stream_a, stream_b = io.StringIO(), io.StringIO()
    handler_a = logging.StreamHandler(stream_a)
    handler_b = logging.StreamHandler(stream_b)
    setattr(handler_a, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    setattr(handler_b, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    root = logging.getLogger()
    root.addHandler(handler_a)
    root.addHandler(handler_b)

    with LiveRunDisplay(refresh_per_second=1000):
        assert handler_a not in root.handlers
        assert handler_b not in root.handlers
        assert sum(isinstance(h, _LogSink) for h in root.handlers) == 2
    assert handler_a in root.handlers
    assert handler_b in root.handlers


def test_enter_exit_without_sentinel_handler_is_a_noop(
    _clean_root_handlers: object,
) -> None:
    with LiveRunDisplay(refresh_per_second=1000) as display:
        assert display._replaced_log_handlers == []


# ---------------------------------------------------------------------------
# __enter__ / __exit__ — child-logger stderr-bypass sweep
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolate_child_logger() -> object:
    """Yield a factory that creates a named child logger and restores its
    handlers / propagate / level on teardown.

    ``logging.getLogger`` returns the same singleton across a test session,
    so mutations on child loggers leak across tests unless explicitly rewound.
    """
    saved: list[tuple[logging.Logger, list[logging.Handler], bool, int]] = []

    def factory(name: str, *, propagate: bool = True, level: int = logging.DEBUG) -> logging.Logger:
        lg = logging.getLogger(name)
        saved.append((lg, list(lg.handlers), lg.propagate, lg.level))
        lg.handlers = []
        lg.propagate = propagate
        lg.setLevel(level)
        return lg

    yield factory
    for lg, handlers, propagate, level in reversed(saved):
        lg.handlers = list(handlers)
        lg.propagate = propagate
        lg.setLevel(level)


def test_child_logger_bypassing_handler_removed_and_restored(
    monkeypatch: pytest.MonkeyPatch,
    _clean_root_handlers: object,
    _isolate_child_logger: Callable[..., logging.Logger],
) -> None:
    """Child logger's ``StreamHandler(sys.stderr)`` is removed during the Live
    lifetime, its INFO records propagate to the root ``_LogSink`` (bypass
    channel gone → no raw write reaches the fake stream), and the handler is
    restored on ``__exit__``.

    INFO — not WARNING — is emitted because ``_LogSink`` routes WARNING+
    through ``print_above → live.console.print``, which itself writes to
    ``sys.stderr`` (the fake). INFO records only land in the buffer, so any
    write of the message text to the fake stream must have come from the
    bypass handler.
    """
    fake_stderr = _FakeStderr()
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    root_sentinel = logging.StreamHandler(io.StringIO())
    setattr(root_sentinel, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    logging.getLogger().addHandler(root_sentinel)

    child = _isolate_child_logger("chatty", propagate=True)
    child_handler = logging.StreamHandler(sys.stderr)
    child.addHandler(child_handler)

    display = LiveRunDisplay(refresh_per_second=1000)
    marker = "chatty-info-marker-abc123"
    with display:
        assert child_handler not in child.handlers
        child.info(marker)
        assert marker not in "".join(fake_stderr.buf)
        assert any(r.getMessage() == marker for r in display.log_records())
    assert child_handler in child.handlers


def test_child_logger_with_non_dangerous_handler_left_untouched(
    _clean_root_handlers: object,
    _isolate_child_logger: Callable[..., logging.Logger],
) -> None:
    """A handler bound to a stream that is NOT one of the captured terminal
    streams stays installed for the Live lifetime and continues to receive
    records."""
    unrelated_stream = io.StringIO()
    child = _isolate_child_logger("well_behaved", propagate=True)
    child_handler = logging.StreamHandler(unrelated_stream)
    child.addHandler(child_handler)

    display = LiveRunDisplay(refresh_per_second=1000)
    with display:
        assert child_handler in child.handlers
        child.warning("still writes")
    assert "still writes" in unrelated_stream.getvalue()


def test_child_logger_shaped_like_litellm_emits_zero_raw_stderr_writes(
    monkeypatch: pytest.MonkeyPatch,
    _clean_root_handlers: object,
    _isolate_child_logger: Callable[..., logging.Logger],
) -> None:
    """A ``propagate=True`` child logger with a ``StreamHandler`` bound to
    the captured ``sys.stderr`` object emits zero *bypass* writes during
    the Live lifetime.

    INFO-level records are used to isolate the bypass channel — WARNING+ would
    additionally flow through ``_LogSink → print_above → Rich`` which writes
    to the same underlying stream via a Rich-coordinated path.
    """
    fake_stderr = _FakeStderr()
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    like_litellm = _isolate_child_logger("like_litellm", propagate=True)
    like_litellm.addHandler(logging.StreamHandler(sys.stderr))

    marker = "like-litellm-info-marker-xyz789"
    with LiveRunDisplay(refresh_per_second=1000):
        like_litellm.info(marker)
        like_litellm.debug(marker + "-debug")

    written = "".join(fake_stderr.buf)
    assert marker not in written
    assert (marker + "-debug") not in written


def test_child_logger_with_propagate_false_gets_log_sink_installed(
    monkeypatch: pytest.MonkeyPatch,
    _clean_root_handlers: object,
    _isolate_child_logger: Callable[..., logging.Logger],
) -> None:
    """A non-propagating child logger's bypassing handler is removed AND a
    fresh ``_LogSink`` is installed on it so records still surface through
    the panel (records must not be silently dropped). Both mutations
    are reversed on ``__exit__``.

    INFO is emitted (not WARNING) so the check that no write hits the fake
    stream isolates the bypass channel from the ``print_above`` route.
    """
    from tolokaforge.dx.live_panel import _LogSink

    fake_stderr = _FakeStderr()
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    private = _isolate_child_logger("private_regression", propagate=False)
    original_handler = logging.StreamHandler(sys.stderr)
    private.addHandler(original_handler)

    display = LiveRunDisplay(refresh_per_second=1000)
    marker = "private-info-marker-def456"
    with display:
        assert original_handler not in private.handlers
        installed_sinks = [h for h in private.handlers if isinstance(h, _LogSink)]
        assert len(installed_sinks) == 1
        private.info(marker)
        assert marker not in "".join(fake_stderr.buf)
        assert any(r.getMessage() == marker for r in display.log_records())
    assert original_handler in private.handlers
    assert not [h for h in private.handlers if isinstance(h, _LogSink)]


def test_placeholder_in_logger_dict_does_not_crash_enter_exit(
    _clean_root_handlers: object,
) -> None:
    """Sweep iterates ``logging.root.manager.loggerDict`` values, which are
    ``Logger`` OR ``PlaceHolder``. ``PlaceHolder`` has no ``.handlers`` —
    a naive iteration would raise ``AttributeError`` inside ``__enter__``,
    crashing the exact display the fix targets. This test seeds a
    ``PlaceHolder`` and locks the ``isinstance(v, logging.Logger)`` guard."""
    # Unique names so a prior test run has not turned intermediates into
    # real Loggers.
    leaf_name = "stage2_ph_regression_x.stage2_ph_regression_y.leaf"
    intermediate = "stage2_ph_regression_x.stage2_ph_regression_y"
    logging.getLogger(leaf_name)
    assert isinstance(logging.root.manager.loggerDict.get(intermediate), logging.PlaceHolder)

    display = LiveRunDisplay(refresh_per_second=1000)
    with display:
        display.run_started(total_trials=1, initial_completed=0)


def test_events_push_updated_layout_to_live() -> None:
    """Regression lock — events must re-render the panel, not freeze at startup.

    Rich's Layout binds child renderables at construction; without an explicit
    Live.update(...) after each event, the panel visually freezes at the
    initial "empty" state. This test installs a stub Live on the display
    (avoiding Rich's auto-refresh thread) and asserts each event handler
    triggers exactly one Live.update call with a freshly-built layout.
    """

    class _StubConsole:
        height = 40

    class _StubLive:
        def __init__(self) -> None:
            self.updates: list[object] = []
            self.console = _StubConsole()

        def update(self, renderable: object, *, refresh: bool = False) -> None:
            self.updates.append(renderable)

    display = LiveRunDisplay()
    stub = _StubLive()
    display._live = stub  # type: ignore[assignment]

    display.run_started(total_trials=3, initial_completed=0)
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
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
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    assert display._trials["a:0"].status == "running"


# ---------------------------------------------------------------------------
# `_LogSink` — INFO/DEBUG swallowed, WARNING+ forwarded, buffer always fills
# ---------------------------------------------------------------------------


def _make_log_record(level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="tolokaforge.probe",
        level=level,
        pathname=__file__,
        lineno=0,
        msg=message,
        args=None,
        exc_info=None,
    )


def test_log_sink_swallows_info_records() -> None:
    from collections import deque

    from tolokaforge.dx.live_panel import _LogSink

    buffer: deque[logging.LogRecord] = deque(maxlen=500)
    printed: list[str] = []
    sink = _LogSink(
        print_above=printed.append,
        formatter=logging.Formatter("%(levelname)s %(message)s"),
        buffer=buffer,
    )

    sink.emit(_make_log_record(logging.INFO, "hello"))
    sink.emit(_make_log_record(logging.DEBUG, "quiet"))

    assert printed == []
    assert [r.getMessage() for r in buffer] == ["hello", "quiet"]


def test_log_sink_forwards_warning_and_above_via_print_above() -> None:
    from collections import deque

    from tolokaforge.dx.live_panel import _LogSink

    buffer: deque[logging.LogRecord] = deque(maxlen=500)
    printed: list[str] = []
    sink = _LogSink(
        print_above=printed.append,
        formatter=logging.Formatter("%(levelname)s %(message)s"),
        buffer=buffer,
    )

    sink.emit(_make_log_record(logging.WARNING, "warn me"))
    sink.emit(_make_log_record(logging.ERROR, "oh no"))

    assert printed == ["WARNING warn me", "ERROR oh no"]
    assert [r.getMessage() for r in buffer] == ["warn me", "oh no"]


def test_log_sink_buffer_is_bounded() -> None:
    from collections import deque

    from tolokaforge.dx.live_panel import _LogSink

    buffer: deque[logging.LogRecord] = deque(maxlen=3)
    sink = _LogSink(print_above=lambda _line: None, formatter=None, buffer=buffer)

    for i in range(10):
        sink.emit(_make_log_record(logging.INFO, f"msg-{i}"))

    assert [r.getMessage() for r in buffer] == ["msg-7", "msg-8", "msg-9"]


def test_log_records_returns_buffered_records(_clean_root_handlers: object) -> None:
    """``log_records`` gives the per-trial log-tail widget a snapshot of the
    buffer without leaking the mutable deque."""
    fake_stream = io.StringIO()
    handler = logging.StreamHandler(fake_stream)
    setattr(handler, _TOLOKAFORGE_ROOT_HANDLER_SENTINEL, True)
    root = logging.getLogger()
    root.addHandler(handler)

    display = LiveRunDisplay(refresh_per_second=1000)
    with display:
        logging.getLogger("tolokaforge.probe").warning("routed")

    records = display.log_records()
    assert isinstance(records, tuple)
    assert any(r.getMessage() == "routed" for r in records)


# ---------------------------------------------------------------------------
# Services widget — mid-region during startup, one-liner once trials dispatch
# ---------------------------------------------------------------------------


def test_phase_changed_populates_services_and_renders_widget() -> None:
    from tolokaforge.core.run_display_events import ServiceSnapshot

    display = LiveRunDisplay(refresh_per_second=1000)
    services: list[ServiceSnapshot] = [
        {"name": "runner", "status": "starting", "ports": {50051: 50051}, "role": "engine"},
        {"name": "db-service", "status": "healthy", "ports": {8000: 8000}, "role": "engine"},
    ]

    display.phase_changed(phase="starting_services", detail="docker compose up", services=services)

    assert display._services == services
    # `_total_trials == 0` and services populated: services widget region present.
    layout = display._build_layout()
    child_names = {getattr(child, "name", None) for child in layout.children}
    assert "services" in child_names


def test_services_widget_absent_once_trials_dispatch() -> None:
    from tolokaforge.core.run_display_events import ServiceSnapshot

    display = LiveRunDisplay(refresh_per_second=1000)
    services: list[ServiceSnapshot] = [
        {"name": "runner", "status": "healthy", "ports": {}, "role": "engine"},
    ]
    display.phase_changed(phase="services_ready", services=services)
    display.run_started(total_trials=1, initial_completed=0)

    layout = display._build_layout()
    child_names = {getattr(child, "name", None) for child in layout.children}
    assert "services" not in child_names


# ---------------------------------------------------------------------------
# Boot-log helpers — filter predicate + tail renderer for the startup widget
# ---------------------------------------------------------------------------


def _make_docker_record(
    *, name: str, created: float, msecs: float, message: str
) -> logging.LogRecord:
    """Build a real ``LogRecord`` with pinned ``created``/``msecs``.

    Uses ``makeLogRecord`` so ``getMessage()`` / ``%``-formatting runs for
    real (no mock) — the ``msg``/``args`` field is exercised the same way
    the production ``_LogSink`` sees it.
    """
    return logging.makeLogRecord(
        {
            "name": name,
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": message,
            "args": None,
            "created": created,
            "msecs": msecs,
            "pathname": __file__,
            "lineno": 0,
        }
    )


def test_docker_boot_records_keeps_docker_dot_prefix_only() -> None:
    from tolokaforge.dx.live_panel import _docker_boot_records

    records = [
        _make_docker_record(
            name="tolokaforge.docker.stack",
            created=1784205296.0,
            msecs=0.0,
            message="starting stack",
        ),
        _make_docker_record(
            name="tolokaforge.docker.container",
            created=1784205297.0,
            msecs=0.0,
            message="starting container",
        ),
        _make_docker_record(
            name="tolokaforge.docker.health",
            created=1784205298.0,
            msecs=0.0,
            message="healthy",
        ),
        _make_docker_record(
            name="tolokaforge.runner",
            created=1784205299.0,
            msecs=0.0,
            message="runner note",
        ),
        _make_docker_record(
            name="tolokaforge.docker",
            created=1784205300.0,
            msecs=0.0,
            message="bare docker namespace",
        ),
    ]

    kept = _docker_boot_records(records)
    assert [r.name for r in kept] == [
        "tolokaforge.docker.stack",
        "tolokaforge.docker.container",
        "tolokaforge.docker.health",
    ]


def test_render_boot_log_tail_keeps_last_max_lines_in_order() -> None:
    from rich.console import Console

    from tolokaforge.dx._display import THEME
    from tolokaforge.dx.live_panel import _render_boot_log_tail

    # Eight docker records at t=1784205296.0 + i seconds; last five are indices 3..7.
    base_epoch = 1784205296.0
    records = [
        _make_docker_record(
            name="tolokaforge.docker.stack",
            created=base_epoch + i,
            msecs=(i + 1) * 100.0,
            message=f"milestone-{i}",
        )
        for i in range(8)
    ]

    panel = _render_boot_log_tail(records, max_lines=5)
    console = Console(
        width=120, force_terminal=True, color_system="truecolor", record=True, theme=THEME
    )
    console.print(panel)
    body = console.export_text()

    # Dropped: indices 0..2 (older than the tail window).
    for dropped in ("milestone-0", "milestone-1", "milestone-2"):
        assert dropped not in body, f"{dropped} should have been trimmed"
    # Kept: indices 3..7, in input order (most-recent last).
    for kept in ("milestone-3", "milestone-4", "milestone-5", "milestone-6", "milestone-7"):
        assert kept in body

    # Deterministic UTC timestamp: base_epoch=1784205296.0 → 12:34:56 UTC on 2026-07-16.
    # First kept line: index 3 → +3s → 12:34:59, msecs=400 → ".400".
    assert "12:34:59.400 | stack | milestone-3" in body
    # Last kept line: index 7 → +7s → 12:35:03, msecs=800 → ".800".
    assert "12:35:03.800 | stack | milestone-7" in body


def test_render_boot_log_tail_truncates_msecs_below_1000() -> None:
    """``msecs = 999.6`` renders ``.999`` — the column never widens to 4 digits."""
    from rich.console import Console

    from tolokaforge.dx._display import THEME
    from tolokaforge.dx.live_panel import _render_boot_log_tail

    record = _make_docker_record(
        name="tolokaforge.docker.stack",
        created=1784205296.0,
        msecs=999.6,
        message="msec-truncation",
    )

    panel = _render_boot_log_tail([record], max_lines=5)
    console = Console(
        width=120, force_terminal=True, color_system="truecolor", record=True, theme=THEME
    )
    console.print(panel)
    body = console.export_text()

    assert "12:34:56.999 | stack | msec-truncation" in body
    assert ".1000" not in body


# ---------------------------------------------------------------------------
# Boot-log region — layout wiring, activation gate, height clamp
# ---------------------------------------------------------------------------


def _install_stub_live(display: LiveRunDisplay, height: int) -> None:
    class _StubConsole:
        pass

    class _StubLive:
        def __init__(self) -> None:
            self.console = _StubConsole()

        def update(self, *_a: object, **_kw: object) -> None:
            pass

    _StubConsole.height = height  # type: ignore[attr-defined]
    display._live = _StubLive()  # type: ignore[assignment]


def test_boot_log_region_present_between_services_and_main_during_boot() -> None:
    """During the startup window with buffered docker records, the boot-log
    region sits between the services widget and ``main``."""
    from tolokaforge.core.run_display_events import ServiceSnapshot

    display = LiveRunDisplay(refresh_per_second=1000)
    services: list[ServiceSnapshot] = [
        {"name": "runner", "status": "starting", "ports": {50051: 50051}, "role": "engine"},
    ]
    display.phase_changed(phase="starting_services", detail="docker compose up", services=services)
    for i in range(3):
        display._log_buffer.append(
            _make_docker_record(
                name="tolokaforge.docker.stack",
                created=1784205296.0 + i,
                msecs=0.0,
                message=f"milestone-{i}",
            )
        )

    layout = display._build_layout()
    names = [getattr(c, "name", None) for c in layout.children]
    assert "services" in names
    assert "boot_log" in names
    assert names.index("boot_log") == names.index("services") + 1
    assert names.index("boot_log") == names.index("main") - 1


def test_boot_log_region_absent_once_trials_dispatch() -> None:
    """``run_started`` collapses the boot-log region even with docker records
    still in the buffer — mirrors the services widget."""
    display = LiveRunDisplay(refresh_per_second=1000)
    for i in range(3):
        display._log_buffer.append(
            _make_docker_record(
                name="tolokaforge.docker.stack",
                created=1784205296.0 + i,
                msecs=0.0,
                message=f"milestone-{i}",
            )
        )
    display.run_started(total_trials=1, initial_completed=0)

    layout = display._build_layout()
    names = {getattr(c, "name", None) for c in layout.children}
    assert "boot_log" not in names


def test_boot_log_region_absent_when_no_docker_records_buffered() -> None:
    """A buffer with only a non-docker record leaves the boot-log region off —
    no empty bordered panel."""
    display = LiveRunDisplay(refresh_per_second=1000)
    display._log_buffer.append(
        _make_docker_record(
            name="tolokaforge.runner",
            created=1784205296.0,
            msecs=0.0,
            message="runner note",
        )
    )

    layout = display._build_layout()
    names = {getattr(c, "name", None) for c in layout.children}
    assert "boot_log" not in names


def _seed_boot_state(display: LiveRunDisplay, *, record_count: int) -> None:
    from tolokaforge.core.run_display_events import ServiceSnapshot

    services: list[ServiceSnapshot] = [
        {"name": "runner", "status": "starting", "ports": {50051: 50051}, "role": "engine"},
        {"name": "db-service", "status": "starting", "ports": {8000: 8000}, "role": "engine"},
    ]
    display.phase_changed(phase="starting_services", detail="docker compose up", services=services)
    for i in range(record_count):
        display._log_buffer.append(
            _make_docker_record(
                name="tolokaforge.docker.stack",
                created=1784205296.0 + i,
                msecs=(i + 1) * 100.0,
                message=f"milestone-{i}",
            )
        )


def _layout_child(layout: object, name: str) -> object | None:
    for child in getattr(layout, "children", []):
        if getattr(child, "name", None) == name:
            return child
    return None


def test_boot_log_height_invariant_no_clamp_tall_viewport() -> None:
    """Viewport 40: boot-log gets its full desired height (5 records + 2 = 7)
    and the total row sum equals ``max(12, 40 - 1) == 39``."""
    display = LiveRunDisplay(refresh_per_second=1000)
    _install_stub_live(display, height=40)
    _seed_boot_state(display, record_count=5)

    layout = display._build_layout()
    sizes = {getattr(c, "name", None): c.size for c in layout.children}

    assert sizes["boot_log"] == 7  # 5 records + 2 border rows
    assert sizes["main"] >= 5
    assert sum(sizes.values()) == 39


def test_boot_log_height_clamped_but_present_locks_392_regression() -> None:
    """Viewport 15: boot-log clamps to ``budget = 14 - 4 - 1 - 5 = 4`` rows;
    region stays present, ``main`` stays at its floor of 5, sum equals 14.

    This is the regime that would silently overflow under the naive
    ``main_h = max(5, total - …)`` formula: ``4 services + 7 desired boot-log
    + 5 main-floor + 1 bottom = 17 > 14``. Row sum > total re-anchors Rich
    Live and re-introduces the #392 panel stacking, so this assertion is
    the regression lock.

    Additionally: rendering the boot-log region into text with the SAME
    clamped ``max_lines`` locks the "most-recent-last under clamp" contract
    — the newest records must survive the crop, not the oldest.
    """
    from rich.console import Console

    from tolokaforge.dx._display import THEME
    from tolokaforge.dx.live_panel import _docker_boot_records, _render_boot_log_tail

    display = LiveRunDisplay(refresh_per_second=1000)
    _install_stub_live(display, height=15)
    _seed_boot_state(display, record_count=5)

    layout = display._build_layout()
    sizes = {getattr(c, "name", None): c.size for c in layout.children}

    assert "boot_log" in sizes, "boot-log region must survive the mid-viewport clamp"
    assert sizes["boot_log"] == 4  # budget = 14 - 4 - 1 - 5 = 4
    assert sizes["services"] == 4
    assert sizes["main"] == 5
    assert sizes["bottom"] == 1
    assert sum(sizes.values()) == 14

    filtered = _docker_boot_records(tuple(display._log_buffer))
    panel = _render_boot_log_tail(filtered, max_lines=sizes["boot_log"] - 2)
    console = Console(
        width=120, force_terminal=True, color_system="truecolor", record=True, theme=THEME
    )
    console.print(panel)
    body = console.export_text()
    # boot_log_h - 2 == 2 content rows: the LAST 2 records must be kept,
    # older ones dropped. Rich crops a Panel from the bottom, so passing
    # a hardcoded max_lines=5 would drop the 3 newest and invert the
    # "most-recent-last" contract exactly on small terminals.
    for kept in ("milestone-3", "milestone-4"):
        assert kept in body, f"newest record {kept} must survive the clamp"
    for dropped in ("milestone-0", "milestone-1", "milestone-2"):
        assert dropped not in body, f"older record {dropped} must be trimmed"


def test_boot_log_region_dropped_when_budget_below_minimum_panel_height() -> None:
    """Viewport 12: ``budget = 12 - 4 - 1 - 5 = 2 < 3`` → region absent.

    Layout is byte-identical to the services-only window: sum equals
    ``max(12, 12 - 1) == 12``, and ``boot_log`` is not among the child
    names.
    """
    display = LiveRunDisplay(refresh_per_second=1000)
    _install_stub_live(display, height=12)
    _seed_boot_state(display, record_count=5)

    layout = display._build_layout()
    names = [getattr(c, "name", None) for c in layout.children]
    sizes = {name: c.size for name, c in zip(names, layout.children, strict=True)}

    assert "boot_log" not in names
    assert sizes["services"] == 4
    assert sizes["main"] == 7  # total 12 - 4 services - 1 bottom - 0 banner - 0 boot_log
    assert sizes["bottom"] == 1
    assert sum(sizes.values()) == 12


def test_boot_log_layout_self_consistent_when_banner_and_boot_log_co_occur() -> None:
    """Contrived state — ``_banner`` set alongside boot-log activation at
    ``_total_trials == 0`` — must still produce a row sum equal to
    ``max(12, viewport - 1)``.

    No current emitter fires both regions simultaneously, but the layout
    math must not silently depend on that caller-ordering invariant. A
    future emitter that raised ``_banner`` before dispatch (e.g.
    ``trial_failed`` during boot) would otherwise overflow the row budget
    by ``banner_h == 5`` — Rich Live would re-anchor and re-introduce the
    #392 panel-stacking regression. Locks the invariant at the layout
    level so it holds regardless of which events fire first.
    """
    display = LiveRunDisplay(refresh_per_second=1000)
    _install_stub_live(display, height=20)
    _seed_boot_state(display, record_count=5)
    display._banner = ("auth error", "check credentials", None)

    layout = display._build_layout()
    sizes = {getattr(c, "name", None): c.size for c in layout.children}

    assert sizes.get("banner") == 5
    assert sum(sizes.values()) == max(12, 20 - 1)


# ---------------------------------------------------------------------------
# `trial_provisioned` — containers land on the focused card
# ---------------------------------------------------------------------------


def test_trial_provisioned_populates_card_containers() -> None:
    from tolokaforge.core.run_display_events import ContainerSnapshot

    display = LiveRunDisplay(refresh_per_second=1000)
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    containers: list[ContainerSnapshot] = [
        {
            "name": "trial-runner",
            "service": "runner",
            "state": "running",
            "health": "healthy",
            "ports": {50051: 50051},
        }
    ]
    display.trial_provisioned(
        trial_id="a:0",
        containers=containers,
        endpoints={"runner": "http://localhost:50051"},
    )
    assert display._trials["a:0"].containers == containers


def test_trial_provisioned_lazy_creates_card_when_started_missed() -> None:
    from tolokaforge.core.run_display_events import ContainerSnapshot

    display = LiveRunDisplay(refresh_per_second=1000)
    containers: list[ContainerSnapshot] = [
        {"name": "x", "service": "runner", "state": "running", "health": None, "ports": {}}
    ]
    display.trial_provisioned(trial_id="ghost:0", containers=containers, endpoints={})
    assert display._trials["ghost:0"].containers == containers


# ---------------------------------------------------------------------------
# Left-pane row prefix — `[N/M]` global-index column, width fixed by total
# ---------------------------------------------------------------------------


def test_left_pane_renders_global_index_prefix_at_fixed_width() -> None:
    from rich.console import Console

    display = LiveRunDisplay(refresh_per_second=1000)
    display.run_started(total_trials=500, initial_completed=0)
    display.trial_started(trial_id="task_a:0", task_id="task_a", trial_index=0, total_index=16)

    console = Console(width=80, force_terminal=True, color_system="truecolor", record=True)
    console.print(display._render_left_pane())
    body = console.export_text()

    # 500 → three-digit column, index 17 (0-based 16) rendered as ` 17`.
    assert "[ 17/500]" in body
    assert "task_a" in body


def test_left_pane_prefix_width_adapts_to_total() -> None:
    from rich.console import Console

    display = LiveRunDisplay(refresh_per_second=1000)
    display.run_started(total_trials=8, initial_completed=0)
    display.trial_started(trial_id="task_a:0", task_id="task_a", trial_index=0, total_index=2)

    console = Console(width=80, force_terminal=True, color_system="truecolor", record=True)
    console.print(display._render_left_pane())
    body = console.export_text()

    # 8 → single-digit column, no leading spaces inside the brackets.
    assert "[3/8]" in body


# ---------------------------------------------------------------------------
# Adaptive main-region sizing
# ---------------------------------------------------------------------------


def test_main_region_size_matches_visible_cards_when_terminal_is_tall() -> None:
    """Three trials in a 40-row terminal give a 5-row main region, not 39."""

    class _StubConsole:
        height = 40

    class _StubLive:
        def __init__(self) -> None:
            self.console = _StubConsole()

        def update(self, *_a, **_kw) -> None:  # noqa: D401 — stub
            pass

    display = LiveRunDisplay(refresh_per_second=1000)
    display._live = _StubLive()  # type: ignore[assignment]
    display.run_started(total_trials=3, initial_completed=0)
    for i in range(3):
        display.trial_started(trial_id=f"t{i}:0", task_id=f"t{i}", trial_index=0, total_index=i)

    size = display._main_region_size(reserved=0)
    assert size == 5  # 3 cards + 2 border rows


def test_main_region_size_caps_at_viewport_when_trials_overflow() -> None:
    """Many trials in a 12-row terminal cap the main region below the desired
    size so the bottom bar remains visible."""

    class _StubConsole:
        height = 12

    class _StubLive:
        def __init__(self) -> None:
            self.console = _StubConsole()

        def update(self, *_a, **_kw) -> None:  # noqa: D401 — stub
            pass

    display = LiveRunDisplay(refresh_per_second=1000, max_trial_rows=50)
    display._live = _StubLive()  # type: ignore[assignment]
    display.run_started(total_trials=50, initial_completed=0)
    for i in range(30):
        display.trial_started(trial_id=f"t{i}:0", task_id=f"t{i}", trial_index=0, total_index=i)

    size = display._main_region_size(reserved=0)
    # 12 - 0 - 1 = 11, capped to at least 3 by design.
    assert size == 11


class _FakeStderr:
    """Minimal ``sys.stderr`` stand-in with an instance-settable ``write``.

    ``io.StringIO`` also satisfies this, but a dedicated fake keeps the
    intent visible: the probe must patch ``.write`` on the stream object
    (not rebind ``sys.stderr``) — see ``_StderrProbe`` docstring.
    """

    def __init__(self) -> None:
        self.buf: list[str] = []

    def write(self, chunk: str) -> int:
        self.buf.append(chunk)
        return len(chunk)

    def flush(self) -> None:
        return None


def _stderr_probe_pentagon_frame(probe_write: Callable[[str], object]) -> None:
    """Named helper so we can assert the probe records the calling frame."""
    probe_write("noise")


def test_stderr_probe_records_wrapped_write_and_delegates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tolokaforge.dx.live_panel import _StderrProbe

    fake = _FakeStderr()
    monkeypatch.setattr(sys, "stderr", fake)
    log_path = tmp_path / "probe.log"

    with _StderrProbe(log_path):
        # Sanity: probe patched the write on the fake stream object.
        assert sys.stderr.write is not _FakeStderr.write
        _stderr_probe_pentagon_frame(sys.stderr.write)

    # Delegation: the wrapped stream still received the chunk.
    assert fake.buf == ["noise"]

    # Log content: chunk repr and calling frame name.
    log_text = log_path.read_text(encoding="utf-8")
    assert "'noise'" in log_text
    assert "_stderr_probe_pentagon_frame" in log_text
    # Stack trace top-frame is the caller file.
    assert __file__ in log_text


def test_stderr_probe_restores_original_write_on_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tolokaforge.dx.live_panel import _StderrProbe

    fake = _FakeStderr()
    monkeypatch.setattr(sys, "stderr", fake)
    log_path = tmp_path / "probe.log"

    with _StderrProbe(log_path):
        tapped_write = sys.stderr.write
        sys.stderr.write("inside")

    # Post-exit: write is no longer the tap.
    assert sys.stderr.write is not tapped_write
    # Subsequent writes still reach the stream — but the log file is not
    # appended to (a fresh record for "after" would grow the file).
    log_size_after_exit = log_path.stat().st_size
    sys.stderr.write("after")
    assert fake.buf == ["inside", "after"]
    assert log_path.stat().st_size == log_size_after_exit


def test_stderr_probe_off_when_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``TOLOKAFORGE_STDERR_PROBE`` is unset, the tap is never installed."""
    monkeypatch.delenv("TOLOKAFORGE_STDERR_PROBE", raising=False)
    display = LiveRunDisplay(refresh_per_second=1000)
    with display:
        assert display._stderr_probe is None


def test_stderr_probe_installed_when_env_var_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When set, ``LiveRunDisplay`` wraps the pre-Live stderr stream."""
    from tolokaforge.dx.live_panel import _StderrProbe

    fake = _FakeStderr()
    monkeypatch.setattr(sys, "stderr", fake)
    log_path = tmp_path / "live_probe.log"
    monkeypatch.setenv("TOLOKAFORGE_STDERR_PROBE", str(log_path))

    display = LiveRunDisplay(refresh_per_second=1000)
    with display:
        assert isinstance(display._stderr_probe, _StderrProbe)
        tapped_write = sys.stderr.write
    # After exit: probe cleared, write no longer the tap, log file exists.
    assert display._stderr_probe is None
    assert sys.stderr.write is not tapped_write
    assert log_path.exists()


def test_main_region_size_reserves_space_for_banner_and_services() -> None:
    class _StubConsole:
        height = 40

    class _StubLive:
        def __init__(self) -> None:
            self.console = _StubConsole()

        def update(self, *_a, **_kw) -> None:  # noqa: D401 — stub
            pass

    display = LiveRunDisplay(refresh_per_second=1000)
    display._live = _StubLive()  # type: ignore[assignment]
    display.run_started(total_trials=2, initial_completed=0)
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0, total_index=1)

    # Reserved 5 (banner) + 5 (services region) leaves 40 - 10 - 1 = 29
    # available; desired = 2 cards + 2 = 4; min wins.
    assert display._main_region_size(reserved=10) == 4


# ---------------------------------------------------------------------------
# Keyboard listener + manual focus mode
# ---------------------------------------------------------------------------


class _FakeStdin:
    """StringIO-shaped stdin with a controllable ``isatty()`` result."""

    def __init__(self, *, isatty: bool = True) -> None:
        self._buffer = io.StringIO()
        self._isatty = isatty

    def isatty(self) -> bool:  # noqa: D401 — simple accessor
        return self._isatty

    def fileno(self) -> int:  # pragma: no cover — never called in these tests
        raise io.UnsupportedOperation("fileno")

    def read(self, size: int) -> str:
        return self._buffer.read(size)


def _seed_three_started_trials(display: LiveRunDisplay) -> None:
    """Fire ``trial_started`` for a, b, c in order; focus lands on c."""
    display.run_started(total_trials=10, initial_completed=0)
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)
    display.trial_started(trial_id="b:0", task_id="b", trial_index=0, total_index=1)
    display.trial_started(trial_id="c:0", task_id="c", trial_index=0, total_index=2)


def test_keyboard_listener_no_ops_when_stdin_not_a_tty() -> None:
    from tolokaforge.dx.live_panel import _KeyboardListener

    display = LiveRunDisplay(refresh_per_second=1000)
    listener = _KeyboardListener(display, stdin=_FakeStdin(isatty=False))
    with listener:
        assert listener.enabled() is False
    # Focus behaviour unchanged: auto-follow still tracks new events.
    _seed_three_started_trials(display)
    assert display._focused_trial_id == "c:0"
    assert display._auto_follow is True


def test_env_var_zero_disables_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    from tolokaforge.dx.live_panel import _INTERACTIVE_PANEL_ENV_VAR, _KeyboardListener

    monkeypatch.setenv(_INTERACTIVE_PANEL_ENV_VAR, "0")
    display = LiveRunDisplay(refresh_per_second=1000)
    listener = _KeyboardListener(display, stdin=_FakeStdin(isatty=True))
    with listener:
        assert listener.enabled() is False


def test_j_focuses_next_visible_trial_and_disables_auto_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    # Visible order is newest-first: [c, b, a]; initial focus on c (index 0).
    assert [card.trial_id for card in display._visible_cards()] == ["c:0", "b:0", "a:0"]
    assert display._focused_trial_id == "c:0"

    display._nav_next_trial()

    assert display._focused_trial_id == "b:0"
    assert display._auto_follow is False


def test_k_focuses_previous_visible_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    # Move to visible-index 1 first so ``k`` has a previous to move back to.
    display._nav_next_trial()
    assert display._focused_trial_id == "b:0"

    display._nav_prev_trial()

    assert display._focused_trial_id == "c:0"
    assert display._auto_follow is False


@pytest.mark.parametrize(
    ("key", "expected_trial_id"),
    [("H", "c:0"), ("L", "a:0")],
)
def test_H_jumps_to_first_visible_and_L_to_last(
    monkeypatch: pytest.MonkeyPatch, key: str, expected_trial_id: str
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    # Visible order is [c, b, a]; H → first (c), L → last (a).
    if key == "H":
        display._nav_first_trial()
    else:
        display._nav_last_trial()

    assert display._focused_trial_id == expected_trial_id
    assert display._auto_follow is False


def test_f_toggles_auto_follow_and_reasserts_current_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    # Move focus off the newest via j.
    display._nav_next_trial()
    assert display._focused_trial_id == "b:0"
    assert display._auto_follow is False

    display._toggle_auto_follow()

    assert display._auto_follow is True
    # Newest lifecycle event is c:0 (last trial_started) — focus snaps back.
    assert display._focused_trial_id == "c:0"


def test_manual_mode_suspends_auto_follow_on_new_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    display._nav_next_trial()
    assert display._focused_trial_id == "b:0"
    assert display._auto_follow is False

    # trial_completed on a DIFFERENT trial must NOT steal focus while
    # manual mode is active.
    display.trial_completed(trial_id="a:0", binary_pass=True, score=1.0)

    assert display._focused_trial_id == "b:0"


def test_listener_restores_termios_on_exit_even_after_exception() -> None:
    if sys.platform == "win32":
        pytest.skip("termios not available on Windows")
    import pty
    import termios

    from tolokaforge.dx.live_panel import _KeyboardListener

    master_fd, slave_fd = pty.openpty()
    try:
        slave = os.fdopen(slave_fd, "r", buffering=1)
        before = termios.tcgetattr(slave.fileno())
        display = LiveRunDisplay(refresh_per_second=1000)
        listener = _KeyboardListener(display, stdin=slave)
        with pytest.raises(RuntimeError, match="boom"):
            with listener:
                assert listener.enabled() is True
                # Verify cbreak actually disabled ICANON (canonical-line
                # buffering) — that's the flag cbreak flips off.
                mid = termios.tcgetattr(slave.fileno())
                assert mid[3] & termios.ICANON == 0
                raise RuntimeError("boom")
        # After the exception + __exit__, ICANON is set again and every
        # non-driver-managed flag matches the pre-cbreak snapshot. The
        # driver reserves bit 0x20000000 (EXTPROC) on macOS PTYs, so mask
        # it out of the comparison to keep the assertion portable.
        after = termios.tcgetattr(slave.fileno())
        assert after[3] & termios.ICANON, "ICANON must be restored"
        _EXTPROC_MASK = 0x20000000
        assert (after[3] & ~_EXTPROC_MASK) == (before[3] & ~_EXTPROC_MASK)
        # Every non-c_lflag field must be byte-identical.
        assert after[:3] == before[:3]
        assert after[4:] == before[4:]
        # Listener also cleared its saved settings on exit.
        assert listener._original_termios is None
    finally:
        os.close(master_fd)


def test_bottom_bar_hint_appears_in_manual_mode_and_hides_in_auto_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    display.run_started(total_trials=5, initial_completed=0)
    display.trial_started(trial_id="a:0", task_id="a", trial_index=0, total_index=0)

    hint = "[j/k or ↑↓ nav · H/L first/last · f follow · l logs]"

    # Auto-follow ON → hint absent, output byte-identical to pre-Stage-B.
    display._auto_follow = True
    auto = display._render_bottom_bar()
    assert hint not in auto.plain

    # Auto-follow OFF while trials have started → hint present.
    display._auto_follow = False
    manual = display._render_bottom_bar()
    assert hint in manual.plain


# ---------------------------------------------------------------------------
# Per-trial log stream — ctxvar stamping + `l` toggle + filtered render
# ---------------------------------------------------------------------------


def _push_tagged_record(
    display: LiveRunDisplay, level: int, message: str, trial_id: str | None
) -> None:
    record = _make_log_record(level, message)
    if trial_id is not None:
        record.trial_id = trial_id  # type: ignore[attr-defined]
    display._log_buffer.append(record)


def test_log_sink_stamps_trial_id_from_ctxvar() -> None:
    from collections import deque

    from tolokaforge.core.logging_context import TRIAL_ID_CTXVAR
    from tolokaforge.dx.live_panel import _LogSink

    buffer: deque[logging.LogRecord] = deque(maxlen=500)
    sink = _LogSink(print_above=lambda _line: None, formatter=None, buffer=buffer)

    token = TRIAL_ID_CTXVAR.set("x:0")
    try:
        sink.emit(_make_log_record(logging.INFO, "in scope"))
    finally:
        TRIAL_ID_CTXVAR.reset(token)
    sink.emit(_make_log_record(logging.INFO, "out of scope"))

    assert getattr(buffer[0], "trial_id", None) == "x:0"
    assert getattr(buffer[1], "trial_id", None) is None


def test_log_sink_preserves_pre_tagged_trial_id() -> None:
    from collections import deque

    from tolokaforge.core.logging_context import TRIAL_ID_CTXVAR
    from tolokaforge.dx.live_panel import _LogSink

    buffer: deque[logging.LogRecord] = deque(maxlen=500)
    sink = _LogSink(print_above=lambda _line: None, formatter=None, buffer=buffer)

    token = TRIAL_ID_CTXVAR.set("x:0")
    try:
        # Simulates the Docker logging pipeline's ``extra={"trial_id": ...}``.
        record = _make_log_record(logging.INFO, "docker line")
        record.trial_id = "y:0"  # type: ignore[attr-defined]
        sink.emit(record)
    finally:
        TRIAL_ID_CTXVAR.reset(token)

    assert getattr(buffer[0], "trial_id", None) == "y:0"


def test_l_toggles_log_pane_state() -> None:
    from tolokaforge.dx.live_panel import _KeyboardListener

    display = LiveRunDisplay(refresh_per_second=1000)
    listener = _KeyboardListener(display, stdin=_FakeStdin(isatty=True))
    auto_before = display._auto_follow
    assert display._show_logs_pane is False

    listener._dispatch("l")
    assert display._show_logs_pane is True

    listener._dispatch("l")
    assert display._show_logs_pane is False
    # `l` is orthogonal to auto-follow.
    assert display._auto_follow is auto_before


def test_render_right_pane_shows_filtered_logs_when_toggled() -> None:
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    display._focused_trial_id = "b:0"
    _push_tagged_record(display, logging.INFO, "alpha only", "a:0")
    _push_tagged_record(display, logging.INFO, "hello from b", "b:0")
    _push_tagged_record(display, logging.INFO, "untagged noise", None)

    display._show_logs_pane = True
    body = _render_right_pane_text(display)

    assert "hello from b" in body
    assert "alpha only" not in body
    assert "untagged noise" not in body


def test_render_right_pane_log_view_empty_state() -> None:
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    display._focused_trial_id = "b:0"
    _push_tagged_record(display, logging.INFO, "alpha only", "a:0")
    _push_tagged_record(display, logging.INFO, "gamma only", "c:0")

    display._show_logs_pane = True
    body = _render_right_pane_text(display)

    assert "(no log records yet for this trial)" in body


def test_render_right_pane_reverts_to_summary_when_toggled_off() -> None:
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    _push_tagged_record(display, logging.INFO, "hello from c", "c:0")

    display._toggle_log_pane()
    display._toggle_log_pane()
    body = _render_right_pane_text(display)

    assert "turn 0" in body
    assert "in 0 / out 0 tok" in body
    assert "(no log records yet for this trial)" not in body


def test_bottom_bar_hint_includes_logs_key_in_manual_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)

    display._nav_next_trial()  # `j` → manual mode
    bar = display._render_bottom_bar()

    assert "l logs" in bar.plain


def test_render_right_pane_shows_last_twenty_records_only() -> None:
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    for i in range(30):
        _push_tagged_record(display, logging.INFO, f"rec-{i:02d}", "c:0")

    display._show_logs_pane = True
    body = _render_right_pane_text(display)

    assert "rec-29" in body
    assert "rec-10" in body
    # rec-00..rec-09 scroll off — only the last 20 records render.
    assert "rec-09" not in body


# ---------------------------------------------------------------------------
# Raw-fd read loop + ESC-sequence parsing (Fixes 1 & 2)
# ---------------------------------------------------------------------------


class _PipeStdin:
    """stdin-shaped wrapper over an ``os.pipe()`` read-end fd.

    ``isatty`` lies True so the listener's start guards pass; ``fileno``
    returns the read-end so ``termios``/``os.read`` in the listener act on
    the pipe.
    """

    def __init__(self, read_fd: int) -> None:
        self._fd = read_fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    """Poll ``predicate`` until True or ``timeout`` elapses — deterministic
    substitute for sleeping on the listener thread's read timing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _spawn_pipe_listener(
    display: LiveRunDisplay,
) -> tuple[object, threading.Thread, int]:
    """Start a listener's read loop on an ``os.pipe()`` read-end, bypassing
    the termios setup (a pipe is not a tty). ``_stdin`` is a real buffered
    file object so the falsification revert (``self._stdin.read(1)``) still
    exercises the userspace-buffering stranding bug. Returns
    ``(listener, thread, write_fd)``."""
    from tolokaforge.dx.live_panel import _KeyboardListener

    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r")
    listener = _KeyboardListener(display, stdin=stdin)
    listener._fd = read_fd
    listener._enabled = True
    thread = threading.Thread(target=listener._run, name="test-panel-input", daemon=True)
    thread.start()
    return listener, thread, write_fd


def _stop_pipe_listener(listener: object, thread: threading.Thread, write_fd: int | None) -> None:
    if write_fd is not None:
        try:
            os.close(write_fd)
        except OSError:
            pass
    listener._stop_event.set()  # type: ignore[attr-defined]
    thread.join(timeout=1.0)
    listener._stdin.close()  # type: ignore[attr-defined]


def test_burst_input_dispatches_every_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    # Visible [c, b, a]; focus starts on c:0.
    listener, thread, write_fd = _spawn_pipe_listener(display)
    try:
        # j j k j from c → b → a → b → a in one syscall's worth of bytes.
        os.write(write_fd, b"jjkj")
        moved = _wait_until(lambda: display._focused_trial_id == "a:0")
        assert moved, f"expected focus a:0 after burst, got {display._focused_trial_id}"
    finally:
        _stop_pipe_listener(listener, thread, write_fd)


def test_arrow_up_maps_to_prev_arrow_down_to_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    listener, thread, write_fd = _spawn_pipe_listener(display)
    try:
        os.write(write_fd, b"\x1b[B")  # down → next
        assert _wait_until(lambda: display._focused_trial_id == "b:0")
        os.write(write_fd, b"\x1b[A")  # up → prev
        assert _wait_until(lambda: display._focused_trial_id == "c:0")
    finally:
        _stop_pipe_listener(listener, thread, write_fd)


def test_arrow_left_right_map_to_prev_next(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    listener, thread, write_fd = _spawn_pipe_listener(display)
    try:
        os.write(write_fd, b"\x1b[C")  # right → next
        assert _wait_until(lambda: display._focused_trial_id == "b:0")
        os.write(write_fd, b"\x1b[D")  # left → prev
        assert _wait_until(lambda: display._focused_trial_id == "c:0")
    finally:
        _stop_pipe_listener(listener, thread, write_fd)


@pytest.mark.parametrize("home_bytes", [b"\x1b[H", b"\x1bOH"])
def test_home_key_csi_and_ss3_variants_focus_first(
    monkeypatch: pytest.MonkeyPatch, home_bytes: bytes
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    listener, thread, write_fd = _spawn_pipe_listener(display)
    try:
        os.write(write_fd, b"\x1b[B")  # move off first (c → b)
        assert _wait_until(lambda: display._focused_trial_id == "b:0")
        os.write(write_fd, home_bytes)  # Home → first visible (c)
        assert _wait_until(lambda: display._focused_trial_id == "c:0")
    finally:
        _stop_pipe_listener(listener, thread, write_fd)


@pytest.mark.parametrize("end_bytes", [b"\x1b[F", b"\x1bOF"])
def test_end_key_csi_and_ss3_variants_focus_last(
    monkeypatch: pytest.MonkeyPatch, end_bytes: bytes
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    listener, thread, write_fd = _spawn_pipe_listener(display)
    try:
        os.write(write_fd, end_bytes)  # End → last visible (a)
        assert _wait_until(lambda: display._focused_trial_id == "a:0")
    finally:
        _stop_pipe_listener(listener, thread, write_fd)


def test_unknown_esc_sequence_is_dropped_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    listener, thread, write_fd = _spawn_pipe_listener(display)
    try:
        # Shift-Tab (\x1b[Z) is unknown; the trailing j must still dispatch
        # exactly one move, proving the unknown run was dropped, not queued.
        os.write(write_fd, b"\x1b[Zj")
        assert _wait_until(lambda: display._focused_trial_id == "b:0")
        assert display._focused_trial_id == "b:0"
        assert thread.is_alive()
    finally:
        _stop_pipe_listener(listener, thread, write_fd)


def test_bare_esc_press_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)
    listener, thread, write_fd = _spawn_pipe_listener(display)
    try:
        os.write(write_fd, b"\x1b")
        # Let the read-loop's select timeout fire so the lone ESC is flushed.
        time.sleep(0.25)
        assert display._focused_trial_id == "c:0"
        assert display._auto_follow is True
        # A later keypress is unaffected by the dropped ESC — j moves once.
        os.write(write_fd, b"j")
        assert _wait_until(lambda: display._focused_trial_id == "b:0")
    finally:
        _stop_pipe_listener(listener, thread, write_fd)


def test_pipe_eof_stops_listener_thread() -> None:
    display = LiveRunDisplay(refresh_per_second=1000)
    listener, thread, write_fd = _spawn_pipe_listener(display)
    try:
        # Closing the write end makes os.read return b"" — the thread must
        # exit on its own, without _stop_event being set.
        os.close(write_fd)
        assert _wait_until(lambda: not thread.is_alive())
    finally:
        _stop_pipe_listener(listener, thread, None)


def test_l_before_first_trial_shows_placeholder() -> None:
    display = LiveRunDisplay(refresh_per_second=1000)

    default_body = _render_right_pane_text(display)
    assert "(waiting for first trial)" in default_body

    display._show_logs_pane = True
    logs_body = _render_right_pane_text(display)
    assert "log stream enabled" in logs_body


def test_bottom_bar_hint_advertises_first_last_shortcuts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_incrementing_now(monkeypatch)
    display = LiveRunDisplay(refresh_per_second=1000)
    _seed_three_started_trials(display)

    display._nav_next_trial()  # enter manual mode
    bar = display._render_bottom_bar()

    assert "H/L" in bar.plain
    assert "↑↓" in bar.plain


def test_listener_disabled_when_tcgetattr_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios

    from tolokaforge.dx.live_panel import _KeyboardListener

    def _boom(_fd: int) -> list[object]:
        raise termios.error("no tty here")

    monkeypatch.setattr("tolokaforge.dx.live_panel.termios.tcgetattr", _boom)
    display = LiveRunDisplay(refresh_per_second=1000)
    read_fd, write_fd = os.pipe()
    try:
        listener = _KeyboardListener(display, stdin=_PipeStdin(read_fd))
        with listener:
            assert listener.enabled() is False
            assert listener._thread is None
            assert listener._original_termios is None
    finally:
        os.close(read_fd)
        os.close(write_fd)
