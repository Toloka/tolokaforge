"""Unit tests for ``tolokaforge.core.grading.judge_kinds._shared``.

Locks the helpers ``voted.py`` and ``auto_anchored.py`` reuse:
``member_failure_reason``, ``assert_construction_fields_match`` and ``sum_usage``.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge_kinds._shared import (
    assert_construction_fields_match,
    member_failure_reason,
    sum_usage,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.runner.models import CriterionResult

pytestmark = pytest.mark.unit


def _completed(**overrides) -> JudgeResult:
    defaults: dict = {
        "status": JudgeStatus.COMPLETED,
        "usage": JudgeUsage(),
        "reasons": "ok",
        "criterion_results": (
            CriterionResult(id="c0", met=True, score=1.0, justification="j\nVERDICT: MET"),
            CriterionResult(id="c1", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
    }
    defaults.update(overrides)
    return JudgeResult(**defaults)


class TestMemberFailureReason:
    def test_completed_with_all_verdicts_is_not_a_failure(self) -> None:
        assert member_failure_reason(_completed(), ("c0", "c1")) is None

    def test_errored_status_is_a_failure(self) -> None:
        """The helper's message is unit-agnostic — the caller supplies its own
        wrapping, so the errored result reads the same across callers."""
        result = JudgeResult(
            status=JudgeStatus.ERRORED,
            usage=JudgeUsage(),
            reasons="sample blew up",
        )
        reason = member_failure_reason(result, ("c0",))
        assert reason is not None
        assert "status=errored" in reason
        assert "sample blew up" in reason

    def test_missing_verdict_is_a_failure(self) -> None:
        result = _completed(
            criterion_results=(
                CriterionResult(id="c0", met=True, score=1.0, justification="j\nVERDICT: MET"),
            )
        )
        reason = member_failure_reason(result, ("c0", "c1"))
        assert reason is not None
        assert "missing verdicts" in reason
        assert "c1" in reason


class TestAssertConstructionFieldsMatch:
    def test_matching_fields_across_results_do_not_raise(self) -> None:
        results = [_completed(custom_system_prompt=True), _completed(custom_system_prompt=True)]
        assert_construction_fields_match(
            results, ("custom_system_prompt",), kind_label="voted_rubric", unit_noun="sample"
        )

    def test_divergent_field_raises_naming_field_and_both_values(self) -> None:
        results = [
            _completed(custom_system_prompt=False),
            _completed(custom_system_prompt=True),
        ]
        with pytest.raises(RuntimeError) as excinfo:
            assert_construction_fields_match(
                results, ("custom_system_prompt",), kind_label="voted_rubric", unit_noun="sample"
            )
        message = str(excinfo.value)
        assert "voted_rubric construction-field mismatch across samples" in message
        assert "'custom_system_prompt'" in message
        assert "sample 0 is False" in message
        assert "sample 1 is True" in message

    def test_unit_noun_and_kind_label_are_parametrised_into_the_message(self) -> None:
        results = [_completed(state_diff="a"), _completed(state_diff="b")]
        with pytest.raises(RuntimeError, match=r"voted_rubric.*samples.*sample 0.*sample 1"):
            assert_construction_fields_match(
                results, ("state_diff",), kind_label="voted_rubric", unit_noun="sample"
            )


class TestSumUsage:
    def test_the_stated_charge_sums_across_members(self) -> None:
        members = [
            _completed(usage=JudgeUsage(calls=2, cost_usd=0.002, billed_cost_usd=0.0021)),
            _completed(usage=JudgeUsage(calls=1, cost_usd=0.001, billed_cost_usd=0.0009)),
        ]
        usage = sum_usage(members)
        assert (usage.calls, usage.cost_usd) == (3, pytest.approx(0.003))
        assert usage.billed_cost_usd == pytest.approx(0.003)

    def test_a_member_that_stated_no_charge_leaves_the_sum_unknown(self) -> None:
        members = [
            _completed(usage=JudgeUsage(calls=1, cost_usd=0.001, billed_cost_usd=0.001)),
            _completed(usage=JudgeUsage(calls=1, cost_usd=0.001)),
        ]
        usage = sum_usage(members)
        assert usage.cost_usd == pytest.approx(0.002)
        assert usage.billed_cost_usd is None

    def test_a_member_that_made_no_call_neither_adds_nor_voids(self) -> None:
        members = [
            _completed(usage=JudgeUsage(calls=1, cost_usd=0.001, billed_cost_usd=0.001)),
            _completed(usage=JudgeUsage()),
        ]
        assert sum_usage(members).billed_cost_usd == pytest.approx(0.001)
        assert sum_usage([_completed(usage=JudgeUsage())]).billed_cost_usd is None
