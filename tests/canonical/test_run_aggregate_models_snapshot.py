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

from datetime import UTC, datetime
from typing import Any

import pytest

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
    TerminationReason,
    Trajectory,
    TrialStatus,
    Usage,
)
from tolokaforge.core.output.aggregate_models import (
    AggregateMetrics,
    FailureAttribution,
    FailureRecord,
    FailureSummary,
    MetadataSlices,
    PerTaskMetrics,
    RunAggregate,
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
    tool_log: list[dict[str, Any]] | None = None,
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
            {"tool": "run_python", "success": False, "error": "SyntaxError: unexpected EOF"},
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
    payload["schema_version"] = 1

    _round_trip(RunAggregate, payload)


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
    assert (
        dumped["schema_version"] == 1
    ), f"schema_version default drifted; expected 1, got {dumped['schema_version']!r}"

    # And it survives a JSON round-trip too.
    reloaded = RunAggregate.model_validate_json(
        model.model_dump_json(by_alias=True, exclude_unset=True)
    )
    assert reloaded.schema_version == 1


def test_int_float_json_string_no_drift() -> None:
    """The producer emits ``int`` for empty-sum aggregates
    (``sum([]) == 0``) and ``float`` for populated ones. The model's
    ``int | float`` unions preserve the source type on validation so
    the dumped JSON is byte-identical to the source dict's JSON — no
    ``0`` → ``0.0`` drift on the wire.

    A pre-fix version of the model with `float` fields would coerce
    ``0`` to ``0.0`` on validation, and the JSON dump would carry
    ``0.0`` where the source dict carries ``0`` — a wire-format break
    invisible to Python-dict comparison (``0 == 0.0``) but real when
    downstream tooling diffs the JSON files byte-for-byte. Closes #153.
    """
    import json

    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, default=str)

    # Empty aggregates — the harshest test for int/float drift because
    # every ``total_*`` / ``avg_*`` is ``sum([]) == 0`` on the source.
    empty_traj = _make_trajectory()
    task_payload = calculate_task_metrics([empty_traj])
    _augment_task_metrics(task_payload, task_id="task-int-float")

    # PerTaskMetrics — source dict JSON vs. model-dumped JSON must match
    # byte-for-byte.
    model = PerTaskMetrics.model_validate(task_payload)
    dumped_dict = model.model_dump(by_alias=True, mode="json", exclude_unset=True)
    source_json = _canonical(task_payload)
    dumped_json = _canonical(dumped_dict)
    assert source_json == dumped_json, (
        "PerTaskMetrics JSON drift between dict path and model path.\n"
        f"source: {source_json}\n"
        f"model:  {dumped_json}"
    )

    # AggregateMetrics — same story; empty-aggregate `total_*_tokens`
    # start as ``int`` from the producer.
    agg_payload = calculate_aggregate_metrics([task_payload], weighted=True)
    agg_model = AggregateMetrics.model_validate(agg_payload)
    agg_dumped = agg_model.model_dump(by_alias=True, mode="json", exclude_unset=True)
    assert _canonical(agg_payload) == _canonical(agg_dumped), (
        "AggregateMetrics JSON drift between dict path and model path.\n"
        f"source: {_canonical(agg_payload)}\n"
        f"model:  {_canonical(agg_dumped)}"
    )

    # RunAggregate — model injects `schema_version`. Compare with the
    # source dict stamped to match.
    run_payload = dict(agg_payload)
    run_payload["schema_version"] = 1
    run_model = RunAggregate.model_validate(run_payload)
    run_dumped = run_model.model_dump(by_alias=True, mode="json", exclude_unset=True)
    assert _canonical(run_payload) == _canonical(run_dumped), (
        "RunAggregate JSON drift between dict path and model path.\n"
        f"source: {_canonical(run_payload)}\n"
        f"model:  {_canonical(run_dumped)}"
    )

    # FailureAttribution — the envelope isn't a numeric-heavy shape,
    # but we exercise it to prove the invariant holds for every
    # writer artifact.
    attributions = [attribute_failure(empty_traj)]
    fa_payload = {
        "summary": summarize_failure_attributions(attributions),
        "failures": attributions,
    }
    fa_model = FailureAttribution.model_validate(fa_payload)
    fa_dumped = fa_model.model_dump(by_alias=True, mode="json", exclude_unset=True)
    assert _canonical(fa_payload) == _canonical(fa_dumped), (
        "FailureAttribution JSON drift between dict path and model path.\n"
        f"source: {_canonical(fa_payload)}\n"
        f"model:  {_canonical(fa_dumped)}"
    )
