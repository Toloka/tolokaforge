"""Hard caps for :meth:`Orchestrator.run` — cost, wall-time, sample count.

Every budget is a stateful tracker with a uniform surface. The orchestrator
holds ONE :class:`CompositeBudget`, calls its ``record_generation_cost`` on
trials that produced a cost and ``record_trial_terminated`` on every terminal
queue transition, then polls it at the "should we schedule more work" gate.
On hit, the returned :class:`BudgetHit` names which limit fired and its
accumulated value; the orchestrator writes it to ``LIMIT_HIT.json`` under
the run directory via :func:`write_limit_hit_marker` and lets in-flight
trials drain.

Trackers are thread-safe — the orchestrator's ThreadPoolExecutor completes
futures from multiple worker threads, and the wait-loop's ``record_*`` +
``poll`` calls happen on the same thread as the futures land.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

__all__ = [
    "BudgetHit",
    "BudgetKind",
    "BudgetTracker",
    "CompositeBudget",
    "CostBudget",
    "LimitHitMarker",
    "SampleBudget",
    "TimeBudget",
    "make_budget",
    "write_limit_hit_marker",
]

BudgetKind = Literal["cost", "time", "sample"]


@dataclass(frozen=True)
class BudgetHit:
    """A budget crossed its threshold.

    ``timestamp`` is Unix epoch seconds; :func:`write_limit_hit_marker`
    converts it to ISO 8601 UTC for the on-disk marker.
    """

    which: BudgetKind
    threshold: float
    value_at_hit: float
    timestamp: float


class BudgetTracker(Protocol):
    """One limit tracker.

    ``poll()`` is idempotent — the first crossing freezes a
    :class:`BudgetHit` on the tracker and every subsequent call returns
    the same value.
    """

    def record_generation_cost(self, cost_delta_usd: float) -> None: ...

    def record_trial_terminated(self) -> None: ...

    def poll(self) -> BudgetHit | None: ...


class CostBudget:
    """Fires when cumulative agent cost crosses ``limit_usd``.

    ``initial_cost_usd`` seeds the tracker for resumed runs so cost
    already spent on prior invocations counts against the cap.
    """

    def __init__(self, *, limit_usd: float, initial_cost_usd: float = 0.0) -> None:
        self._limit = float(limit_usd)
        self._value = float(initial_cost_usd)
        self._lock = threading.Lock()
        self._hit: BudgetHit | None = None

    def record_generation_cost(self, cost_delta_usd: float) -> None:
        with self._lock:
            self._value += float(cost_delta_usd)

    def record_trial_terminated(self) -> None:
        return None

    def poll(self) -> BudgetHit | None:
        with self._lock:
            if self._hit is not None:
                return self._hit
            if self._value >= self._limit:
                self._hit = BudgetHit(
                    which="cost",
                    threshold=self._limit,
                    value_at_hit=self._value,
                    timestamp=time.time(),
                )
            return self._hit


class TimeBudget:
    """Fires when ``time.monotonic() - start`` crosses ``limit_seconds``.

    ``start`` is set on the first ``record_*`` call, whichever comes
    first. Task loading (which may take tens of seconds on large
    projects) does not count against the time budget; the clock starts
    when the run enters its execution phase.
    """

    def __init__(self, *, limit_seconds: float) -> None:
        self._limit = float(limit_seconds)
        self._start: float | None = None
        self._lock = threading.Lock()
        self._hit: BudgetHit | None = None

    def _start_clock_if_needed(self) -> None:
        if self._start is None:
            self._start = time.monotonic()

    def record_generation_cost(self, cost_delta_usd: float) -> None:
        with self._lock:
            self._start_clock_if_needed()

    def record_trial_terminated(self) -> None:
        with self._lock:
            self._start_clock_if_needed()

    def poll(self) -> BudgetHit | None:
        with self._lock:
            if self._hit is not None:
                return self._hit
            if self._start is None:
                return None
            elapsed = time.monotonic() - self._start
            if elapsed >= self._limit:
                self._hit = BudgetHit(
                    which="time",
                    threshold=self._limit,
                    value_at_hit=elapsed,
                    timestamp=time.time(),
                )
            return self._hit


class SampleBudget:
    """Fires when ``record_trial_terminated`` has been called ``limit`` times.

    "Terminated" = a trial reached a terminal queue state (completed OR
    retry-exhausted). Started-but-not-terminated trials do not count —
    fast-fail trials would otherwise inflate the counter and cap a run
    before ``limit`` trials produced any output.
    """

    def __init__(self, *, limit: int) -> None:
        self._limit = int(limit)
        self._count = 0
        self._lock = threading.Lock()
        self._hit: BudgetHit | None = None

    def record_generation_cost(self, cost_delta_usd: float) -> None:
        return None

    def record_trial_terminated(self) -> None:
        with self._lock:
            self._count += 1

    def poll(self) -> BudgetHit | None:
        with self._lock:
            if self._hit is not None:
                return self._hit
            if self._count >= self._limit:
                self._hit = BudgetHit(
                    which="sample",
                    threshold=float(self._limit),
                    value_at_hit=float(self._count),
                    timestamp=time.time(),
                )
            return self._hit


class CompositeBudget:
    """First-tracker-to-hit wins.

    ``record_*`` fans out to every child; ``poll`` walks the children in
    construction order and returns the first :class:`BudgetHit`.
    """

    def __init__(self, trackers: list[BudgetTracker]) -> None:
        if not trackers:
            raise ValueError("CompositeBudget requires at least one tracker")
        self._trackers: list[BudgetTracker] = list(trackers)

    @property
    def trackers(self) -> tuple[BudgetTracker, ...]:
        return tuple(self._trackers)

    def record_generation_cost(self, cost_delta_usd: float) -> None:
        for tracker in self._trackers:
            tracker.record_generation_cost(cost_delta_usd)

    def record_trial_terminated(self) -> None:
        for tracker in self._trackers:
            tracker.record_trial_terminated()

    def poll(self) -> BudgetHit | None:
        for tracker in self._trackers:
            hit = tracker.poll()
            if hit is not None:
                return hit
        return None


def make_budget(
    *,
    cost_limit_usd: float | None,
    time_limit_seconds: float | None,
    sample_limit: int | None,
    initial_cost_usd: float = 0.0,
) -> CompositeBudget | None:
    """Assemble the composite from CLI-resolved kwargs.

    Returns ``None`` when every kwarg is ``None`` so the orchestrator can
    branch once at the call site rather than pay for no-op ``record_*``
    calls inside the hot loop.
    """
    trackers: list[BudgetTracker] = []
    if cost_limit_usd is not None:
        trackers.append(CostBudget(limit_usd=cost_limit_usd, initial_cost_usd=initial_cost_usd))
    if time_limit_seconds is not None:
        trackers.append(TimeBudget(limit_seconds=time_limit_seconds))
    if sample_limit is not None:
        trackers.append(SampleBudget(limit=sample_limit))
    if not trackers:
        return None
    return CompositeBudget(trackers)


class LimitHitMarker(BaseModel):
    """On-disk shape of ``LIMIT_HIT.json``.

    Written by the orchestrator on the first budget hit; read by the CLI
    to shape the end banner. ``timestamp`` is ISO 8601 UTC with a ``Z``
    suffix (``2026-07-15T12:34:56Z``).
    """

    model_config = ConfigDict(extra="forbid")

    which: BudgetKind
    threshold: float
    value_at_hit: float
    timestamp: str


LIMIT_HIT_MARKER_FILENAME = "LIMIT_HIT.json"


def write_limit_hit_marker(output_dir: Path, hit: BudgetHit) -> Path:
    """Write ``output_dir / LIMIT_HIT.json`` and return the written path.

    Overwrites an existing marker so a resumed run that hits a fresh
    limit reflects the current state.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    marker = LimitHitMarker(
        which=hit.which,
        threshold=hit.threshold,
        value_at_hit=hit.value_at_hit,
        timestamp=datetime.fromtimestamp(hit.timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    marker_path = output_dir / LIMIT_HIT_MARKER_FILENAME
    marker_path.write_text(json.dumps(marker.model_dump(), indent=2) + "\n")
    return marker_path
