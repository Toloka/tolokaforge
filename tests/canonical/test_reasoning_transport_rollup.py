"""Canonical test — what a run says about the reasoning it was billed for.

The run-level rollup is the only place an operator sees this without reading
per-trial files. Its two counts answer different questions and must not be
collapsed: recovery means a preset reads a narrower channel than its model uses
and nothing was lost; an unknown channel means the provider billed for
deliberation that arrived nowhere the engine looks, which is lost.

The case this pins hardest is the silent one. An opaque ``reasoning.encrypted``
payload is billed and kept as nothing, and that is correct — it holds no text
anyone could keep. The counter it replaced fired on exactly those calls, so it
was loudest about the routes that were behaving.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tolokaforge.core.models.trajectory import Metrics, Trajectory
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.output.aggregate_models import ReasoningTransportRollup

pytestmark = pytest.mark.canonical


def _trial(*, recovered: int = 0, unknown: int = 0) -> Trajectory:
    return Trajectory(
        task_id="t",
        trial_index=0,
        messages=[],
        start_ts=datetime(2026, 10, 2, tzinfo=UTC),
        end_ts=datetime(2026, 10, 2, tzinfo=UTC),
        metrics=Metrics(
            reasoning_recovered_by_fallback=recovered,
            reasoning_channel_unknown=unknown,
        ),
    )


def _rollup(*trials: Trajectory) -> dict[str, int]:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.results = list(trials)
    return orchestrator._reasoning_transport_rollup()


def test_calls_and_trials_are_counted_separately() -> None:
    """A single chatty trial and a run-wide problem look the same in calls."""
    got = _rollup(_trial(recovered=7), _trial(), _trial(recovered=1))

    assert got["recovered_by_fallback_calls"] == 8
    assert got["recovered_by_fallback_trials"] == 2


def test_a_run_that_lost_nothing_reports_zeros() -> None:
    got = _rollup(_trial(), _trial())

    assert got == {
        "recovered_by_fallback_calls": 0,
        "recovered_by_fallback_trials": 0,
        "channel_unknown_calls": 0,
        "channel_unknown_trials": 0,
    }


def test_the_two_signals_stay_apart() -> None:
    """Summing them would report a loss on a run that lost nothing."""
    got = _rollup(_trial(recovered=3), _trial(unknown=2))

    assert got["recovered_by_fallback_trials"] == 1
    assert got["channel_unknown_trials"] == 1
    assert got["recovered_by_fallback_calls"] == 3
    assert got["channel_unknown_calls"] == 2


def test_the_rollup_is_exactly_what_the_aggregate_accepts() -> None:
    """``RunAggregate`` forbids extra keys, so a rollup key the envelope does
    not declare fails the whole run's aggregate rather than being dropped."""
    got = _rollup(_trial(recovered=1, unknown=1))

    assert ReasoningTransportRollup(**got).model_dump() == got
