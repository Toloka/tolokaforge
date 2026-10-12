"""The charge a response states, beside the eval's own cost.

``ProviderRawCall.billed_cost_usd`` is read off the response's usage block
(:func:`extract_billed_cost`) and never touches ``cost_usd``. See
docs/LLM_LAYER.md § "Billed cost".
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from tolokaforge.core.llm.usage import (
    ProviderRawCall,
    Usage,
    UsageExtractor,
    extract_billed_cost,
    sum_known,
)
from tolokaforge.core.models import Metrics

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _usage_dict(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text())["usage"]


def _ns(obj: Any) -> Any:
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    return obj


class TestExtractBilledCost:
    def test_an_openrouter_block_states_the_charge(self) -> None:
        # recorded: litellm's response_cost carried the same figure
        assert extract_billed_cost(_ns(_usage_dict("openrouter_usage_billed.json"))) == 7.56e-07

    def test_a_byok_call_cost_the_fee_plus_the_upstreams_bill(self) -> None:
        usage = _usage_dict("openrouter_usage_byok.json")
        assert extract_billed_cost(_ns(usage)) == pytest.approx(0.027)
        usage["cost"] = 0.00135  # a fee outside the free allowance is paid on top
        assert extract_billed_cost(_ns(usage)) == pytest.approx(0.02835)

    def test_a_byok_block_without_the_upstreams_bill_states_no_complete_charge(self) -> None:
        usage = _usage_dict("openrouter_usage_byok.json")
        del usage["cost_details"]["upstream_inference_cost"]
        assert extract_billed_cost(_ns(usage)) is None

    def test_a_free_call_is_billed_zero_not_unknown(self) -> None:
        usage = _usage_dict("openrouter_usage_billed.json")
        usage["cost"] = 0
        assert extract_billed_cost(_ns(usage)) == 0.0

    def test_a_providers_own_api_states_no_charge(self) -> None:
        anthropic = json.loads((FIXTURE_DIR / "anthropic_usage_with_cache.json").read_text())
        assert extract_billed_cost(_ns(anthropic["usage"])) is None

    def test_a_dict_block_reads_like_the_object(self) -> None:
        assert extract_billed_cost(_usage_dict("openrouter_usage_byok.json")) == pytest.approx(
            0.027
        )

    @pytest.mark.parametrize("cost", ["0.01", True, -0.01, math.nan, math.inf, None, [0.01]])
    def test_a_malformed_charge_is_no_charge(self, cost: object) -> None:
        assert extract_billed_cost(SimpleNamespace(cost=cost, is_byok=False)) is None

    def test_a_truthy_non_bool_byok_flag_is_not_byok(self) -> None:
        # only a literal true adds the upstream's bill
        block = SimpleNamespace(
            cost=0.5, is_byok="yes", cost_details={"upstream_inference_cost": 9.0}
        )
        assert extract_billed_cost(block) == 0.5

    def test_a_test_doubles_auto_attributes_are_no_charge(self) -> None:
        # MagicMock answers every attribute, and float(MagicMock()) is 1.0
        assert extract_billed_cost(MagicMock()) is None


class TestTheCallRecord:
    def test_the_record_carries_the_charge_beside_the_callers_cost(self) -> None:
        response = SimpleNamespace(usage=_ns(_usage_dict("openrouter_usage_billed.json")))
        usage = UsageExtractor().extract(response, cost_usd=1.25e-06, cost_source="local")
        (call,) = usage.calls
        assert call.billed_cost_usd == 7.56e-07
        # the eval's own figure is what the caller computed, untouched
        assert (call.cost_usd, call.cost_source) == (1.25e-06, "local")

    def test_a_route_without_a_charge_records_none(self) -> None:
        anthropic = json.loads((FIXTURE_DIR / "anthropic_usage_with_cache.json").read_text())
        usage = UsageExtractor().extract(_ns(anthropic), cost_usd=0.01, cost_source="litellm")
        (call,) = usage.calls
        assert call.billed_cost_usd is None
        assert call.cost_usd == 0.01


class TestSumKnown:
    def test_sums_when_every_amount_is_known(self) -> None:
        assert sum_known([0.25, 0.5, 0.0]) == pytest.approx(0.75)

    def test_one_unknown_amount_makes_the_sum_unknown(self) -> None:
        assert sum_known([0.25, None, 0.5]) is None

    def test_no_amounts_is_unknown_not_zero(self) -> None:
        assert sum_known([]) is None


class TestBundleCompatibility:
    """``metrics.yaml`` carries the charge per call; a bundle from before the field
    still loads, with the charge unknown."""

    def test_the_fields_round_trip_through_metrics_yaml(self) -> None:
        call = ProviderRawCall(
            prompt_tokens=100,
            completion_tokens=10,
            cache_read_input_tokens=80,
            cost_usd=0.002,
            cost_source="litellm",
            billed_cost_usd=0.0021,
        )
        metrics = Metrics(usage=Usage(calls=(call,)))
        dumped = yaml.safe_load(yaml.safe_dump(metrics.model_dump(mode="json")))
        assert dumped["usage"]["calls"][0]["billed_cost_usd"] == 0.0021
        assert Metrics.model_validate(dumped).usage.calls == (call,)

    def test_a_call_written_before_the_field_loads_with_the_charge_unknown(self) -> None:
        old_call = {
            "prompt_tokens": 9597,
            "completion_tokens": 121,
            "cached_tokens": 9472,
            "reasoning_tokens": 32,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 9472,
            "cost_usd": 0.0001980665,
            "cost_source": "litellm",
            "latency_s": 0.99,
            "gateway_route": None,
            "gateway_route_kind": None,
            "openrouter_generation_id": "gen-1",
        }
        (call,) = Metrics.model_validate({"usage": {"calls": [old_call]}}).usage.calls
        assert call.billed_cost_usd is None
        assert call.cost_usd == 0.0001980665
