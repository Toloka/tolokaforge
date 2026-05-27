"""Red-first test for Stage 5 (P7) — Anthropic cache + reasoning token loss.

Before Stage 5, ``Metrics`` carried only ``tokens_input`` / ``tokens_output``
as two flat integers. The runner accumulated them from
``GenerationResult.token_usage["input"|"output"]``. Anthropic's ``usage``
block *does* populate ``cache_creation_input_tokens``,
``cache_read_input_tokens``, and ``completion_tokens_details.reasoning_tokens``
— but the legacy dict discarded everything except prompt/completion totals,
making it impossible to audit Stage 6 caching efficacy or Claude 4.7
thinking-budget spend.

This test asserts the full :class:`Usage` survives the pipeline. It is
also the proof-of-loss red test: on pre-Stage-5 code it FAILS because
``Metrics.usage`` does not exist (or ``GenerationResult.usage`` is a
dict) — capture that failure in the closing report.

After the fix, the whole test passes without ``xfail``.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import Metrics

pytestmark = pytest.mark.unit


def _anthropic_result() -> GenerationResult:
    """Build a ``GenerationResult`` mirroring a real Anthropic completion."""
    return GenerationResult(
        text="hello",
        tool_calls=[],
        usage=Usage(
            prompt_tokens=200,
            completion_tokens=100,
            reasoning_tokens=250,
            cached_tokens=1500,
            cache_creation_input_tokens=1500,
            cache_read_input_tokens=800,
            provider_raw={"example": "payload"},
        ),
    )


class TestMetricsCacheAndReasoningSurvive:
    """The fix for P7 — full ``Usage`` accumulates into ``Metrics.usage``."""

    def test_single_call_preserves_all_fields(self) -> None:
        metrics = Metrics()
        result = _anthropic_result()

        # The one-line accumulation that replaces the old
        #     self.metrics.tokens_input  += result.token_usage.get("input", 0)
        #     self.metrics.tokens_output += result.token_usage.get("output", 0)
        # logic (runner.py pre-Stage-5).
        metrics.usage = metrics.usage + result.usage

        assert metrics.usage.prompt_tokens == 200
        assert metrics.usage.completion_tokens == 100
        # These four assertions would have been impossible pre-Stage-5 —
        # the fields did not exist on Metrics at all.
        assert metrics.usage.reasoning_tokens == 250
        assert metrics.usage.cached_tokens == 1500
        assert metrics.usage.cache_creation_input_tokens == 1500
        assert metrics.usage.cache_read_input_tokens == 800

    def test_two_calls_accumulate_field_wise(self) -> None:
        metrics = Metrics()
        metrics.usage = metrics.usage + _anthropic_result().usage
        metrics.usage = metrics.usage + _anthropic_result().usage

        assert metrics.usage.prompt_tokens == 400
        assert metrics.usage.completion_tokens == 200
        assert metrics.usage.reasoning_tokens == 500
        assert metrics.usage.cache_creation_input_tokens == 3000
        assert metrics.usage.cache_read_input_tokens == 1600

    def test_metrics_model_dump_serialises_usage_as_dict(self) -> None:
        """``metrics.yaml`` carries a plain dict, not an opaque dataclass."""
        metrics = Metrics()
        metrics.usage = metrics.usage + _anthropic_result().usage

        dumped = metrics.model_dump(mode="json")
        assert isinstance(dumped["usage"], dict)
        usage_dump = dumped["usage"]
        assert usage_dump["prompt_tokens"] == 200
        assert usage_dump["completion_tokens"] == 100
        assert usage_dump["reasoning_tokens"] == 250
        assert usage_dump["cache_creation_input_tokens"] == 1500
        assert usage_dump["cache_read_input_tokens"] == 800
        assert usage_dump["cached_tokens"] == 1500

    def test_metrics_round_trip_from_dict(self) -> None:
        """YAML → dict → Metrics works; the dict form is the stored shape."""
        raw = {
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 100,
                "reasoning_tokens": 250,
                "cached_tokens": 1500,
                "cache_creation_input_tokens": 1500,
                "cache_read_input_tokens": 800,
                "provider_raw": {},
            }
        }
        metrics = Metrics.model_validate(raw)
        assert metrics.usage.prompt_tokens == 200
        assert metrics.usage.cache_read_input_tokens == 800
        assert metrics.usage.reasoning_tokens == 250
