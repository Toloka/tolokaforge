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

The models exist alongside the current dict-typed writer surface —
they do not yet flow through the metric-calc functions or the writer
Protocol. Round-trip byte-identity against the current on-disk shape
is pinned by ``tests/canonical/test_run_aggregate_models_snapshot.py``,
so a later change that swaps the writer to accept models can be
verified against the same wire format.

The ``pass@k`` / ``pass_hat@k`` keys aren't valid Python identifiers,
so they carry :class:`~pydantic.Field` ``alias`` values. Dump with
``model_dump(by_alias=True, mode="json")`` to produce the wire shape.

Two wire-format invariants pinned by the canonical tests:

1. ``schema_version`` on :class:`RunAggregate` is **always emitted** on
   dump, regardless of ``exclude_unset``. See the model-serializer on
   the class for details.
2. **Type-preserving numeric fields.** Fields that can naturally be
   ``int`` at the producer (token counts, latency percentiles,
   stuck-rate counts, cost sums) carry ``int | float`` unions so the
   wire type matches whatever the producer emitted. Pydantic v2's
   smart-union picks the more specific type on validation, so a
   source ``int 42`` round-trips to ``42`` (not ``42.0``) in the
   JSON dump. Rate-shaped fields (``success_rate``, ``pass@k``,
   ``avg_score`` and their macro/micro aggregates) stay narrow
   ``float`` because they're always ``sum(...) / n`` divisions —
   never ``int`` at the producer.

   The current producers in ``metrics.py`` and
   ``failure_attribution.py`` happen to emit ``float`` for every
   widened field today (both metric-calc functions short-circuit on
   empty input rather than dividing by zero). The union is
   defensive future-proofing: if a future refactor changes a
   producer to emit ``int`` (e.g. a counting aggregator that returns
   ``0`` for empty), the model preserves the wire type without a
   coordinated model change.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer

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

    # Averages across trials. Typed as ``int | float`` unions so the
    # model preserves the numeric type the producer emitted: e.g.
    # ``sum([]) == 0`` (``int``) for empty aggregates, ``float``
    # otherwise. Pydantic's smart-union picks the more specific type on
    # validation, so a source ``0`` round-trips to ``0`` (not ``0.0``)
    # in the JSON dump — pinning the wire format against the current
    # dict-based writer path. See :class:`RunAggregateWriter` docstring.
    avg_score: int | float
    avg_latency_s: int | float
    avg_turns: int | float
    avg_tool_calls: int | float

    # Per-usage-field averages (Metrics.usage — six fields).
    avg_prompt_tokens: int | float = 0
    avg_completion_tokens: int | float = 0
    avg_reasoning_tokens: int | float = 0
    avg_cached_tokens: int | float = 0
    avg_cache_creation_input_tokens: int | float = 0
    avg_cache_read_input_tokens: int | float = 0

    # Cost is ``None`` when no trial reported a cost (unknown-provider case).
    total_cost_usd: int | float | None = None
    avg_cost_usd: int | float | None = None

    # Judge-cost split — ``judge_cost_usd`` is the LLM-judge grader's
    # spend, tracked separately so the agent-vs-judge cost breakdown
    # survives round-trips. ``total_cost_incl_judge_usd`` = agent +
    # judge; ``None`` when neither is known.
    judge_cost_usd: int | float | None = None
    total_cost_incl_judge_usd: int | float | None = None

    # Per-trial wall-time percentiles.
    latency_p50_s: int | float = 0
    latency_p90_s: int | float = 0
    latency_p99_s: int | float = 0

    # Per-API-call percentiles aggregated across every call in every trial.
    api_call_latency_p50_s: int | float = 0
    api_call_latency_p90_s: int | float = 0
    api_call_latency_p99_s: int | float = 0

    stuck_rate: int | float = 0


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

    # Weighted (micro) averages — set when ``weighted=True``. Rate-shaped
    # ([0, 1] scalar from ``sum(...) / n``): always ``float`` at the
    # producer. Stays narrow — no ``int`` widening — matching the
    # ``PerTaskMetrics.success_rate`` / ``avg_score`` siblings.
    success_rate_micro: float | None = None
    avg_score_micro: float | None = None

    # Unweighted (macro) averages — set when ``weighted=False``. Same
    # rate-shaped rationale.
    success_rate_macro: float | None = None
    avg_score_macro: float | None = None

    # pass@k macro averages across tasks, plus their pass_hat aliases.
    # Also always ``float`` (macro-average of ``pass@k`` rates).
    pass_at_1_macro: float | None = Field(default=None, alias="pass@1_macro")
    pass_at_5_macro: float | None = Field(default=None, alias="pass@5_macro")
    pass_at_10_macro: float | None = Field(default=None, alias="pass@10_macro")
    pass_hat_at_1_macro: float | None = Field(default=None, alias="pass_hat@1_macro")
    pass_hat_at_5_macro: float | None = Field(default=None, alias="pass_hat@5_macro")
    pass_hat_at_10_macro: float | None = Field(default=None, alias="pass_hat@10_macro")

    # Simple cross-task averages.
    avg_latency_s: int | float = 0
    avg_turns: int | float = 0
    avg_tool_calls: int | float = 0
    stuck_rate: int | float = 0

    # Per-usage-field totals + macro averages.
    total_prompt_tokens: int | float = 0
    total_completion_tokens: int | float = 0
    total_reasoning_tokens: int | float = 0
    total_cached_tokens: int | float = 0
    total_cache_creation_input_tokens: int | float = 0
    total_cache_read_input_tokens: int | float = 0
    avg_prompt_tokens: int | float = 0
    avg_completion_tokens: int | float = 0
    avg_reasoning_tokens: int | float = 0
    avg_cached_tokens: int | float = 0
    avg_cache_creation_input_tokens: int | float = 0
    avg_cache_read_input_tokens: int | float = 0

    total_cost_usd: int | float | None = None
    avg_cost_usd: int | float | None = None

    # Judge-cost split at the run/slice level — same shape as
    # ``PerTaskMetrics`` but aggregated across the tasks/slice.
    judge_cost_usd: int | float | None = None
    total_cost_incl_judge_usd: int | float | None = None

    latency_p50_s_macro: int | float = 0
    latency_p90_s_macro: int | float = 0
    latency_p99_s_macro: int | float = 0


class RunAggregate(AggregateMetrics):
    """The top-level ``aggregate.json`` shape — :class:`AggregateMetrics`
    plus the ``schema_version`` envelope field every downstream consumer
    (dashboards, metric collectors, historical-run readers) reads to
    dispatch between wire-format generations.

    Slice values inside ``metadata_slices.json`` are :class:`AggregateMetrics`
    directly; only the top-level artifact carries the envelope.

    ``schema_version`` is **always emitted** on dump, regardless of
    whether the caller constructs the model with an explicit value or
    lets the default apply. Pydantic's ``exclude_unset=True`` normally
    drops fields left at their default — for this envelope that would
    silently ship an aggregate.json without a version, which would
    break every version-dispatching downstream consumer. The
    :func:`_always_include_schema_version` model-serializer below
    guarantees the field is present regardless of dump options.
    """

    schema_version: int = 1

    @model_serializer(mode="wrap")
    def _always_include_schema_version(self, handler: Any) -> dict[str, Any]:
        """Wrap the default serializer so ``schema_version`` is always
        present in the dumped dict, even under ``exclude_unset=True``.

        The wrapped handler produces whatever dict the caller's dump
        options normally would; this method injects ``schema_version``
        only when the handler's output omitted it (which happens under
        ``exclude_unset=True`` when the field was left at its default).
        No forced overwrite of a caller-set value.
        """
        data = handler(self)
        if isinstance(data, dict) and "schema_version" not in data:
            data["schema_version"] = self.schema_version
        return data


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
