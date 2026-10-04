"""Per-harness comparison aggregation and a labelled text table.

Pure functions over the list of per-task metric rows that ``_generate_reports``
already builds (each row carries ``harness_entry``, ``execution_mode`` and
``benchmark_type`` beside the per-task metric fields). Nothing here does I/O,
logging, or renders into the run output; a caller groups the rows into slices
and, when there is something to compare, logs the formatted table.

Each slice value is produced by re-running
:func:`~tolokaforge.core.metrics.calculate_aggregate_metrics` over the grouped
rows, the same way the ``by_benchmark_type`` slice is built — no metric math
lives here.

Buckets:

* **Harness bucket** — ``harness_entry`` normalised so a missing entry (an old
  single-adapter bundle records ``None``) and an empty entry both land in one
  ``"native"`` bucket, and any named entry keeps its name. A run that mixes old
  (``None``) and new (named) rows groups without error.
* **Execution mode** — the row's ``execution_mode``; a missing mode lands in an
  ``"unknown"`` bucket, matching the ``"unknown"`` default the other slices use.
* **Task family** — the row's ``benchmark_type``, which already defaults to
  ``"unknown"`` when the task has no category.

The composite harness×family slice is keyed by a flat
``"<harness>::<family>"`` string, so it drops into a
``dict[str, AggregateMetrics]`` field exactly like the other ``by_*`` slices.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from tolokaforge.core.metrics import calculate_aggregate_metrics

__all__ = [
    "NATIVE_BUCKET",
    "UNKNOWN_BUCKET",
    "COMPOSITE_KEY_SEPARATOR",
    "HARNESS_COMPARISON_FOOTNOTE",
    "harness_bucket",
    "build_harness_comparison_slices",
    "has_comparable_harnesses",
    "format_harness_comparison_table",
]

#: Bucket for a row with no harness entry (``None`` or empty string): the
#: native engine loop / single-adapter path.
NATIVE_BUCKET = "native"

#: Bucket for a row whose execution mode is unrecorded.
UNKNOWN_BUCKET = "unknown"

#: Separator joining harness bucket and task family in the composite slice key.
COMPOSITE_KEY_SEPARATOR = "::"

#: Printed verbatim beneath every rendered table.
HARNESS_COMPARISON_FOOTNOTE = (
    "Cost and turns are defined per harness and are NOT directly comparable "
    "across harnesses (a delegated harness's cost is the engine's price for the "
    "CLI-reported tokens; turns is the CLI's own count). Success rate, score, "
    "pass@k and wall-time are the cross-harness comparables."
)


def harness_bucket(harness_entry: str | None) -> str:
    """Normalise a row's ``harness_entry`` to its comparison bucket.

    ``None`` (old bundle) and ``""`` (single adapter) collapse into
    :data:`NATIVE_BUCKET`; any other value is the bucket itself.
    """
    return harness_entry if harness_entry else NATIVE_BUCKET


def _execution_mode_bucket(execution_mode: str | None) -> str:
    """Normalise a row's ``execution_mode`` to its bucket (``None`` → unknown)."""
    return execution_mode if execution_mode else UNKNOWN_BUCKET


def _task_family(row: dict[str, Any]) -> str:
    """The row's task family — ``benchmark_type``, defaulting to ``"unknown"``."""
    return str(row.get("benchmark_type") or UNKNOWN_BUCKET)


def _aggregate_groups(groups: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Re-run ``calculate_aggregate_metrics`` over each group of rows."""
    return {key: calculate_aggregate_metrics(rows, weighted=True) for key, rows in groups.items()}


def build_harness_comparison_slices(
    all_task_metrics: Sequence[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Group the per-task rows three ways and aggregate each group.

    Returns a dict with:

    * ``by_harness_entry`` — keyed by harness bucket.
    * ``by_execution_mode`` — keyed by execution-mode bucket.
    * ``by_harness_and_task_family`` — keyed by ``"<harness>::<family>"``.

    Each value is the dict returned by
    :func:`~tolokaforge.core.metrics.calculate_aggregate_metrics` for that
    group, matching the shape the other ``by_*`` slices carry.
    """
    by_harness: dict[str, list[dict[str, Any]]] = {}
    by_execution_mode: dict[str, list[dict[str, Any]]] = {}
    by_harness_and_family: dict[str, list[dict[str, Any]]] = {}

    for row in all_task_metrics:
        bucket = harness_bucket(row.get("harness_entry"))
        mode = _execution_mode_bucket(row.get("execution_mode"))
        family = _task_family(row)
        composite = f"{bucket}{COMPOSITE_KEY_SEPARATOR}{family}"

        by_harness.setdefault(bucket, []).append(dict(row))
        by_execution_mode.setdefault(mode, []).append(dict(row))
        by_harness_and_family.setdefault(composite, []).append(dict(row))

    return {
        "by_harness_entry": _aggregate_groups(by_harness),
        "by_execution_mode": _aggregate_groups(by_execution_mode),
        "by_harness_and_task_family": _aggregate_groups(by_harness_and_family),
    }


def has_comparable_harnesses(by_harness_entry: dict[str, dict[str, Any]]) -> bool:
    """Whether the ``by_harness_entry`` slice holds more than one harness to compare.

    True only when at least two distinct non-native buckets are present. A run
    with one adapter — everything in a single bucket, native or otherwise — has
    nothing to compare, so the formatter degrades to ``None`` and leaves the
    single-adapter output unchanged.
    """
    return len([key for key in by_harness_entry if key != NATIVE_BUCKET]) > 1


def _fmt_rate(value: Any) -> str:
    return "-" if value is None else f"{value:.3f}"


def _fmt_seconds(value: Any) -> str:
    return "-" if value is None else f"{value:.2f}"


def _fmt_cost(value: Any) -> str:
    return "-" if value is None else f"${value:.4f}"


def _fmt_turns(value: Any) -> str:
    return "-" if value is None else f"{value:.2f}"


# (header, metric key, cell formatter). Success rate, score, pass@k and
# wall-time are the cross-harness comparables and carry no basis tag; cost and
# turns are defined per harness and say so in their header.
_METRIC_COLUMNS: tuple[tuple[str, str, Callable[[Any], str]], ...] = (
    ("success rate", "success_rate_micro", _fmt_rate),
    ("score", "avg_score_micro", _fmt_rate),
    ("pass@1", "pass@1_macro", _fmt_rate),
    ("pass@5", "pass@5_macro", _fmt_rate),
    ("pass@10", "pass@10_macro", _fmt_rate),
    ("wall-time (s)", "avg_latency_s", _fmt_seconds),
    ("cost (per-harness basis)", "total_cost_usd", _fmt_cost),
    ("turns (per-harness basis)", "avg_turns", _fmt_turns),
)


def _render_table(
    label_header: str,
    keys: Sequence[str],
    slice_dict: dict[str, dict[str, Any]],
) -> str:
    """Render one monospace table — one row per key, the metric columns beside it."""
    headers = [label_header, *(col[0] for col in _METRIC_COLUMNS)]
    body: list[list[str]] = []
    for key in keys:
        metrics = slice_dict[key]
        body.append([key, *(fmt(metrics.get(field)) for _, field, fmt in _METRIC_COLUMNS)])

    widths = [len(header) for header in headers]
    for cells in body:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))

    def render_row(cells: Sequence[str]) -> str:
        rendered = [cells[0].ljust(widths[0])]
        rendered += [cells[index].rjust(widths[index]) for index in range(1, len(cells))]
        return "  ".join(rendered)

    lines = [render_row(headers), "-" * len(render_row(headers))]
    lines += [render_row(cells) for cells in body]
    return "\n".join(lines)


def format_harness_comparison_table(
    slices: dict[str, dict[str, dict[str, Any]]],
) -> str | None:
    """Render the per-harness comparison tables, or ``None`` when nothing to compare.

    Takes the dict :func:`build_harness_comparison_slices` returns. Emits a
    per-harness-entry table and, when any family groups exist, a per-harness ×
    task-family table, both followed by :data:`HARNESS_COMPARISON_FOOTNOTE`.
    Returns ``None`` when :func:`has_comparable_harnesses` is false, so a
    single-adapter run renders no table at all.
    """
    by_harness_entry = slices["by_harness_entry"]
    if not has_comparable_harnesses(by_harness_entry):
        return None

    sections = [
        "Per-harness comparison",
        _render_table("harness", sorted(by_harness_entry), by_harness_entry),
    ]

    by_family = slices.get("by_harness_and_task_family") or {}
    if by_family:
        sections.append("")
        sections.append("Per-harness :: task-family comparison")
        sections.append(
            _render_table(
                f"harness {COMPOSITE_KEY_SEPARATOR} task family",
                sorted(by_family),
                by_family,
            )
        )

    sections.append("")
    sections.append(HARNESS_COMPARISON_FOOTNOTE)
    return "\n".join(sections)
