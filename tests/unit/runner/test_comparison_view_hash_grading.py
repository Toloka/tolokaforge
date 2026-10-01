"""The runner grades a hash through a declared comparison view.

``_execute_hash_grading`` reads both full states, restores the trial's database, and
only then runs the view and the steps after it — so a view that cannot be computed
fails the grade and leaves the trial's database as it was. A trial whose state
collides under ``normalize_ids`` fails with the collision as the reason, and the grade
carries both views' records in ``Grade.comparison_view_json``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from tests.utils.comparison_view_runner import (
    GOLDEN_TOOL,
    Verdict,
    grade_through_the_runner,
    verdict_of,
)
from tolokaforge.runner.models import ComparisonViewGradeRecord

pytestmark = pytest.mark.unit

_INITIAL: dict[str, list[dict[str, Any]]] = {
    "documents": [{"id": "D1", "source_id": "S1", "created_at": "t0"}],
    "corrections": [],
}
_VIEW = {
    "version": 1,
    "rules": [
        {
            "kind": "normalize_ids",
            "table": "documents",
            "key": ["source_id"],
            "references": [{"table": "corrections", "field": "document_ref"}],
        }
    ],
}


def _description(task_id: str, *, view: dict[str, Any] | None = _VIEW) -> dict[str, Any]:
    state_checks: dict[str, Any] = {
        "hash_enabled": True,
        "golden_actions": [{"tool_name": GOLDEN_TOOL, "arguments": {}}],
    }
    if view is not None:
        state_checks["comparison_view"] = view
    return {
        "task_id": task_id,
        "name": "A view over filed documents",
        "category": "test",
        "description": "A document filed under a generated id, and a correction citing it.",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": _INITIAL,
            "unstable_fields": [
                {"table_name": "documents", "field_name": "id", "reason": "auto_id"},
                {"table_name": "document", "field_name": "created_at", "reason": "timestamp"},
            ],
        },
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 0.5,
            "state_checks": state_checks,
        },
    }


def _filed(new_id: str, *, created_at: str, reason: str = "typo") -> dict[str, list[dict]]:
    return {
        "documents": [
            *copy.deepcopy(_INITIAL["documents"]),
            {"id": new_id, "source_id": "S2", "created_at": created_at},
        ],
        "corrections": [{"id": "C1", "document_ref": new_id, "reason": reason}],
    }


def _record(response) -> ComparisonViewGradeRecord:
    assert response.grade.comparison_view_json, "the grade carries no comparison view record"
    return ComparisonViewGradeRecord.model_validate_json(response.grade.comparison_view_json)


def test_a_trial_that_differs_only_in_a_generated_id_passes(runner_service, mock_grpc_context):
    response = grade_through_the_runner(
        runner_service,
        mock_grpc_context,
        description=_description("view_rename"),
        trial_id="view_rename:0",
        trial=_filed("D3", created_at="t9"),
        golden=_filed("D2", created_at="t5"),
    )
    assert verdict_of(response) is Verdict.PASS, response.error or response.grade.reasons
    record = _record(response)
    assert record.view_diff is None and record.trial_collision is None
    assert record.trial is not None and record.trial.config_sha256 == record.golden.config_sha256
    assert [field.dotted for field in record.golden.rekeyed_fields] == ["documents.id"]


def test_without_the_view_the_same_pair_fails_and_records_no_view(
    runner_service, mock_grpc_context
):
    """The control: the server-side path, untouched, sees the generated id."""
    description = _description("view_absent", view=None)
    description["initial_state"]["unstable_fields"] = [
        {"table_name": "document", "field_name": "created_at", "reason": "timestamp"}
    ]
    response = grade_through_the_runner(
        runner_service,
        mock_grpc_context,
        description=description,
        trial_id="view_absent:0",
        trial=_filed("D3", created_at="t9"),
        golden=_filed("D2", created_at="t5"),
    )
    assert verdict_of(response) is Verdict.FAIL
    assert not response.grade.HasField("comparison_view_json")


def test_a_difference_the_view_keeps_fails_with_the_view_diff(runner_service, mock_grpc_context):
    response = grade_through_the_runner(
        runner_service,
        mock_grpc_context,
        description=_description("view_mismatch"),
        trial_id="view_mismatch:0",
        trial=_filed("D3", created_at="t9", reason="wrong client"),
        golden=_filed("D2", created_at="t5"),
    )
    assert verdict_of(response) is Verdict.FAIL
    record = _record(response)
    assert record.view_diff is not None and set(record.view_diff.tables) == {"corrections"}
    assert f"Comparison view: {record.view_diff.summary}" in response.grade.reasons
    raw = json.loads(response.grade.state_diff_json)
    assert raw["tables"], "the raw diff of the stable states rides beside the view diff"


def test_a_trial_collision_fails_the_trial_naming_the_ids(runner_service, mock_grpc_context):
    trial = _filed("D3", created_at="t9")
    trial["documents"].append({"id": "D4", "source_id": "S2", "created_at": "t10"})
    response = grade_through_the_runner(
        runner_service,
        mock_grpc_context,
        description=_description("view_collision"),
        trial_id="view_collision:0",
        trial=trial,
        golden=_filed("D2", created_at="t5"),
    )
    assert verdict_of(response) is Verdict.FAIL, response.error
    record = _record(response)
    assert record.trial is None and record.trial_collision is not None
    assert set(record.trial_collision.ids) == {"D3", "D4"}
    assert "Comparison view: the trial's state cannot be re-keyed" in response.grade.reasons


def test_a_golden_view_error_is_a_grading_error_that_leaves_the_trials_database_as_it_was(
    runner_service, mock_grpc_context
):
    """The view runs after the restore, so the golden state never stays in the database."""
    golden = _filed("D2", created_at="t5")
    golden["documents"].append({"id": "D7", "source_id": "S2", "created_at": "t6"})
    trial = _filed("D3", created_at="t9")
    response = grade_through_the_runner(
        runner_service,
        mock_grpc_context,
        description=_description("view_golden_error"),
        trial_id="view_golden_error:0",
        trial=trial,
        golden=golden,
    )
    assert verdict_of(response) is Verdict.GRADING_ERROR
    assert "ComparisonViewCollision" in response.error
    left = runner_service._run_async(runner_service.db_client.get_state("view_golden_error:0"))
    assert left.data == trial, "a failing view left something other than the trial's state"
