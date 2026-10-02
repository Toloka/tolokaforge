"""A comparison-view rule registered out of tree grades a trial on both substrates.

The rule is :class:`tests.utils.comparison_view_rules.DropField`, registered by a
throwaway distribution through real entry-point metadata. The runner resolves it when
``RegisterTrial`` validates the trial spec and again when the view applies; core
resolves it in ``check_hash``. Both reach one verdict and record one view, and a runner
whose installation lacks the rule refuses the trial at registration, naming the kinds it
has.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from tests.utils.comparison_view_rules import install_rule_distribution, target
from tests.utils.comparison_view_runner import (
    GOLDEN_TOOL,
    Verdict,
    grade_through_the_runner,
    verdict_of,
)
from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.core.grading.comparison_view import ComparisonViewConfig
from tolokaforge.core.grading.state_checks import StateChecker
from tolokaforge.runner.models import ComparisonViewGradeRecord

pytestmark = pytest.mark.unit

_INITIAL: dict[str, list[dict[str, Any]]] = {"orders": [{"id": "O1", "total": 5, "note": ""}]}
_VIEW = {"version": 1, "rules": [{"kind": "drop_field", "table": "orders", "field": "note"}]}


def _state(note: str, *, total: int = 5) -> dict[str, list[dict[str, Any]]]:
    return {"orders": [{"id": "O1", "total": total, "note": note}]}


def _description(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "name": "An order whose note does not count",
        "category": "test",
        "description": "An order the agent annotates; the note is free text.",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {"tables": copy.deepcopy(_INITIAL)},
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 0.5,
            "state_checks": {
                "hash_enabled": True,
                "golden_actions": [{"tool_name": GOLDEN_TOOL, "arguments": {}}],
                "comparison_view": _VIEW,
            },
        },
    }


@pytest.fixture
def drop_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_rule_distribution(monkeypatch, tmp_path, {"drop_field": target("DropField")})


@pytest.mark.usefixtures("drop_field")
@pytest.mark.parametrize(
    ("trial", "verdict"),
    [
        pytest.param(_state("left at the door"), Verdict.PASS, id="only-the-note-differs"),
        pytest.param(_state("as asked", total=6), Verdict.FAIL, id="the-total-differs"),
    ],
)
def test_a_registered_rule_grades_one_trial_alike_on_both_substrates(
    runner_service, mock_grpc_context, trial: dict[str, Any], verdict: Verdict
) -> None:
    golden = _state("as asked")
    task_id = f"registered_rule_{verdict.value}"
    response = grade_through_the_runner(
        runner_service,
        mock_grpc_context,
        description=_description(task_id),
        trial_id=f"{task_id}:0",
        trial=trial,
        golden=golden,
    )
    assert verdict_of(response) is verdict, response.error or response.grade.reasons
    runner_record = ComparisonViewGradeRecord.model_validate_json(
        response.grade.comparison_view_json
    )
    assert runner_record.trial is not None
    assert [application.kind for application in runner_record.trial.applied] == ["drop_field"]

    core = StateChecker().check_hash(
        copy.deepcopy(trial),
        expected_state=copy.deepcopy(golden),
        comparison_view=ComparisonViewConfig.model_validate(_VIEW),
        initial_state=copy.deepcopy(_INITIAL),
    )
    assert core.hash_match is (verdict is Verdict.PASS), core.reason
    assert core.comparison_view is not None
    assert core.comparison_view.model_dump(mode="json") == runner_record.model_dump(mode="json")


def test_a_runner_without_the_rule_refuses_the_trial_at_registration(
    runner_service, mock_grpc_context, tmp_path: Path
) -> None:
    """The host validated the spec with the rule installed; the runner's install lacks it."""
    with pytest.MonkeyPatch.context() as patch:
        install_rule_distribution(patch, tmp_path, {"drop_field": target("DropField")})
        spec = trial_spec_json(_description("missing_rule"), trial_id="missing_rule:0")
    registered = runner_service.RegisterTrial(
        register_request(spec, trial_id="missing_rule:0"), mock_grpc_context
    )
    assert registered.success is False
    assert "unknown comparison_view rule kind 'drop_field'" in registered.error
    assert "registered kinds: ['exclude_records', 'exclude_tables', 'normalize_ids']" in (
        registered.error
    )
