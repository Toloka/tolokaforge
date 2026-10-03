"""The comparison view's record crosses from the runner's grade to the host's, and lands.

``Grade.comparison_view_json`` is presence-tracked on both wires, and the host's
``Grade.comparison_view`` is left out of every dump while it is ``None``: a grade without
a view encodes, prints and writes byte-identically to a grade without the field.
"""

from __future__ import annotations

import json

import pytest
import yaml

from tolokaforge.core.grading.comparison_view import ComparisonViewConfig
from tolokaforge.core.grading.composite_fold import build_grade_reasons
from tolokaforge.core.grading.pre_hash import (
    PreHashDeclaration,
    comparison_view_grade_record,
    comparison_view_reason,
    view_the_pair,
)
from tolokaforge.core.models import Grade
from tolokaforge.core.trial_grader import GradingFailedError, _parse_grade_result
from tolokaforge.grader.client import _grade_from_wire
from tolokaforge.grader.service import _grade_to_wire
from tolokaforge.runner.grading import grade_to_runner_wire

pytestmark = pytest.mark.unit

_VIEW = ComparisonViewConfig.model_validate(
    {
        "version": 1,
        "rules": [{"kind": "normalize_ids", "table": "docs", "key": ["source_id"]}],
    }
)


def _record(*, matched: bool) -> dict:
    trial = {"docs": [{"id": "D3", "source_id": "S1", "note": "x" if matched else "y"}]}
    golden = {"docs": [{"id": "D2", "source_id": "S1", "note": "x"}]}
    outcome = view_the_pair(
        trial, golden, initial={"docs": []}, declaration=PreHashDeclaration(view=_VIEW)
    )
    return comparison_view_grade_record(outcome, matched=matched).model_dump(mode="json")


def test_a_grade_without_a_view_dumps_without_the_key() -> None:
    grade = Grade(binary_pass=True, score=1.0)
    assert "comparison_view" not in grade.model_dump()
    assert "comparison_view" not in json.loads(grade.model_dump_json())
    assert "comparison_view" not in yaml.safe_load(yaml.safe_dump(grade.model_dump(mode="json")))


def test_a_grade_with_a_view_round_trips_it() -> None:
    record = _record(matched=False)
    grade = Grade(binary_pass=False, score=0.0, comparison_view=record)
    assert Grade.model_validate_json(grade.model_dump_json()).comparison_view == record


def _raw(**fields) -> dict:
    return {"binary_pass": False, "score": 0.0, "components": {}, "reasons": "", **fields}


def test_the_host_reads_the_runners_record_into_the_grade() -> None:
    record = _record(matched=False)
    grade = _parse_grade_result(_raw(comparison_view_json=json.dumps(record)))
    assert grade.comparison_view == record
    assert _parse_grade_result(_raw(comparison_view_json=None)).comparison_view is None
    assert _parse_grade_result(_raw()).comparison_view is None


def test_an_unreadable_record_is_a_grading_failure_not_a_grade_without_one() -> None:
    with pytest.raises(GradingFailedError, match="comparison_view_json payload is not readable"):
        _parse_grade_result(_raw(comparison_view_json=json.dumps({"golden": {"version": 1}})))


def test_both_wires_carry_the_record_and_leave_it_unset_without_one() -> None:
    record = _record(matched=True)
    with_view = Grade(binary_pass=True, score=1.0, comparison_view=record)
    without = Grade(binary_pass=True, score=1.0)

    runner_wire = grade_to_runner_wire(with_view)
    assert json.loads(runner_wire.comparison_view_json) == record
    assert not grade_to_runner_wire(without).HasField("comparison_view_json")

    grader_wire = _grade_to_wire(with_view)
    assert json.loads(grader_wire.comparison_view_json) == record
    assert not _grade_to_wire(without).HasField("comparison_view_json")
    assert _parse_grade_result(_grade_from_wire(grader_wire)).comparison_view == record
    assert _parse_grade_result(_grade_from_wire(_grade_to_wire(without))).comparison_view is None


def test_runner_wire_preserves_host_grader_snapshots() -> None:
    from tolokaforge.core.models.grade import GradingStateSnapshots

    snapshots = GradingStateSnapshots(
        source="environment replay",
        initial={"agent": {}},
        golden={"agent": {"x": 1}},
        final={"agent": {"x": 1}},
    )
    wire = grade_to_runner_wire(Grade(binary_pass=True, score=1.0, state_snapshots=snapshots))
    restored = _parse_grade_result(_raw(state_snapshots_json=wire.state_snapshots_json))
    assert restored.state_snapshots == snapshots
    assert not grade_to_runner_wire(Grade(binary_pass=True, score=1.0)).HasField(
        "state_snapshots_json"
    )


def test_unreadable_snapshots_refuse_with_the_runners_completed_evidence() -> None:
    record = _record(matched=False)
    raw = _raw(
        state_snapshots_json="{not json",
        state_diff_json=json.dumps({"docs": {"changed": 1}}),
        comparison_view_json=json.dumps(record),
        judge_report={"calls": 2, "prompt_tokens": 30, "completion_tokens": 7, "cost_usd": 0.01},
    )
    with pytest.raises(
        GradingFailedError, match="state_snapshots_json payload is not readable"
    ) as e:
        _parse_grade_result(raw)
    assert e.value.judge_usage is not None
    assert (e.value.judge_usage.calls, e.value.judge_usage.prompt_tokens) == (2, 30)
    assert e.value.state_diff == {"docs": {"changed": 1}}
    assert e.value.comparison_view == record
    assert e.value.state_snapshots is None


def test_the_view_reason_follows_the_hash_sentence() -> None:
    from tolokaforge.runner.models import ComparisonViewGradeRecord

    record = ComparisonViewGradeRecord.model_validate(_record(matched=False))
    reason = comparison_view_reason(record)
    assert reason is not None
    rendered = build_grade_reasons(
        {"hash_score": 0.0, "hash_match": False}, comparison_view_reason=reason
    )
    assert rendered == f"State: hash mismatch | {reason}"
    unscored = build_grade_reasons({"hash_score": -1.0}, comparison_view_reason=reason)
    assert reason not in unscored
