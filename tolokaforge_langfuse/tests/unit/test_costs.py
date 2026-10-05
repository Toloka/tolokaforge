"""The cost a generation shows: the charge the provider stated, else the eval's own figure, and
which (``cost_basis``).

``costs`` is engine-free and reads bundle mappings; the offline connector keeps a copy held to the
same rules by the shared golden.
"""

from __future__ import annotations

import pytest
from tolokaforge_langfuse.costs import (
    COST_BASIS_BILLED,
    COST_BASIS_EVAL,
    COST_BASIS_LIST,
    COST_BASIS_LITELLM,
    NONE,
    call_cost,
    call_record,
    judge_cost,
)

from tolokaforge.core.llm.usage import ProviderRawCall

pytestmark = pytest.mark.unit


def _call(**fields) -> dict:
    return {"prompt_tokens": 100, "completion_tokens": 10, **fields}


class TestCallCost:
    def test_a_stated_charge_is_the_cost(self) -> None:
        # a BYOK call: the upstream's bill is above litellm's estimate
        call = _call(cost_usd=0.0266895, cost_source="litellm", billed_cost_usd=0.027022)
        assert call_cost(call) == (0.027022, COST_BASIS_BILLED)

    def test_a_zero_charge_is_a_charge(self) -> None:
        call = _call(cost_usd=0.001, cost_source="litellm", billed_cost_usd=0.0)
        assert call_cost(call) == (0.0, COST_BASIS_BILLED)

    @pytest.mark.parametrize(
        ("source", "basis"),
        [
            ("litellm", COST_BASIS_LITELLM),
            ("local", COST_BASIS_LIST),
            ("provider", COST_BASIS_EVAL),  # a source the rules do not name
            (None, COST_BASIS_EVAL),
        ],
    )
    def test_without_a_charge_the_eval_figure_is_the_cost_and_names_its_source(
        self, source: str | None, basis: str
    ) -> None:
        assert call_cost(_call(cost_usd=0.002, cost_source=source)) == (0.002, basis)

    def test_a_call_written_before_the_field_keeps_its_cost(self) -> None:
        # what every bundle recorded before billed_cost_usd existed
        assert call_cost(_call(cost_usd=0.0094118, cost_source="litellm")) == (
            0.0094118,
            COST_BASIS_LITELLM,
        )

    def test_a_call_without_any_cost_has_none(self) -> None:
        assert call_cost(_call(cost_usd=None, cost_source="unknown")) == (None, NONE)

    @pytest.mark.parametrize("bad", ["0.01", True, [0.01]])
    def test_a_value_that_is_no_number_is_no_figure(self, bad: object) -> None:
        assert call_cost(_call(cost_usd=bad, billed_cost_usd=bad, cost_source="litellm")) == (
            None,
            NONE,
        )


class TestJudgeCost:
    def test_the_judges_stated_charge_is_the_cost(self) -> None:
        usage = {"calls": 3, "cost_usd": 0.0142, "billed_cost_usd": 0.0145}
        assert judge_cost(usage) == (0.0145, COST_BASIS_BILLED)

    def test_without_one_the_eval_figure_is_the_cost_its_source_unrecorded(self) -> None:
        assert judge_cost({"calls": 3, "cost_usd": 0.0142}) == (0.0142, COST_BASIS_EVAL)

    def test_a_judge_usage_without_any_cost_has_none(self) -> None:
        assert judge_cost({"calls": 1}) == (None, NONE)


def test_an_engine_call_record_reads_like_its_bundle_row() -> None:
    call = ProviderRawCall(
        prompt_tokens=10, cost_usd=0.002, cost_source="litellm", billed_cost_usd=0.0021
    )
    assert call_record(call) == {
        "cost_usd": 0.002,
        "cost_source": "litellm",
        "billed_cost_usd": 0.0021,
    }
    assert call_cost(call_record(call)) == (0.0021, COST_BASIS_BILLED)
