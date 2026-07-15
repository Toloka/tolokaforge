"""Unit tests for :mod:`tolokaforge.core.budgets`.

Locks the tracker contracts a stage-later CLI + orchestrator rely on:

- ``CostBudget`` / ``TimeBudget`` / ``SampleBudget`` fire at their
  respective thresholds, freeze the resulting :class:`BudgetHit`, and
  keep returning the frozen hit on every subsequent ``poll``.
- ``CompositeBudget`` fans out ``record_*`` and returns the first
  child's hit (short-circuit on any single budget).
- ``make_budget`` returns ``None`` iff every limit is unset, and builds
  a composite with the exact tracker set the CLI resolved.
- Concurrent ``record_*`` calls from many threads are atomic (no
  interleaved-update races on the aggregate counters).
"""

from __future__ import annotations

import threading

import pytest

from tolokaforge.core.budgets import (
    BudgetHit,
    CompositeBudget,
    CostBudget,
    SampleBudget,
    TimeBudget,
    make_budget,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# CostBudget
# ---------------------------------------------------------------------------


class TestCostBudget:
    def test_below_threshold_returns_none(self) -> None:
        budget = CostBudget(limit_usd=1.0)
        budget.record_generation_cost(0.4)
        assert budget.poll() is None

    def test_seeded_below_threshold_returns_none(self) -> None:
        """``initial_cost_usd`` counts against the cap on the first ``poll``."""
        budget = CostBudget(limit_usd=1.0, initial_cost_usd=0.5)
        budget.record_generation_cost(0.4)
        assert budget.poll() is None

    def test_crossing_threshold_freezes_hit(self) -> None:
        budget = CostBudget(limit_usd=1.0, initial_cost_usd=0.5)
        budget.record_generation_cost(0.4)
        budget.record_generation_cost(0.2)  # crosses at 1.1
        hit = budget.poll()
        assert hit is not None
        assert hit.which == "cost"
        assert hit.threshold == pytest.approx(1.0)
        assert hit.value_at_hit == pytest.approx(1.1)

    def test_poll_is_idempotent_after_hit(self) -> None:
        budget = CostBudget(limit_usd=1.0)
        budget.record_generation_cost(1.5)
        first = budget.poll()
        assert first is not None
        # More cost accumulated after the hit — poll returns the FROZEN hit.
        budget.record_generation_cost(0.5)
        assert budget.poll() is first

    def test_record_trial_terminated_is_noop(self) -> None:
        """Sample counting is not this tracker's job."""
        budget = CostBudget(limit_usd=1.0)
        for _ in range(10):
            budget.record_trial_terminated()
        assert budget.poll() is None


# ---------------------------------------------------------------------------
# TimeBudget
# ---------------------------------------------------------------------------


class TestTimeBudget:
    def test_poll_before_clock_start_returns_none(self) -> None:
        """The clock starts on the first ``record_*``; a ``poll`` before
        that is a no-op (task loading time doesn't count)."""
        budget = TimeBudget(limit_seconds=60.0)
        assert budget.poll() is None

    def test_clock_starts_on_first_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Sequence: [start-clock, poll-1, poll-2].
        clock = iter([100.0, 130.0, 161.0])
        monkeypatch.setattr("tolokaforge.core.budgets.time.monotonic", lambda: next(clock))
        budget = TimeBudget(limit_seconds=60.0)
        budget.record_generation_cost(0.001)  # start clock at t=100
        assert budget.poll() is None  # elapsed = 30
        hit = budget.poll()  # elapsed = 61 — fires
        assert hit is not None
        assert hit.which == "time"
        assert hit.threshold == pytest.approx(60.0)
        assert hit.value_at_hit == pytest.approx(61.0)

    def test_record_trial_terminated_also_starts_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sequence: [start-clock, poll-1, poll-2].
        clock = iter([50.0, 60.0, 111.0])
        monkeypatch.setattr("tolokaforge.core.budgets.time.monotonic", lambda: next(clock))
        budget = TimeBudget(limit_seconds=60.0)
        budget.record_trial_terminated()  # start at t=50
        assert budget.poll() is None  # elapsed = 10
        hit = budget.poll()  # elapsed = 61 — fires
        assert hit is not None
        assert hit.which == "time"

    def test_poll_is_idempotent_after_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = iter([0.0, 100.0, 200.0])
        monkeypatch.setattr("tolokaforge.core.budgets.time.monotonic", lambda: next(clock))
        budget = TimeBudget(limit_seconds=60.0)
        budget.record_generation_cost(0.001)  # start clock at t=0
        first = budget.poll()  # elapsed = 100 — fires with value 100
        assert first is not None
        assert first.value_at_hit == pytest.approx(100.0)
        # Even though time moved to 200, the hit is frozen.
        assert budget.poll() is first


# ---------------------------------------------------------------------------
# SampleBudget
# ---------------------------------------------------------------------------


class TestSampleBudget:
    def test_below_threshold_returns_none(self) -> None:
        budget = SampleBudget(limit=3)
        budget.record_trial_terminated()
        budget.record_trial_terminated()
        assert budget.poll() is None

    def test_crossing_threshold_freezes_hit(self) -> None:
        budget = SampleBudget(limit=3)
        for _ in range(3):
            budget.record_trial_terminated()
        hit = budget.poll()
        assert hit is not None
        assert hit.which == "sample"
        assert hit.threshold == pytest.approx(3.0)
        assert hit.value_at_hit == pytest.approx(3.0)

    def test_poll_is_idempotent_after_hit(self) -> None:
        budget = SampleBudget(limit=2)
        budget.record_trial_terminated()
        budget.record_trial_terminated()
        first = budget.poll()
        assert first is not None
        # Extra terminations after the hit — poll returns the FROZEN hit,
        # value_at_hit stays at 2 (not 3).
        budget.record_trial_terminated()
        again = budget.poll()
        assert again is first
        assert again.value_at_hit == pytest.approx(2.0)

    def test_record_generation_cost_is_noop(self) -> None:
        budget = SampleBudget(limit=1)
        budget.record_generation_cost(9999.0)
        assert budget.poll() is None

    def test_concurrent_record_trial_terminated_is_atomic(self) -> None:
        """12 threads × 1000 calls each — final count must be exact."""
        budget = SampleBudget(limit=99_999_999)
        thread_count = 12
        per_thread = 1000

        def worker() -> None:
            for _ in range(per_thread):
                budget.record_trial_terminated()

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Access via a fresh poll after lowering the limit so we can read
        # the counter without exposing it.
        readout = SampleBudget(limit=thread_count * per_thread)
        for _ in range(thread_count * per_thread):
            readout.record_trial_terminated()
        hit = readout.poll()
        assert hit is not None
        assert hit.value_at_hit == thread_count * per_thread

        # And the real budget's count matches — asserted by lowering the
        # limit and confirming a subsequent poll fires with the exact value.
        drained = SampleBudget(limit=thread_count * per_thread)
        drained._count = budget._count  # noqa: SLF001 — private counter cross-check
        hit2 = drained.poll()
        assert hit2 is not None
        assert hit2.value_at_hit == thread_count * per_thread


# ---------------------------------------------------------------------------
# CompositeBudget
# ---------------------------------------------------------------------------


class TestCompositeBudget:
    def test_empty_composite_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one tracker"):
            CompositeBudget([])

    def test_record_fans_out_to_every_child(self) -> None:
        cost = CostBudget(limit_usd=1.0)
        samples = SampleBudget(limit=2)
        composite = CompositeBudget([cost, samples])

        composite.record_generation_cost(0.5)
        composite.record_trial_terminated()
        composite.record_generation_cost(0.5)
        composite.record_trial_terminated()

        # Cost hit at 1.0; sample hit at 2.
        hit = composite.poll()
        assert hit is not None
        # Cost is first in the list — first-tracker-to-hit wins.
        assert hit.which == "cost"

    def test_returns_first_child_to_hit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = iter([0.0, 100.0])
        monkeypatch.setattr("tolokaforge.core.budgets.time.monotonic", lambda: next(clock))
        time_budget = TimeBudget(limit_seconds=1.0)
        cost_budget = CostBudget(limit_usd=1_000_000.0)
        composite = CompositeBudget([time_budget, cost_budget])
        composite.record_generation_cost(0.001)  # start time clock at 0
        hit = composite.poll()  # elapsed=100 → time fires; cost far below
        assert hit is not None
        assert hit.which == "time"

    def test_cost_first_in_list_wins_when_both_cross(self) -> None:
        cost = CostBudget(limit_usd=1.0)
        samples = SampleBudget(limit=1)
        composite = CompositeBudget([cost, samples])
        composite.record_generation_cost(1.5)
        composite.record_trial_terminated()
        hit = composite.poll()
        assert hit is not None
        assert hit.which == "cost"

    def test_sample_first_in_list_wins_when_both_cross(self) -> None:
        samples = SampleBudget(limit=1)
        cost = CostBudget(limit_usd=1.0)
        composite = CompositeBudget([samples, cost])
        composite.record_generation_cost(1.5)
        composite.record_trial_terminated()
        hit = composite.poll()
        assert hit is not None
        assert hit.which == "sample"


# ---------------------------------------------------------------------------
# make_budget
# ---------------------------------------------------------------------------


class TestMakeBudget:
    def test_all_none_returns_none(self) -> None:
        assert (
            make_budget(
                cost_limit_usd=None,
                time_limit_seconds=None,
                sample_limit=None,
            )
            is None
        )

    def test_single_cost_limit_returns_single_tracker_composite(self) -> None:
        budget = make_budget(cost_limit_usd=0.5, time_limit_seconds=None, sample_limit=None)
        assert isinstance(budget, CompositeBudget)
        assert len(budget.trackers) == 1
        assert isinstance(budget.trackers[0], CostBudget)

    def test_single_time_limit_returns_single_tracker_composite(self) -> None:
        budget = make_budget(cost_limit_usd=None, time_limit_seconds=30.0, sample_limit=None)
        assert isinstance(budget, CompositeBudget)
        assert len(budget.trackers) == 1
        assert isinstance(budget.trackers[0], TimeBudget)

    def test_single_sample_limit_returns_single_tracker_composite(self) -> None:
        budget = make_budget(cost_limit_usd=None, time_limit_seconds=None, sample_limit=5)
        assert isinstance(budget, CompositeBudget)
        assert len(budget.trackers) == 1
        assert isinstance(budget.trackers[0], SampleBudget)

    def test_all_three_returns_composite_of_three(self) -> None:
        budget = make_budget(cost_limit_usd=1.0, time_limit_seconds=30.0, sample_limit=5)
        assert isinstance(budget, CompositeBudget)
        assert [type(t) for t in budget.trackers] == [
            CostBudget,
            TimeBudget,
            SampleBudget,
        ]

    def test_initial_cost_seeds_only_cost_budget(self) -> None:
        """``initial_cost_usd`` seeds the ``CostBudget`` and nothing else."""
        budget = make_budget(
            cost_limit_usd=1.0,
            time_limit_seconds=None,
            sample_limit=None,
            initial_cost_usd=0.9,
        )
        assert isinstance(budget, CompositeBudget)
        budget.record_generation_cost(0.2)  # 0.9 + 0.2 = 1.1 crosses
        hit = budget.poll()
        assert hit is not None
        assert hit.which == "cost"
        assert hit.value_at_hit == pytest.approx(1.1)


# ---------------------------------------------------------------------------
# BudgetHit
# ---------------------------------------------------------------------------


def test_budget_hit_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    hit = BudgetHit(which="cost", threshold=1.0, value_at_hit=1.5, timestamp=1234567890.0)
    with pytest.raises(FrozenInstanceError):
        hit.threshold = 2.0  # type: ignore[misc]
