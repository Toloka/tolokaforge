"""Pydantic models for the four run-level aggregate payloads.

Locks the on-wire shape of ``per_task_metrics.json`` / ``aggregate.json``
/ ``metadata_slices.json`` / ``failure_attribution.json``. See
:class:`~tolokaforge.core.output.aggregates.RunAggregateWriter` for the
writer surface these payloads flow through.

* :class:`PerTaskMetrics` — one row of ``per_task_metrics.json``.
* :class:`AggregateMetrics` — the shared body of ``aggregate.json`` (via
  :class:`RunAggregate`) and every slice inside ``metadata_slices.json``.
* :class:`RunAggregate` — ``AggregateMetrics`` + the ``schema_version``
  envelope and the ``captured_service_logs`` roll-up that ``aggregate.json``
  carries at the top level.
* :class:`CapturedServiceLogsRollup`, :class:`ServiceLogCaptureEntry`,
  :class:`ServiceLogCaptureSource` — the run-level roll-up of the per-trial
  and run-level captured compose-log bundles.
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

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from tolokaforge.core.failure_attribution import TrialOutcomeClass
from tolokaforge.core.models import TerminationReason

__all__ = [
    "AGGREGATE_SCHEMA_VERSION",
    "AggregateMetrics",
    "CapturedServiceLogsRollup",
    "FailureAttribution",
    "FailureRecord",
    "FailureSummary",
    "MetadataSlices",
    "OutcomeReasonCount",
    "PerTaskMetrics",
    "RunAggregate",
    "ServiceLogCaptureEntry",
    "ServiceLogCaptureSource",
]

AGGREGATE_SCHEMA_VERSION = 3
"""The ``aggregate.json`` wire generation.

Version 3 rates are over ``measured_trials`` — the trials that measured the
agent, including the ones whose grading refused. Such a trial reaches
``total_trials`` and ``measured_trials``, carries its own ``ungradeable`` count
and its own ``ungradeable_<reason>`` row, and counts as a non-pass, so a
consumer reading a file can tell which denominator produced
``success_rate_micro``, ``avg_score_micro`` and ``pass@k_macro`` without
inspecting the run that wrote it. The ``class`` vocabulary a consumer branches
on has four members.
"""


class OutcomeReasonCount(BaseModel):
    """One ``outcomes_by_reason`` row: how a group of trials was counted.

    The key the row hangs off is a termination reason, or ``unset_<status>`` for
    a trial that recorded no reason, or either of those under an ``ungradeable_``
    prefix — see :func:`~tolokaforge.core.metrics._outcome_key` for why the
    prefix is what keeps one key mapping to exactly one ``class``.

    ``class`` is a Python keyword, so the field is aliased; dump with
    ``by_alias=True`` to produce the wire shape.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    outcome_class: TrialOutcomeClass = Field(alias="class")
    count: int


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

    # Trial counts + basic success signal. ``total_trials`` counts every
    # attempt; ``measured_trials`` counts the ones that measured the agent and
    # is the denominator of every rate in this row except ``avg_score``, whose
    # denominator is ``scored_trials`` — the measured trials that produced a
    # grade at all. ``harness_errors`` and ``ungradeable`` overlap
    # ``measured_trials`` (our own defects are counted, and each count is a
    # run-health signal); ``infrastructure_aborts`` does not — it is the rest of
    # ``total_trials``, broken down per reason so provider throttling and a
    # harness regression can never read the same.
    total_trials: int
    measured_trials: int
    scored_trials: int
    infrastructure_aborts: dict[TerminationReason, int] = Field(default_factory=dict)
    harness_errors: int = 0
    ungradeable: int = 0
    outcomes_by_reason: dict[str, OutcomeReasonCount] = Field(default_factory=dict)
    successful_trials: int
    success_rate: float | None

    # pass@k / pass_hat@k for k in [1, 5, 10]. ``None`` when the task has
    # fewer *measured* trials than k — mirrors ``calculate_pass_k``.
    pass_at_1: float | None = Field(default=None, alias="pass@1")
    pass_at_5: float | None = Field(default=None, alias="pass@5")
    pass_at_10: float | None = Field(default=None, alias="pass@10")
    pass_hat_at_1: float | None = Field(default=None, alias="pass_hat@1")
    pass_hat_at_5: float | None = Field(default=None, alias="pass_hat@5")
    pass_hat_at_10: float | None = Field(default=None, alias="pass_hat@10")

    # Averages over the measured trials. Typed as ``int | float`` unions so the
    # model preserves the numeric type the producer emitted: e.g.
    # ``sum([]) == 0`` (``int``) for empty aggregates, ``float``
    # otherwise. Pydantic's smart-union picks the more specific type on
    # validation, so a source ``0`` round-trips to ``0`` (not ``0.0``)
    # in the JSON dump — pinning the wire format against the current
    # dict-based writer path. See :class:`RunAggregateWriter` docstring.
    # ``None`` when the task measured nothing: no trial ran, so there is no
    # average, and a ``0.0`` would read as a measured zero.
    avg_score: int | float | None
    avg_latency_s: int | float | None
    avg_turns: int | float | None
    avg_tool_calls: int | float | None

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

    stuck_rate: int | float | None = 0


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

    # The run-level halves of the per-task counts, so a rate is never read
    # without the denominator that produced it in the same object.
    measured_trials: int
    scored_trials: int
    infrastructure_aborts: dict[TerminationReason, int] = Field(default_factory=dict)
    harness_errors: int = 0
    ungradeable: int = 0
    outcomes_by_reason: dict[str, OutcomeReasonCount] = Field(default_factory=dict)

    # Weighted (micro) averages — set when ``weighted=True``. Rate-shaped
    # ([0, 1] scalar from ``sum(...) / n``): always ``float`` at the
    # producer. Stays narrow — no ``int`` widening — matching the
    # ``PerTaskMetrics.success_rate`` / ``avg_score`` siblings. ``None`` when
    # the run measured nothing at all. Each weighs by the denominator of the
    # per-task figure it averages: ``success_rate_micro`` by ``measured_trials``,
    # ``avg_score_micro`` by ``scored_trials``.
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

    # Simple cross-task averages, over the tasks that measured anything.
    avg_latency_s: int | float | None = 0
    avg_turns: int | float | None = 0
    avg_tool_calls: int | float | None = 0
    stuck_rate: int | float | None = 0

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


class ServiceLogCaptureSource(str, Enum):
    """Which failure stage produced a captured-service-log bundle.

    Closed 3-value classification of the run's on-disk capture surfaces:
    the per-trial provision-failure ``services/_capture.yaml``, the
    per-trial trial-body/graded-red ``metrics.yaml.captured_service_logs``,
    and the run-level shared-stack materialise-failure
    ``<output_dir>/services/_capture.yaml``.
    """

    PROVISION_FAILURE = "provision_failure"
    TRIAL_BODY = "trial_body"
    SHARED_STACK_MATERIALISE = "shared_stack_materialise"


class ServiceLogCaptureEntry(BaseModel):
    """One captured-service-log bundle rolled up into ``aggregate.json``.

    ``task_id`` / ``trial_index`` are ``None`` for the run-level
    shared-stack surface (there is no owning trial). ``capture_reason`` is
    ``None`` on the trial-body surface, whose durable record
    (``metrics.yaml.captured_service_logs``) carries no manifest reason.
    ``services`` maps compose-service name to captured byte count.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    trial_index: int | None = None
    source: ServiceLogCaptureSource
    capture_reason: str | None = None
    total_bytes: int
    services: dict[str, int] = Field(default_factory=dict)


class CapturedServiceLogsRollup(BaseModel):
    """Run-level roll-up of every captured-service-log bundle on disk.

    ``per_service_bytes`` sums each service's bytes across every entry;
    ``total_bytes`` is the grand total. A run that captured nothing rolls
    up to a zero envelope (``captures=0``, empty maps/lists), which is
    distinguishable from a pre-feature ``aggregate.json`` that omits the
    field entirely.
    """

    model_config = ConfigDict(extra="forbid")

    captures: int
    total_bytes: int
    per_service_bytes: dict[str, int] = Field(default_factory=dict)
    entries: list[ServiceLogCaptureEntry] = Field(default_factory=list)


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

    schema_version: int = AGGREGATE_SCHEMA_VERSION

    captured_service_logs: CapturedServiceLogsRollup | None = None

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
    ``termination_reason`` / ``synthesized_grade`` / …) and locking the
    union here would over-constrain the classifier. A future ticket can
    promote each ``kind`` to its own model.

    ``synthesized`` + ``synthesized_by_termination_reason`` name the
    harness auto-fail branches of :class:`~tolokaforge.core.trial_grader.TrialGrader`:
    a trial whose grade was fabricated by the harness (no evaluator ran
    on it — ``ERROR`` / ``TIMEOUT`` / ``STUCK_DETECTED`` /
    ``EMPTY_COMPLETION``) carries ``synthesized: true`` and the reason
    name, and the record's ``failure_class`` is ``harness_autofail``
    when the synth reason is not otherwise enumerated in
    :func:`~tolokaforge.core.failure_attribution.attribute_failure`.
    Present-but-null off the synth path so a downstream consumer can
    read ``record["synthesized"]`` unconditionally.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    trial_index: int
    status: str
    termination_reason: str | None = None
    # Which point of the provisioning lifecycle raised ``ProvisionError``. Non-
    # ``None`` iff ``termination_reason == "provision_error"``; otherwise present
    # and null so a downstream consumer can read ``record["provision_stage"]``
    # unconditionally rather than gate the access on ``termination_reason``.
    # Typed ``str`` (not the ``ProvisionStage`` Literal) for the same reason
    # ``termination_reason`` is: a bundle written by a future run whose classifier
    # has heard of a new stage round-trips rather than raising here.
    provision_stage: str | None = None
    outcome_class: TrialOutcomeClass
    failure_class: str
    deterministic: bool
    confidence: float
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # Whether the grade was harness-synthesised on an auto-fail branch (no
    # evaluator ran). Additive-with-default so a bundle written before this
    # field existed round-trips as ``synthesized: false``,
    # ``synthesized_by_termination_reason: None``.
    synthesized: bool = False
    synthesized_by_termination_reason: str | None = None


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
    # ``str`` keys, not :class:`TrialOutcomeClass`: this counter's ``"unknown"``
    # bucket holds the attributions that carry no ``outcome_class`` at all, which
    # is not a class the classifier can ever return.
    by_outcome_class: dict[str, int] = Field(default_factory=dict)
    by_tool: dict[str, int] = Field(default_factory=dict)


class FailureAttribution(BaseModel):
    """The ``failure_attribution.json`` envelope — ``{"summary": ...,
    "failures": [...]}``.
    """

    model_config = ConfigDict(extra="forbid")

    summary: FailureSummary
    failures: list[FailureRecord] = Field(default_factory=list)
