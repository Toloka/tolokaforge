"""Output artifact writers (Stage 7 / P6).

This package is the boundary between the orchestrator and disk. It defines:

* :class:`~tolokaforge.core.output.artifacts.TrialArtifactWriter` —
  a :class:`typing.Protocol` that the orchestrator depends on (not the
  concrete class).
* :class:`~tolokaforge.core.output.artifacts.FileArtifactWriter` —
  the disk-backed implementation. Composes the existing
  :class:`tolokaforge.core.output_writer.OutputWriter` for per-trial files
  and adds the Stage 7 ``results/tools_schemas/`` sidecar writer.
* :func:`~tolokaforge.core.output.artifacts.model_id_slug` — deterministic
  filesystem-safe ``(provider, name)`` slug used as the ``<model_id>`` part
  of ``results/tools_schemas/<task_id>__<model_id>.json``.
* :class:`~tolokaforge.core.output.aggregates.RunAggregateWriter` —
  Protocol covering the four post-run aggregate JSONs at the run-output
  root (``per_task_metrics.json``, ``aggregate.json``,
  ``metadata_slices.json``, ``failure_attribution.json``); see ADR-0005.
* :class:`~tolokaforge.core.output.aggregates.FileAggregateWriter` and
  :class:`~tolokaforge.core.output.aggregates.InMemoryAggregateWriter` —
  the disk-backed and in-memory implementations; the latter records
  payloads on a :class:`RunAggregateBundle`.

See [`docs/OUTPUT_FORMAT.md`](../../../docs/OUTPUT_FORMAT.md) for the full
on-disk contract.
"""

from __future__ import annotations

from tolokaforge.core.output.aggregates import (
    FileAggregateWriter,
    InMemoryAggregateWriter,
    RunAggregateBundle,
    RunAggregateWriter,
)
from tolokaforge.core.output.artifacts import (
    FileArtifactWriter,
    TrialArtifactWriter,
    model_id_slug,
)

__all__ = [
    "FileAggregateWriter",
    "FileArtifactWriter",
    "InMemoryAggregateWriter",
    "RunAggregateBundle",
    "RunAggregateWriter",
    "TrialArtifactWriter",
    "model_id_slug",
]
