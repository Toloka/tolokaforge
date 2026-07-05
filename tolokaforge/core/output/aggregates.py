"""Run-level aggregate writer — Protocol + disk-backed implementation.

The writer covers the four run-level JSON artifacts produced once per
run at the run-output root:

* ``per_task_metrics.json`` — per-task metric rows (success rate, pass@k,
  latency / token / cost aggregates, plus task metadata).
* ``aggregate.json`` — single run-level aggregate, carries
  ``schema_version: 1``.
* ``metadata_slices.json`` — aggregates sliced by benchmark type,
  complexity, tag, and expected failure mode.
* ``failure_attribution.json`` — deterministic failure-attribution
  report: ``{"summary": ..., "failures": [...]}``.

These artifacts live at the run-output root (next to ``trials/``); they
are the run-level analogue of the per-trial files written by
:class:`tolokaforge.core.output.artifacts.TrialArtifactWriter`.

* :class:`RunAggregateWriter` — Protocol the orchestrator depends on.
* :class:`FileAggregateWriter` — disk-backed implementation.
* :class:`InMemoryAggregateWriter` — non-disk implementation used as a
  test fixture and as proof the seam is swappable.
* :class:`RunAggregateBundle` — the per-run record the in-memory writer
  populates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "FileAggregateWriter",
    "InMemoryAggregateWriter",
    "RunAggregateBundle",
    "RunAggregateWriter",
]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RunAggregateWriter(Protocol):
    """Writes run-level aggregate artifacts. The orchestrator depends on
    this Protocol; alternative implementations (in-memory, remote object
    store, …) satisfy the same contract.
    """

    def write_per_task_metrics(
        self,
        output_dir: Path,
        metrics: list[dict[str, Any]],
    ) -> None:
        """Persist ``output_dir/per_task_metrics.json`` from *metrics*."""
        ...

    def write_aggregate(
        self,
        output_dir: Path,
        aggregate: dict[str, Any],
    ) -> None:
        """Persist ``output_dir/aggregate.json`` from *aggregate*.

        Wire-format invariants (also pinned by the canonical tests on
        :class:`~tolokaforge.core.output.aggregate_models.RunAggregate`):

        * **``schema_version`` is always present** on the written JSON.
          Callers passing a dict must set it before calling. Callers
          passing a :class:`~tolokaforge.core.output.aggregate_models.RunAggregate`
          model get this for free — the model's serializer forces the
          field into the dump regardless of ``exclude_unset``.
        * **Numeric fields preserve source type.** Values the producer
          emitted as ``int`` (e.g. ``sum([]) == 0`` for empty
          aggregates) stay ``int`` on the wire; values emitted as
          ``float`` stay ``float``. The model achieves this via
          ``int | float`` unions; the direct dict path preserves it
          naturally.
        """
        ...

    def write_metadata_slices(
        self,
        output_dir: Path,
        slices: dict[str, dict[str, Any]],
    ) -> None:
        """Persist ``output_dir/metadata_slices.json`` from *slices*.

        *slices* is a ``{slice_dimension: {slice_key: aggregate_dict}}``
        mapping — one entry per dimension (``by_benchmark_type``,
        ``by_complexity``, ``by_tag``, ``by_expected_failure_mode``).
        """
        ...

    def write_failure_attribution(
        self,
        output_dir: Path,
        attribution: dict[str, Any],
    ) -> None:
        """Persist ``output_dir/failure_attribution.json`` from *attribution*.

        *attribution* is the orchestrator-assembled envelope
        ``{"summary": ..., "failures": [...]}``.
        """
        ...

    def write_run_aggregates(
        self,
        output_dir: Path,
        per_task_metrics: list[dict[str, Any]],
        aggregate: dict[str, Any],
        metadata_slices: dict[str, dict[str, Any]],
        failure_attribution: dict[str, Any],
    ) -> None:
        """Write the four run-level aggregate artifacts in one call:
        ``per_task_metrics.json``, ``aggregate.json``,
        ``metadata_slices.json``, ``failure_attribution.json``.
        Convenience for the common orchestrator path.
        """
        ...


# ---------------------------------------------------------------------------
# FileAggregateWriter — disk-backed implementation
# ---------------------------------------------------------------------------


def _dump_json(target: Path, payload: object) -> None:
    """Write *payload* to *target* with the orchestrator's serializer
    conventions: ``indent=2`` and ``default=str`` so Enums / datetimes /
    other non-JSON-native values stringify deterministically.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


class FileAggregateWriter:
    """Disk-backed :class:`RunAggregateWriter`.

    Each artifact is one JSON file at ``output_dir``. Overwrites
    unconditionally — the run-output root is owned by this run.
    """

    def write_per_task_metrics(
        self,
        output_dir: Path,
        metrics: list[dict[str, Any]],
    ) -> None:
        _dump_json(Path(output_dir) / "per_task_metrics.json", metrics)

    def write_aggregate(
        self,
        output_dir: Path,
        aggregate: dict[str, Any],
    ) -> None:
        _dump_json(Path(output_dir) / "aggregate.json", aggregate)

    def write_metadata_slices(
        self,
        output_dir: Path,
        slices: dict[str, dict[str, Any]],
    ) -> None:
        _dump_json(Path(output_dir) / "metadata_slices.json", slices)

    def write_failure_attribution(
        self,
        output_dir: Path,
        attribution: dict[str, Any],
    ) -> None:
        _dump_json(Path(output_dir) / "failure_attribution.json", attribution)

    def write_run_aggregates(
        self,
        output_dir: Path,
        per_task_metrics: list[dict[str, Any]],
        aggregate: dict[str, Any],
        metadata_slices: dict[str, dict[str, Any]],
        failure_attribution: dict[str, Any],
    ) -> None:
        self.write_per_task_metrics(output_dir, per_task_metrics)
        self.write_aggregate(output_dir, aggregate)
        self.write_metadata_slices(output_dir, metadata_slices)
        self.write_failure_attribution(output_dir, failure_attribution)


# ---------------------------------------------------------------------------
# InMemoryAggregateWriter — non-disk implementation, test fixture
# ---------------------------------------------------------------------------


@dataclass
class RunAggregateBundle:
    """The artifacts an :class:`InMemoryAggregateWriter` records for one run.

    Each attribute holds the most recent value written for that artifact
    name, or ``None`` if the corresponding ``write_*`` method has not
    been called for this run.

    ``write_run_aggregates`` populates all four slots. Per-piece writers
    only touch their own slot.
    """

    per_task_metrics: list[dict[str, Any]] | None = None
    aggregate: dict[str, Any] | None = None
    metadata_slices: dict[str, dict[str, Any]] | None = None
    failure_attribution: dict[str, Any] | None = None


class InMemoryAggregateWriter:
    """In-memory :class:`RunAggregateWriter`.

    Stores each run's aggregates in ``self.runs`` keyed by ``output_dir``.
    Use as a test fixture when code requires a writer but the assertion
    is about what was written, not about a filesystem layout.

    Dicts / lists passed in are stored by reference, not copied —
    matching how :class:`FileAggregateWriter` would serialise whatever
    state existed at write time.

    The bundle key is ``Path(output_dir)`` as supplied — *not*
    ``.resolve()``-d. Two surface forms of the same logical path
    (``Path("a/b")`` vs ``Path("./a/b")``) bucket into separate runs
    here; tests should pass canonical paths (typically ``tmp_path``) so
    the divergence doesn't surface.
    """

    def __init__(self) -> None:
        self.runs: dict[Path, RunAggregateBundle] = {}

    def _bundle(self, output_dir: Path) -> RunAggregateBundle:
        key = Path(output_dir)
        if key not in self.runs:
            self.runs[key] = RunAggregateBundle()
        return self.runs[key]

    def write_per_task_metrics(
        self,
        output_dir: Path,
        metrics: list[dict[str, Any]],
    ) -> None:
        self._bundle(output_dir).per_task_metrics = metrics

    def write_aggregate(
        self,
        output_dir: Path,
        aggregate: dict[str, Any],
    ) -> None:
        self._bundle(output_dir).aggregate = aggregate

    def write_metadata_slices(
        self,
        output_dir: Path,
        slices: dict[str, dict[str, Any]],
    ) -> None:
        self._bundle(output_dir).metadata_slices = slices

    def write_failure_attribution(
        self,
        output_dir: Path,
        attribution: dict[str, Any],
    ) -> None:
        self._bundle(output_dir).failure_attribution = attribution

    def write_run_aggregates(
        self,
        output_dir: Path,
        per_task_metrics: list[dict[str, Any]],
        aggregate: dict[str, Any],
        metadata_slices: dict[str, dict[str, Any]],
        failure_attribution: dict[str, Any],
    ) -> None:
        bundle = self._bundle(output_dir)
        bundle.per_task_metrics = per_task_metrics
        bundle.aggregate = aggregate
        bundle.metadata_slices = metadata_slices
        bundle.failure_attribution = failure_attribution
