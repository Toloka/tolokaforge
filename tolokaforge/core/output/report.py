"""Pure run-report assembly: trajectories + task metadata → the four aggregate artifacts.

``build_run_report`` is the report-assembly stitch shared by the single-box
orchestrator and the library ``aggregate_results`` surface. It names no live
dependency: it folds a flat list of :class:`Trajectory` into the same
``per_task_metrics`` / ``aggregate`` / ``metadata_slices`` / ``failure_attribution``
shapes a full ``tolokaforge run`` writes, through the pure metric and
failure-attribution functions. Disk-only concerns (service-log capture, the
engine-version stamp, the writer) stay with the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tolokaforge.core.failure_attribution import (
    attribute_failure,
    is_failed_trajectory,
    summarize_failure_attributions,
)
from tolokaforge.core.metrics import (
    calculate_aggregate_metrics,
    calculate_latency_percentiles,
    calculate_task_metrics,
)
from tolokaforge.core.models import Trajectory
from tolokaforge.core.output.aggregate_models import AGGREGATE_SCHEMA_VERSION


@dataclass(frozen=True)
class TaskReportMetadata:
    """The per-task metadata the report folds into ``per_task_metrics`` and the slices.

    The four fields the metadata-sliced aggregates group by. Each defaults to the
    value an unclassified task carries, so a task the caller has no metadata for
    buckets under ``unknown`` / ``unspecified`` exactly as a full run does.
    """

    benchmark_type: str | None = None
    complexity: str | None = None
    expected_failure_modes: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunReport:
    """The reproducible core of a run's aggregate report.

    The four artifacts a full ``tolokaforge run`` writes as
    ``per_task_metrics.json`` / ``aggregate.json`` / ``metadata_slices.json`` /
    ``failure_attribution.json`` — minus the disk-only, non-reproducible fields
    (``tolokaforge_version``, ``captured_service_logs``) a single-box run stamps
    onto ``aggregate`` after this report is built.
    """

    per_task_metrics: list[dict[str, Any]]
    aggregate: dict[str, Any]
    metadata_slices: dict[str, dict[str, Any]]
    failure_attribution: dict[str, Any]


def reasoning_transport_rollup(trajectories: list[Trajectory]) -> dict[str, int]:
    """How the run's reasoning actually travelled, counted over *trajectories*.

    Recovery means a preset read a narrower channel than its model uses —
    nothing was lost. An unknown channel means the provider billed for
    deliberation that arrived nowhere the engine looks.
    """
    recovered = [t.metrics.reasoning_recovered_by_fallback for t in trajectories]
    unknown = [t.metrics.reasoning_channel_unknown for t in trajectories]
    return {
        "recovered_by_fallback_calls": sum(recovered),
        "recovered_by_fallback_trials": sum(1 for n in recovered if n),
        "channel_unknown_calls": sum(unknown),
        "channel_unknown_trials": sum(1 for n in unknown if n),
    }


def build_run_report(
    trajectories: list[Trajectory],
    tasks_metadata: Mapping[str, TaskReportMetadata],
    *,
    reasoning_transport: dict[str, int] | None = None,
) -> RunReport:
    """Fold *trajectories* into the reproducible four-artifact run report.

    *trajectories* is the run's deduplicated trial list (one row per
    ``(task_id, trial_index)``); the caller dedups and renumbers before calling.
    *tasks_metadata* maps ``task_id`` to the metadata the slices group by; a
    ``task_id`` absent from it is folded with no metadata, exactly as a full run
    folds a trajectory whose task config it no longer holds.

    *reasoning_transport* lets the caller supply a rollup computed over its own
    trial set (the orchestrator counts over every attempt, not the deduped set);
    ``None`` computes it over *trajectories*.
    """
    task_trajectories: dict[str, list[Trajectory]] = {}
    for traj in trajectories:
        task_trajectories.setdefault(traj.task_id, []).append(traj)

    all_task_metrics: list[dict[str, Any]] = []
    for task_id, trajs in task_trajectories.items():
        task_metrics = calculate_task_metrics(trajs)
        task_metrics["task_id"] = task_id
        metadata = tasks_metadata.get(task_id)
        if metadata is not None:
            task_metrics["benchmark_type"] = metadata.benchmark_type
            task_metrics["complexity"] = metadata.complexity
            task_metrics["expected_failure_modes"] = list(metadata.expected_failure_modes)
            task_metrics["tags"] = list(metadata.tags)
        all_task_metrics.append(task_metrics)

    aggregate = calculate_aggregate_metrics(all_task_metrics, weighted=True)
    aggregate.update(
        calculate_latency_percentiles([t.metrics.latency_total_s for t in trajectories])
    )
    aggregate["reasoning_transport"] = (
        reasoning_transport_rollup(trajectories)
        if reasoning_transport is None
        else reasoning_transport
    )
    aggregate["schema_version"] = AGGREGATE_SCHEMA_VERSION

    metadata_slices = _metadata_slices(all_task_metrics)

    failure_attributions = [
        attribute_failure(traj) for traj in trajectories if is_failed_trajectory(traj)
    ]
    failure_attribution = {
        "summary": summarize_failure_attributions(failure_attributions),
        "failures": failure_attributions,
    }

    return RunReport(
        per_task_metrics=all_task_metrics,
        aggregate=aggregate,
        metadata_slices=metadata_slices,
        failure_attribution=failure_attribution,
    )


def _metadata_slices(all_task_metrics: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Weighted aggregates grouped by benchmark type, complexity, tag and failure mode."""
    metadata_slices: dict[str, dict[str, Any]] = {
        "by_benchmark_type": {},
        "by_complexity": {},
        "by_tag": {},
        "by_expected_failure_mode": {},
    }
    groups_by_benchmark: dict[str, list[dict[str, Any]]] = {}
    groups_by_complexity: dict[str, list[dict[str, Any]]] = {}
    groups_by_tag: dict[str, list[dict[str, Any]]] = {}
    groups_by_failure_mode: dict[str, list[dict[str, Any]]] = {}

    for task_metrics in all_task_metrics:
        benchmark_type = str(task_metrics.get("benchmark_type") or "unknown")
        complexity = str(task_metrics.get("complexity") or "unspecified")
        groups_by_benchmark.setdefault(benchmark_type, []).append(task_metrics)
        groups_by_complexity.setdefault(complexity, []).append(task_metrics)
        for tag in task_metrics.get("tags", []) or []:
            groups_by_tag.setdefault(str(tag), []).append(task_metrics)
        for failure_mode in task_metrics.get("expected_failure_modes", []) or []:
            groups_by_failure_mode.setdefault(str(failure_mode), []).append(task_metrics)

    for key, group in groups_by_benchmark.items():
        metadata_slices["by_benchmark_type"][key] = calculate_aggregate_metrics(
            group, weighted=True
        )
    for key, group in groups_by_complexity.items():
        metadata_slices["by_complexity"][key] = calculate_aggregate_metrics(group, weighted=True)
    for key, group in groups_by_tag.items():
        metadata_slices["by_tag"][key] = calculate_aggregate_metrics(group, weighted=True)
    for key, group in groups_by_failure_mode.items():
        metadata_slices["by_expected_failure_mode"][key] = calculate_aggregate_metrics(
            group, weighted=True
        )

    return metadata_slices


__all__ = [
    "RunReport",
    "TaskReportMetadata",
    "build_run_report",
    "reasoning_transport_rollup",
]
