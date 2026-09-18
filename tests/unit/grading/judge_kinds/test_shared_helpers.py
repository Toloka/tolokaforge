"""Unit tests for ``tolokaforge.core.grading.judge_kinds._shared``.

Locks the two Rule-of-Three extractions (issue #1602) that ``chunked.py``,
``voted.py``, and the forthcoming ``jury.py`` all reuse:
``member_failure_reason`` and ``assert_construction_fields_match``.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge_kinds._shared import (
    assert_construction_fields_match,
    member_failure_reason,
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

    @pytest.mark.parametrize("unit_label", ["sample", "chunk"])
    def test_errored_status_is_a_failure_regardless_of_caller(self, unit_label: str) -> None:
        """The helper's message is unit-agnostic — the caller supplies its own
        wrapping, so the same errored result reads identically whether the
        caller is chunked_rubric or voted_rubric."""
        result = JudgeResult(
            status=JudgeStatus.ERRORED,
            usage=JudgeUsage(),
            reasons=f"{unit_label} blew up",
        )
        reason = member_failure_reason(result, ("c0",))
        assert reason is not None
        assert "status=errored" in reason
        assert f"{unit_label} blew up" in reason

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
            results, ("custom_system_prompt",), kind_label="jury_rubric", unit_noun="member"
        )

    def test_divergent_field_raises_naming_field_and_both_values(self) -> None:
        results = [
            _completed(custom_system_prompt=False),
            _completed(custom_system_prompt=True),
        ]
        with pytest.raises(RuntimeError) as excinfo:
            assert_construction_fields_match(
                results, ("custom_system_prompt",), kind_label="jury_rubric", unit_noun="member"
            )
        message = str(excinfo.value)
        assert "jury_rubric construction-field mismatch across members" in message
        assert "'custom_system_prompt'" in message
        assert "member 0 is False" in message
        assert "member 1 is True" in message

    def test_unit_noun_and_kind_label_are_parametrised_into_the_message(self) -> None:
        results = [_completed(state_diff="a"), _completed(state_diff="b")]
        with pytest.raises(RuntimeError, match=r"chunked_rubric.*chunks.*chunk 0.*chunk 1"):
            assert_construction_fields_match(
                results, ("state_diff",), kind_label="chunked_rubric", unit_noun="chunk"
            )
