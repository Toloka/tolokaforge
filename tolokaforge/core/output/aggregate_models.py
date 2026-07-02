"""Pydantic models for the four run-level aggregate payloads.

Locks the on-wire shape of ``per_task_metrics.json`` / ``aggregate.json``
/ ``metadata_slices.json`` / ``failure_attribution.json``. See
:class:`~tolokaforge.core.output.aggregates.RunAggregateWriter` for the
writer surface these payloads flow through.

* :class:`PerTaskMetrics` — one row of ``per_task_metrics.json``.
* :class:`AggregateMetrics` — the shared body of ``aggregate.json`` (via
  :class:`RunAggregate`) and every slice inside ``metadata_slices.json``.
* :class:`RunAggregate` — ``AggregateMetrics`` + the ``schema_version``
  envelope field that ``aggregate.json`` carries at the top level.
* :class:`MetadataSlices` — the four ``by_*`` slice dictionaries.
* :class:`FailureAttribution`, :class:`FailureSummary`,
  :class:`FailureRecord` — the ``failure_attribution.json`` envelope.

Stage 1 of TECHDEL-407: these models exist alongside the current
dict-typed writer surface. Stage 2 migrates the metric-calc functions
and the writer Protocol to return / accept them; stage 3 migrates
in-process consumers to attribute access. Round-trip byte-identity is
pinned by ``tests/canonical/test_run_aggregate_models_snapshot.py``.

The ``pass@k`` / ``pass_hat@k`` keys aren't valid Python identifiers,
so they carry :class:`~pydantic.Field` ``alias`` values. Dump with
``model_dump(by_alias=True, mode="json")`` to produce the wire shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AggregateMetrics",
    "FailureAttribution",
    "FailureRecord",
    "FailureSummary",
    "MetadataSlices",
    "PerTaskMetrics",
    "RunAggregate",
]


class PerTaskMetrics(BaseModel):
    """One row of ``per_task_metrics.json`` — a task's aggregate across trials.

    Produced by :func:`~tolokaforge.core.metrics.calculate_task_metrics`
    and augmented by the orchestrator with the task-identity + metadata
    fields (``task_id``, ``benchmark_type``, ``complexity``, ``tags``,
    ``expected_failure_modes``). The union is what lands in the JSON row.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Task identity + metadata (added by the orchestrator, not by the
    # metric-calc function).
    task_id: str
    benchmark_type: str | None = None
    complexity: str | None = None
    tags: list[str] = Field(default_factory=list)
    expected_failure_modes: list[str] = Field(default_factory=list)

    # Trial counts + basic success signal.
    total_trials: int
    successful_trials: int
    success_rate: float

    # pass@k / pass_hat@k for k in [1, 5, 10]. ``None`` when the run has
    # fewer trials than k — mirrors ``calculate_pass_k``.
    pass_at_1: float | None = Field(default=None, alias="pass@1")
    pass_at_5: float | None = Field(default=None, alias="pass@5")
    pass_at_10: float | None = Field(default=None, alias="pass@10")
    pass_hat_at_1: float | None = Field(default=None, alias="pass_hat@1")
    pass_hat_at_5: float | None = Field(default=None, alias="pass_hat@5")
    pass_hat_at_10: float | None = Field(default=None, alias="pass_hat@10")

    # Averages across trials.
    avg_score: float
    avg_latency_s: float
    avg_turns: float
    avg_tool_calls: float

    # Per-usage-field averages (Metrics.usage — six fields).
    avg_prompt_tokens: float = 0.0
    avg_completion_tokens: float = 0.0
    avg_reasoning_tokens: float = 0.0
    avg_cached_tokens: float = 0.0
    avg_cache_creation_input_tokens: float = 0.0
    avg_cache_read_input_tokens: float = 0.0

    # Cost is ``None`` when no trial reported a cost (unknown-provider case).
    total_cost_usd: float | None = None
    avg_cost_usd: float | None = None

    # Per-trial wall-time percentiles.
    latency_p50_s: float = 0.0
    latency_p90_s: float = 0.0
    latency_p99_s: float = 0.0

    # Per-API-call percentiles aggregated across every call in every trial.
    api_call_latency_p50_s: float = 0.0
    api_call_latency_p90_s: float = 0.0
    api_call_latency_p99_s: float = 0.0

    stuck_rate: float = 0.0


class AggregateMetrics(BaseModel):
    """The shared body used both by ``aggregate.json`` (via
    :class:`RunAggregate`) and by every slice value inside
    ``metadata_slices.json``.

    Produced by :func:`~tolokaforge.core.metrics.calculate_aggregate_metrics`.
    Micro vs macro averages are populated depending on whether the
    aggregate was computed weighted or unweighted; the un-set half stays
    ``None``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    total_tasks: int
    total_trials: int

    # Weighted (micro) averages — set when ``weighted=True``.
    success_rate_micro: float | None = None
    avg_score_micro: float | None = None

    # Unweighted (macro) averages — set when ``weighted=False``.
    success_rate_macro: float | None = None
    avg_score_macro: float | None = None

    # pass@k macro averages across tasks, plus their pass_hat aliases.
    pass_at_1_macro: float | None = Field(default=None, alias="pass@1_macro")
    pass_at_5_macro: float | None = Field(default=None, alias="pass@5_macro")
    pass_at_10_macro: float | None = Field(default=None, alias="pass@10_macro")
    pass_hat_at_1_macro: float | None = Field(default=None, alias="pass_hat@1_macro")
    pass_hat_at_5_macro: float | None = Field(default=None, alias="pass_hat@5_macro")
    pass_hat_at_10_macro: float | None = Field(default=None, alias="pass_hat@10_macro")

    # Simple cross-task averages.
    avg_latency_s: float = 0.0
    avg_turns: float = 0.0
    avg_tool_calls: float = 0.0
    stuck_rate: float = 0.0

    # Per-usage-field totals + macro averages.
    total_prompt_tokens: float = 0.0
    total_completion_tokens: float = 0.0
    total_reasoning_tokens: float = 0.0
    total_cached_tokens: float = 0.0
    total_cache_creation_input_tokens: float = 0.0
    total_cache_read_input_tokens: float = 0.0
    avg_prompt_tokens: float = 0.0
    avg_completion_tokens: float = 0.0
    avg_reasoning_tokens: float = 0.0
    avg_cached_tokens: float = 0.0
    avg_cache_creation_input_tokens: float = 0.0
    avg_cache_read_input_tokens: float = 0.0

    total_cost_usd: float | None = None
    avg_cost_usd: float | None = None

    latency_p50_s_macro: float = 0.0
    latency_p90_s_macro: float = 0.0
    latency_p99_s_macro: float = 0.0


class RunAggregate(AggregateMetrics):
    """The top-level ``aggregate.json`` shape — :class:`AggregateMetrics`
    plus the ``schema_version`` envelope field the orchestrator stamps
    before writing.

    Slice values inside ``metadata_slices.json`` are :class:`AggregateMetrics`
    directly; only the top-level artifact carries the envelope.
    """

    schema_version: int = 1


class MetadataSlices(BaseModel):
    """The ``metadata_slices.json`` envelope — four ``by_*`` dictionaries.

    Each entry maps a slice key (benchmark type, complexity name, tag,
    or expected failure mode) to the :class:`AggregateMetrics` computed
    over the tasks in that slice.
    """

    model_config = ConfigDict(extra="forbid")

    by_benchmark_type: dict[str, AggregateMetrics] = Field(default_factory=dict)
    by_complexity: dict[str, AggregateMetrics] = Field(default_factory=dict)
    by_tag: dict[str, AggregateMetrics] = Field(default_factory=dict)
    by_expected_failure_mode: dict[str, AggregateMetrics] = Field(default_factory=dict)


class FailureRecord(BaseModel):
    """One entry in ``failure_attribution.json``'s ``failures`` list —
    the output of
    :func:`~tolokaforge.core.failure_attribution.attribute_failure` for a
    single failed trajectory.

    ``evidence`` stays as a list of dicts; the entries are heterogeneous
    (`kind` acts as a tag — ``tool_log`` / ``state_diff`` /
    ``termination_reason`` / …) and locking the union here would
    over-constrain the classifier. A future ticket can promote each
    ``kind`` to its own model.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    trial_index: int
    status: str
    termination_reason: str | None = None
    failure_class: str
    deterministic: bool
    confidence: float
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class FailureSummary(BaseModel):
    """The ``summary`` sub-object of ``failure_attribution.json``.

    Produced by
    :func:`~tolokaforge.core.failure_attribution.summarize_failure_attributions`.
    ``deterministic_attribution_coverage`` is ``None`` when the summary
    describes zero failed attempts (division by zero avoided at source).
    """

    model_config = ConfigDict(extra="forbid")

    total_failed_attempts: int
    deterministic_attribution_coverage: float | None = None
    by_failure_class: dict[str, int] = Field(default_factory=dict)
    by_tool: dict[str, int] = Field(default_factory=dict)


class FailureAttribution(BaseModel):
    """The ``failure_attribution.json`` envelope — ``{"summary": ...,
    "failures": [...]}``.
    """

    model_config = ConfigDict(extra="forbid")

    summary: FailureSummary
    failures: list[FailureRecord] = Field(default_factory=list)
