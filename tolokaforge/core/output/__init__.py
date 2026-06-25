"""Output artifact writers — the boundary between the orchestrator and disk.

The package types both halves of the data plane behind runtime-checkable
Protocols, each with a disk-backed and an in-memory implementation.

Per-trial seam (see ADR-0004):

* :class:`~tolokaforge.core.output.artifacts.TrialArtifactWriter` — the
  Protocol the orchestrator depends on for per-trial artifacts.
* :class:`~tolokaforge.core.output.artifacts.FileArtifactWriter` — the
  disk-backed implementation; composes
  :class:`tolokaforge.core.output_writer.OutputWriter` for the eight
  per-trial YAML files (``task.yaml``, ``trajectory.yaml``,
  ``env.yaml``, ``metrics.yaml``, ``grade.yaml``, ``logs.yaml``,
  ``tools_schemas.yaml``, ``prompts.yaml``) inside each trial bundle.

Run-level seam (see ADR-0005):

* :class:`~tolokaforge.core.output.aggregates.RunAggregateWriter` —
  Protocol covering the four post-run aggregate JSONs at the run-output
  root (``per_task_metrics.json``, ``aggregate.json``,
  ``metadata_slices.json``, ``failure_attribution.json``).
* :class:`~tolokaforge.core.output.aggregates.FileAggregateWriter` and
  :class:`~tolokaforge.core.output.aggregates.InMemoryAggregateWriter`
  — the disk-backed and in-memory implementations; the latter records
  payloads on a :class:`RunAggregateBundle`.

Helper:

* :func:`~tolokaforge.core.output.artifacts.model_id_slug` —
  deterministic filesystem-safe ``(provider, name)`` slug used wherever
  an artifact path needs a model identifier (cache files, debug dumps).

See [`docs/OUTPUT_FORMAT.md`](../../../docs/OUTPUT_FORMAT.md) for the
full on-disk contract.
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
