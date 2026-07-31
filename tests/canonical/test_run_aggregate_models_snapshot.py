"""Byte-identity round-trip for the run-level aggregate Pydantic models.

The models in :mod:`tolokaforge.core.output.aggregate_models` exist
alongside the current dict-typed writer surface. This suite proves the
models faithfully round-trip every field the production metric-calc
functions produce today, so a later change that swaps the writer to
accept the models — or migrates in-process consumers to attribute
access — can be verified against the same on-disk shape rather than
being trusted on inspection.

Each test:

* generates a real dict payload by invoking the production metric-calc
  function on representative inputs;
* passes it through the corresponding model
  (``Model.model_validate(dict).model_dump(by_alias=True, mode="json")``);
* asserts the dumped dict equals the original dict — same keys, same
  values, same types.

Anything the current code writes that the model can't round-trip fails
this suite loudly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.failure_attribution import (
    attribute_failure,
    summarize_failure_attributions,
)
from tolokaforge.core.metrics import (
    calculate_aggregate_metrics,
    calculate_task_metrics,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Metrics,
    RecordedToolCall,
    TerminationReason,
    ToolExecutionStatus,
    Trajectory,
    TrialStatus,
    Usage,
)
from tolokaforge.core.output.aggregate_models import (
    AGGREGATE_SCHEMA_VERSION,
    AggregateMetrics,
    CapturedServiceLogsRollup,
    FailureAttribution,
    FailureRecord,
    FailureSummary,
    MetadataSlices,
    PerTaskMetrics,
    RunAggregate,
    ServiceLogCaptureEntry,
    ServiceLogCaptureSource,
)

pytestmark = pytest.mark.canonical


def _make_trajectory(
    task_id: str = "task-1",
    trial_index: int = 0,
    *,
    binary_pass: bool = True,
    score: float = 1.0,
    latency_s: float = 12.34,
    turns: int = 8,
    tool_calls: int = 5,
    stuck_detected: bool = False,
    cost_usd: float | None = 0.021,
    status: TrialStatus = TrialStatus.COMPLETED,
    termination_reason: TerminationReason | None = None,
    tool_log: list[RecordedToolCall] | None = None,
) -> Trajectory:
    """Build a Trajectory populated on every field the aggregate path reads.

    Every ``Usage`` field is nonzero so the round-trip catches drops in
    the ``avg_*`` / ``total_*`` slots. ``binary_pass=False`` (with a
    non-``COMPLETED`` status) drives the failure-attribution path.
    ``tool_log`` drives the tool-execution branch of
    :func:`attribute_failure`, which produces ``evidence`` entries
    carrying a ``tool`` key — the source of the ``by_tool`` counter in
    :func:`summarize_failure_attributions`.
    """
    now = datetime.now(tz=UTC)
    return Trajectory(
        task_id=task_id,
        trial_index=trial_index,
        start_ts=now,
        end_ts=now,
        status=status,
        termination_reason=termination_reason,
        messages=[],
        tool_log=tool_log or [],
        metrics=Metrics(
            latency_total_s=latency_s,
            turns=turns,
            tool_calls=tool_calls,
            stuck_detected=stuck_detected,
            cost_usd=cost_usd,
            usage=Usage(
                prompt_tokens=1200,
                completion_tokens=340,
                reasoning_tokens=80,
                cached_tokens=200,
                cache_creation_input_tokens=150,
                cache_read_input_tokens=90,
            ),
        ),
        grade=Grade(
            binary_pass=binary_pass,
            score=score,
            components=GradeComponents(state_checks=score),
        ),
    )


def _make_tool_execution_failure() -> Trajectory:
    """A tool-execution failure trajectory — ``attribute_failure`` produces
    an ``evidence`` entry with a ``tool`` key, so the summary's
    ``by_tool`` counter increments.

    Without this shape every failure path in this suite hits the TIMEOUT
    branch of ``attribute_failure``, whose evidence carries only a
    ``termination_reason`` — leaving ``by_tool`` empty and the byte-identity
    gate blind to a future field-order or type divergence on that dict.
    """
    return _make_trajectory(
        task_id="task-tool-fail",
        binary_pass=False,
        score=0.0,
        status=TrialStatus.FAILED,
        termination_reason=None,
        tool_log=[
            recorded_call(
                "run_python",
                status=ToolExecutionStatus.ERROR,
                output="SyntaxError: unexpected EOF",
            ),
        ],
    )


def _augment_task_metrics(
    task_metrics: dict, *, task_id: str, benchmark_type: str = "airline"
) -> dict:
    """Mimic the orchestrator's per-row augmentation (see
    ``orchestrator.py`` around the ``calculate_task_metrics`` call site)."""
    task_metrics["task_id"] = task_id
    task_metrics["benchmark_type"] = benchmark_type
    task_metrics["complexity"] = "simple"
    task_metrics["expected_failure_modes"] = ["timeout_or_resource"]
    task_metrics["tags"] = ["domain:airline", "smoke"]
    return task_metrics


def _round_trip(model_cls, payload) -> None:
    """Validate → dump → assert equal. Failure means either a field drop
    or a serialisation-order divergence between the model and the source
    dict.

    ``exclude_unset=True`` on the dump matches the wire behaviour of the
    current writer path: keys the metric-calc functions did not set are
    absent from the JSON (e.g. ``success_rate_macro`` on a weighted run,
    ``success_rate_micro`` on an unweighted one). ``exclude_none`` would
    over-prune — real ``None`` values (``total_cost_usd`` when no trial
    reported a cost) must round-trip as ``None``, not drop.
    """
    model = model_cls.model_validate(payload)
    dumped = model.model_dump(by_alias=True, mode="json", exclude_unset=True)
    assert dumped == payload, (
        f"{model_cls.__name__} round-trip lost or reshaped fields. "
        f"expected={payload!r}\n  got={dumped!r}"
    )


# ---------------------------------------------------------------------------
# PerTaskMetrics
# ---------------------------------------------------------------------------


def test_per_task_metrics_round_trip_from_real_metric_calc() -> None:
    """A ``PerTaskMetrics`` model round-trips the dict the production
    ``calculate_task_metrics`` produces (plus the orchestrator's task-id
    / metadata augmentation)."""
    trajectories = [
        _make_trajectory(trial_index=0, binary_pass=True, score=1.0),
        _make_trajectory(trial_index=1, binary_pass=False, score=0.4),
        _make_trajectory(trial_index=2, binary_pass=True, score=0.9, stuck_detected=True),
    ]
    payload = calculate_task_metrics(trajectories)
    payload = _augment_task_metrics(payload, task_id="task-1")

    _round_trip(PerTaskMetrics, payload)


def test_per_task_metrics_round_trip_with_none_cost() -> None:
    """When no trial reported a cost, ``total_cost_usd`` / ``avg_cost_usd``
    are ``None``. The model must preserve ``None`` (not collapse to 0)."""
    trajectories = [
        _make_trajectory(trial_index=0, cost_usd=None),
        _make_trajectory(trial_index=1, cost_usd=None),
    ]
    payload = calculate_task_metrics(trajectories)
    payload = _augment_task_metrics(payload, task_id="task-none-cost")

    assert payload["total_cost_usd"] is None
    assert payload["avg_cost_usd"] is None
    _round_trip(PerTaskMetrics, payload)


# ---------------------------------------------------------------------------
# AggregateMetrics + RunAggregate
# ---------------------------------------------------------------------------


def _sample_task_metrics_list() -> list[dict]:
    """Two task-metric rows fed into aggregate / slice calculators."""
    t1 = calculate_task_metrics(
        [_make_trajectory(trial_index=i, binary_pass=(i % 2 == 0)) for i in range(3)]
    )
    _augment_task_metrics(t1, task_id="task-1")
    t2 = calculate_task_metrics(
        [
            _make_trajectory(trial_index=0, binary_pass=True),
            _make_trajectory(trial_index=1, binary_pass=True),
        ]
    )
    _augment_task_metrics(t2, task_id="task-2", benchmark_type="retail")
    t2["complexity"] = "complex"
    return [t1, t2]


def test_aggregate_metrics_round_trip_weighted() -> None:
    """Weighted (micro-average) aggregate — populates ``*_micro`` fields,
    leaves ``*_macro`` un-set (``None``)."""
    payload = calculate_aggregate_metrics(_sample_task_metrics_list(), weighted=True)
    _round_trip(AggregateMetrics, payload)


def test_aggregate_metrics_round_trip_unweighted() -> None:
    """Unweighted (macro-average) aggregate — populates ``*_macro`` fields,
    leaves ``*_micro`` un-set (``None``)."""
    payload = calculate_aggregate_metrics(_sample_task_metrics_list(), weighted=False)
    _round_trip(AggregateMetrics, payload)


def test_run_aggregate_round_trip_with_schema_version() -> None:
    """``RunAggregate`` = ``AggregateMetrics`` + the ``schema_version``
    envelope the orchestrator stamps before writing ``aggregate.json``."""
    payload = calculate_aggregate_metrics(_sample_task_metrics_list(), weighted=True)
    payload["schema_version"] = AGGREGATE_SCHEMA_VERSION

    _round_trip(RunAggregate, payload)


# ---------------------------------------------------------------------------
# captured_service_logs roll-up (#337)
# ---------------------------------------------------------------------------


def _captured_service_logs_payload() -> dict[str, Any]:
    """A populated roll-up covering all three capture sources — a
    provision-failure and a trial-body entry (per-trial), plus a
    shared-stack-materialise entry (run-level, ``task_id``/``trial_index``
    ``None``). The trial-body entry carries ``capture_reason: None``,
    exercising both the ``None`` and populated paths of that field, and
    ``db`` recurs across two entries so ``per_service_bytes`` sums it."""
    return {
        "captures": 3,
        "total_bytes": 9216,
        "per_service_bytes": {"db": 5120, "runner": 512, "api": 3584},
        "entries": [
            {
                "task_id": "task-1",
                "trial_index": 0,
                "source": "provision_failure",
                "capture_reason": "provision_error",
                "total_bytes": 4608,
                "services": {"db": 4096, "runner": 512},
            },
            {
                "task_id": "task-2",
                "trial_index": 1,
                "source": "trial_body",
                "capture_reason": None,
                "total_bytes": 1024,
                "services": {"db": 1024},
            },
            {
                "task_id": None,
                "trial_index": None,
                "source": "shared_stack_materialise",
                "capture_reason": "materialise_error",
                "total_bytes": 3584,
                "services": {"api": 3584},
            },
        ],
    }


def test_service_log_capture_source_enum_values() -> None:
    """The closed source vocabulary must be exactly the three lowercase
    strings the collector produces — a rename here is a wire break."""
    assert [s.value for s in ServiceLogCaptureSource] == [
        "provision_failure",
        "trial_body",
        "shared_stack_materialise",
    ]


def test_run_aggregate_round_trip_with_captured_service_logs() -> None:
    """A ``RunAggregate`` payload carrying a populated
    ``captured_service_logs`` — one entry per source, including a
    run-level entry (``task_id``/``trial_index`` ``None``) and a
    trial-body entry (``capture_reason`` ``None``) — round-trips
    byte-identically."""
    payload = calculate_aggregate_metrics(_sample_task_metrics_list(), weighted=True)
    payload["schema_version"] = AGGREGATE_SCHEMA_VERSION
    payload["captured_service_logs"] = _captured_service_logs_payload()

    _round_trip(RunAggregate, payload)


def test_run_aggregate_round_trip_omitting_captured_service_logs() -> None:
    """A payload that omits ``captured_service_logs`` still round-trips
    under ``exclude_unset=True`` — the optional field stays absent, so a
    pre-feature ``aggregate.json`` (no key) is distinguishable from a
    clean-run zero roll-up (key present, ``captures: 0``)."""
    payload = calculate_aggregate_metrics(_sample_task_metrics_list(), weighted=True)
    payload["schema_version"] = AGGREGATE_SCHEMA_VERSION
    assert "captured_service_logs" not in payload

    model = RunAggregate.model_validate(payload)
    dumped = model.model_dump(by_alias=True, mode="json", exclude_unset=True)
    assert "captured_service_logs" not in dumped
    _round_trip(RunAggregate, payload)


def test_captured_service_logs_rollup_zero_envelope() -> None:
    """A clean-run zero roll-up serialises with all keys present — the
    explicit ``captures: 0`` / empty-collections shape that distinguishes
    'feature shipped, nothing captured' from a pre-feature file."""
    rollup = CapturedServiceLogsRollup(captures=0, total_bytes=0)
    dumped = rollup.model_dump(mode="json")
    assert dumped == {
        "captures": 0,
        "total_bytes": 0,
        "per_service_bytes": {},
        "entries": [],
    }


def test_service_log_capture_entry_run_level_shape() -> None:
    """The run-level (shared-stack) entry carries ``task_id`` and
    ``trial_index`` ``None`` and validates against the source enum."""
    entry = ServiceLogCaptureEntry.model_validate(
        {
            "source": "shared_stack_materialise",
            "capture_reason": "materialise_error",
            "total_bytes": 3584,
            "services": {"api": 3584},
        }
    )
    assert entry.task_id is None
    assert entry.trial_index is None
    assert entry.source is ServiceLogCaptureSource.SHARED_STACK_MATERIALISE


# ---------------------------------------------------------------------------
# MetadataSlices
# ---------------------------------------------------------------------------


def test_metadata_slices_round_trip() -> None:
    """The four ``by_*`` dictionaries with :class:`AggregateMetrics` slice
    values. Empty and populated dimensions both round-trip."""
    tasks = _sample_task_metrics_list()
    payload = {
        "by_benchmark_type": {"airline": calculate_aggregate_metrics([tasks[0]], weighted=True)},
        "by_complexity": {"simple": calculate_aggregate_metrics(tasks, weighted=True)},
        "by_tag": {"domain:airline": calculate_aggregate_metrics([tasks[0]], weighted=True)},
        "by_expected_failure_mode": {},
    }

    _round_trip(MetadataSlices, payload)


# ---------------------------------------------------------------------------
# FailureAttribution
# ---------------------------------------------------------------------------


def test_failure_record_round_trip_from_real_attribution() -> None:
    """One record produced by ``attribute_failure`` on a failed trajectory."""
    trajectory = _make_trajectory(
        binary_pass=False,
        score=0.0,
        status=TrialStatus.TIMEOUT,
        termination_reason=TerminationReason.TIMEOUT,
    )
    payload = attribute_failure(trajectory)

    _round_trip(FailureRecord, payload)


def test_failure_summary_round_trip_zero_failures() -> None:
    """Zero-failure branch — ``deterministic_attribution_coverage`` is
    ``None`` (division-by-zero guarded in the source)."""
    payload = summarize_failure_attributions([])
    assert payload["deterministic_attribution_coverage"] is None
    _round_trip(FailureSummary, payload)


def test_failure_summary_round_trip_with_failures() -> None:
    """Populated summary — attribution records feed the by-class / by-tool
    counters and drive coverage above 0.

    Mixes TIMEOUT (populates ``by_failure_class['timeout_or_resource']``)
    with tool-execution failures (populates
    ``by_failure_class['tool_execution']`` AND ``by_tool[<tool_name>]``)
    so both counters land non-empty — the previous suite exercised only
    the TIMEOUT branch and left ``by_tool`` as ``{}`` on every dump.
    """
    attributions = [
        attribute_failure(
            _make_trajectory(
                trial_index=i,
                binary_pass=False,
                status=TrialStatus.TIMEOUT,
                termination_reason=TerminationReason.TIMEOUT,
            )
        )
        for i in range(2)
    ]
    attributions.append(attribute_failure(_make_tool_execution_failure()))
    payload = summarize_failure_attributions(attributions)
    assert payload["by_tool"], "guard: by_tool must be non-empty for this test to matter"

    _round_trip(FailureSummary, payload)


def test_failure_attribution_envelope_round_trip() -> None:
    """The full ``{summary: ..., failures: [...]}`` envelope the writer
    persists to ``failure_attribution.json``."""
    attributions = [
        attribute_failure(
            _make_trajectory(
                task_id=f"task-{i}",
                trial_index=0,
                binary_pass=False,
                status=TrialStatus.TIMEOUT,
                termination_reason=TerminationReason.TIMEOUT,
            )
        )
        for i in range(2)
    ]
    payload = {
        "summary": summarize_failure_attributions(attributions),
        "failures": attributions,
    }

    _round_trip(FailureAttribution, payload)


# ---------------------------------------------------------------------------
# Wire-format invariants (closes #152, #153)
# ---------------------------------------------------------------------------


def test_schema_version_survives_exclude_unset() -> None:
    """``RunAggregate.schema_version`` must appear in the dumped output
    even when the model is constructed without an explicit value AND
    dumped with ``exclude_unset=True`` (the mode the migrated writer
    will use to preserve wire-format parity with the current dict path).

    The regression this catches: once the writer migrates from
    ``json.dump(dict)`` to ``model.model_dump_json(exclude_unset=True)``,
    a ``RunAggregate`` whose ``schema_version`` was left at the model
    default would silently drop the envelope field — every downstream
    dashboard that dispatches on ``schema_version`` would break. The
    model's ``@model_serializer`` forces the field into the dump
    regardless. Closes #152.
    """
    # Payload deliberately omits ``schema_version`` — the model must
    # still emit it via the default.
    payload = calculate_aggregate_metrics(_sample_task_metrics_list(), weighted=True)
    assert "schema_version" not in payload, "guard: payload must not stamp schema_version"

    model = RunAggregate.model_validate(payload)
    dumped = model.model_dump(by_alias=True, mode="json", exclude_unset=True)

    assert "schema_version" in dumped, (
        "schema_version must survive exclude_unset=True on RunAggregate — "
        f"got dumped keys: {sorted(dumped.keys())}"
    )
    assert dumped["schema_version"] == AGGREGATE_SCHEMA_VERSION, (
        f"schema_version default drifted; expected {AGGREGATE_SCHEMA_VERSION}, "
        f"got {dumped['schema_version']!r}"
    )

    # And it survives a JSON round-trip too.
    reloaded = RunAggregate.model_validate_json(
        model.model_dump_json(by_alias=True, exclude_unset=True)
    )
    assert reloaded.schema_version == AGGREGATE_SCHEMA_VERSION


def test_int_valued_numeric_fields_preserve_int_type() -> None:
    """The ``int | float`` unions on token-count / latency-percentile /
    cost-sum fields must preserve ``int`` inputs verbatim through
    validation AND JSON dump. A regression that narrows any of these
    fields back to ``float`` would coerce ``int 42`` → ``42.0`` on
    validation and emit ``42.0`` in the JSON — a byte-level wire drift
    invisible to Python-dict comparison (``42 == 42.0``) but real
    when downstream tooling diffs the JSON files byte-for-byte.

    Today's producers in ``metrics.py`` happen to always emit
    ``float`` for these fields (division short-circuits on empty
    input), so no live regression exists. This test guards against a
    future producer refactor introducing ``int`` output — the model
    must handle it correctly without a coordinated widening. Closes #153.
    """
    # Hand-construct a payload with int-valued token counts + latency
    # percentiles. The producer path today never emits these as int,
    # so this is the only way to exercise the int branch of the union.
    task_payload: dict[str, Any] = {
        "task_id": "int-preserve-guard",
        "benchmark_type": "airline",
        "complexity": "simple",
        "tags": [],
        "expected_failure_modes": [],
        "total_trials": 1,
        "measured_trials": 1,
        "successful_trials": 1,
        "success_rate": 1.0,  # rate — stays float
        "avg_score": 1.0,  # rate — stays float
        "avg_latency_s": 5,  # int — union widened field
        "avg_turns": 3,  # int — union widened field
        "avg_tool_calls": 2,  # int — union widened field
        # Token counts — natural integers.
        "avg_prompt_tokens": 100,
        "avg_completion_tokens": 40,
        "avg_reasoning_tokens": 10,
        "avg_cached_tokens": 5,
        "avg_cache_creation_input_tokens": 3,
        "avg_cache_read_input_tokens": 2,
        # Latency percentiles — could be int seconds.
        "latency_p50_s": 4,
        "latency_p90_s": 6,
        "latency_p99_s": 8,
        "api_call_latency_p50_s": 1,
        "api_call_latency_p90_s": 2,
        "api_call_latency_p99_s": 3,
        "stuck_rate": 0,
    }
    model = PerTaskMetrics.model_validate(task_payload)

    # Type preservation at the Python level — union picks int on validation.
    for field in (
        "avg_latency_s",
        "avg_turns",
        "avg_tool_calls",
        "avg_prompt_tokens",
        "avg_completion_tokens",
        "avg_reasoning_tokens",
        "avg_cached_tokens",
        "avg_cache_creation_input_tokens",
        "avg_cache_read_input_tokens",
        "latency_p50_s",
        "latency_p90_s",
        "latency_p99_s",
        "api_call_latency_p50_s",
        "api_call_latency_p90_s",
        "api_call_latency_p99_s",
        "stuck_rate",
    ):
        value = getattr(model, field)
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"PerTaskMetrics.{field}: int input coerced to {type(value).__name__} — "
            f"the int|float union is broken. Got {value!r}."
        )

    # Type preservation on the JSON wire — dump keys must be int, not float.
    dumped = model.model_dump(by_alias=True, mode="json")
    dumped_json = json.dumps(dumped, sort_keys=True)
    # An int field dumped as int has no trailing ``.0`` in the JSON string.
    for field in ("avg_prompt_tokens", "latency_p50_s", "stuck_rate"):
        assert isinstance(dumped[field], int) and not isinstance(dumped[field], bool), (
            f"PerTaskMetrics.{field} dumped as {type(dumped[field]).__name__}, "
            f"expected int. Full dump: {dumped_json}"
        )


def test_int_valued_aggregate_fields_preserve_int_type() -> None:
    """Same invariant as
    :func:`test_int_valued_numeric_fields_preserve_int_type` but for the
    ``AggregateMetrics`` shape. Divison-produced rate fields on
    ``AggregateMetrics`` (``success_rate_micro/macro``, ``avg_score_*``,
    ``pass_at_*_macro``) stay narrow ``float`` — a caller passing ``int``
    there is a producer bug, not a wire invariant to preserve, so we
    do NOT assert int preservation for those fields."""
    agg_payload: dict[str, Any] = {
        "total_tasks": 2,
        "total_trials": 5,
        "measured_trials": 5,
        "success_rate_micro": 1.0,  # rate — stays float
        "avg_score_micro": 1.0,  # rate — stays float
        "avg_latency_s": 5,  # int — widened
        "avg_turns": 3,  # int — widened
        "avg_tool_calls": 2,  # int — widened
        "stuck_rate": 0,  # int — widened
        "total_prompt_tokens": 500,
        "total_completion_tokens": 200,
        "total_reasoning_tokens": 50,
        "total_cached_tokens": 25,
        "total_cache_creation_input_tokens": 15,
        "total_cache_read_input_tokens": 10,
        "avg_prompt_tokens": 100,
        "avg_completion_tokens": 40,
        "avg_reasoning_tokens": 10,
        "avg_cached_tokens": 5,
        "avg_cache_creation_input_tokens": 3,
        "avg_cache_read_input_tokens": 2,
        "latency_p50_s_macro": 4,
        "latency_p90_s_macro": 6,
        "latency_p99_s_macro": 8,
    }
    model = AggregateMetrics.model_validate(agg_payload)
    for field in (
        "avg_latency_s",
        "avg_turns",
        "avg_tool_calls",
        "stuck_rate",
        "total_prompt_tokens",
        "avg_prompt_tokens",
        "latency_p50_s_macro",
    ):
        value = getattr(model, field)
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"AggregateMetrics.{field}: int input coerced to {type(value).__name__}. Got {value!r}."
        )

    # And rate fields DID stay narrow float — a regression that widens them
    # would be caught by mypy at consumer sites, but a runtime guard here
    # documents the invariant.
    assert isinstance(model.success_rate_micro, float), (
        "AggregateMetrics.success_rate_micro widened to int|float unexpectedly — "
        "rate-shaped fields must stay narrow float for consumer type contracts."
    )


def test_current_producer_output_matches_model_dump_byte_for_byte() -> None:
    """Round-trip guard against the current live producer output.

    Even though the ``int | float`` union covers a case the producer
    doesn't emit today, the by-product must still hold: every dict the
    production metric-calc functions actually produce today survives a
    model round-trip byte-for-byte in the JSON representation. This
    catches any accidental type coercion in the round trip for the
    fields we DID narrow (``success_rate_*``, ``avg_score_*``,
    ``pass_at_*_macro``)."""

    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, default=str)

    task_metrics_list = _sample_task_metrics_list()
    for weighted in (True, False):
        agg_payload = calculate_aggregate_metrics(task_metrics_list, weighted=weighted)
        model = AggregateMetrics.model_validate(agg_payload)
        dumped = model.model_dump(by_alias=True, mode="json", exclude_unset=True)
        assert _canonical(agg_payload) == _canonical(dumped), (
            f"AggregateMetrics(weighted={weighted}) JSON drift between dict and "
            f"model path.\nsource: {_canonical(agg_payload)}\nmodel:  {_canonical(dumped)}"
        )
