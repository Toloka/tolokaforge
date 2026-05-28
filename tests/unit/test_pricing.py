"""Unit tests for tolokaforge.core.pricing."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tolokaforge.core.pricing import (
    MODEL_PRICING,
    estimate_cost,
    get_pricing_info,
    list_supported_models,
    normalize_model_name,
    reload_pricing,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Tests for normalize_model_name
# ---------------------------------------------------------------------------


class TestNormalizeModelName:
    """Test model name normalization."""

    def test_already_prefixed(self):
        assert normalize_model_name("openai/gpt-4o") == "openai/gpt-4o"

    def test_openrouter_prefix_stripped(self):
        """OpenRouter routing prefix should be stripped before normalization."""
        assert normalize_model_name("openrouter/openai/gpt-5.4") == "openai/gpt-5.4"
        assert (
            normalize_model_name("openrouter/anthropic/claude-sonnet-4.6")
            == "anthropic/claude-sonnet-4.6"
        )
        assert (
            normalize_model_name("openrouter/google/gemini-3.1-pro-preview")
            == "google/gemini-3.1-pro-preview"
        )
        assert normalize_model_name("openrouter/moonshotai/kimi-k2.6") == "moonshotai/kimi-k2.6"

    def test_gpt_prefix(self):
        assert normalize_model_name("gpt-5.4") == "openai/gpt-5.4"

    def test_o1_prefix(self):
        assert normalize_model_name("o1-mini") == "openai/o1-mini"

    def test_o3_prefix(self):
        assert normalize_model_name("o3-pro") == "openai/o3-pro"

    def test_claude_prefix(self):
        assert normalize_model_name("claude-opus-4.6") == "anthropic/claude-opus-4.6"
        assert normalize_model_name("claude-sonnet-4.6") == "anthropic/claude-sonnet-4.6"

    def test_gemini_prefix(self):
        assert normalize_model_name("gemini-3.0-flash") == "google/gemini-3.0-flash"
        assert normalize_model_name("gemini-3.1-pro") == "google/gemini-3.1-pro"

    def test_gemma_prefix(self):
        assert normalize_model_name("gemma-3-27b-it") == "google/gemma-3-27b-it"

    def test_grok_prefix(self):
        assert normalize_model_name("grok-4.2") == "x-ai/grok-4.2"

    def test_minimax_prefix(self):
        """MiniMax models should be normalized to minimax/ provider."""
        assert normalize_model_name("minimax-m2.7") == "minimax/minimax-m2.7"

    def test_kimi_prefix(self):
        """Kimi models should be normalized to moonshot/ provider."""
        assert normalize_model_name("kimi-k2.5") == "moonshot/kimi-k2.5"

    def test_deepseek_prefix(self):
        assert normalize_model_name("deepseek-r1") == "deepseek/deepseek-r1"

    def test_nova_prefix(self):
        assert normalize_model_name("nova-2-lite") == "nova/nova-2-lite"

    def test_mistral_variants(self):
        assert normalize_model_name("mistral-large") == "mistralai/mistral-large"
        assert normalize_model_name("codestral-2508") == "mistralai/codestral-2508"
        assert normalize_model_name("devstral-small") == "mistralai/devstral-small"
        assert normalize_model_name("magistral-medium-2506") == "mistralai/magistral-medium-2506"

    def test_llama_prefix(self):
        assert normalize_model_name("llama-3.3-70b-instruct") == "meta-llama/llama-3.3-70b-instruct"

    def test_qwen_prefix(self):
        assert normalize_model_name("qwen-max") == "qwen/qwen-max"

    def test_unknown_model_returned_as_is(self):
        assert normalize_model_name("some-unknown-model") == "some-unknown-model"


# ---------------------------------------------------------------------------
# Tests for estimate_cost
# ---------------------------------------------------------------------------


class TestEstimateCost:
    """Test cost estimation."""

    def test_known_model_returns_float(self, tmp_path):
        """Known model should return a float cost.

        Uses hermetic pricing fixture so the test is stable across pricing updates.
        """
        fixture = tmp_path / "pricing.json"
        fixture.write_text(
            json.dumps(
                {"models": {"test/sample-model": {"input": 0.50, "output": 1.00}}}
            )
        )
        reload_pricing(fixture)
        try:
            cost = estimate_cost(
                "test/sample-model", input_tokens=1_000_000, output_tokens=1_000_000
            )
            assert cost is not None
            assert isinstance(cost, float)
            # $0.50/M input + $1.00/M output = $1.50
            assert cost == pytest.approx(1.50, abs=0.01)
        finally:
            reload_pricing()

    def test_known_model_with_normalization(self, tmp_path):
        """Model name normalization should allow short names.

        Uses hermetic pricing fixture so the test is stable across pricing updates.
        """
        fixture = tmp_path / "pricing.json"
        fixture.write_text(
            json.dumps(
                {"models": {"minimax/minimax-test": {"input": 0.50, "output": 1.00}}}
            )
        )
        reload_pricing(fixture)
        try:
            # "minimax-test" should normalize to "minimax/minimax-test"
            cost = estimate_cost("minimax-test", input_tokens=1_000_000, output_tokens=1_000_000)
            assert cost is not None
            assert cost == pytest.approx(1.50, abs=0.01)
        finally:
            reload_pricing()

    def test_unknown_model_returns_none(self):
        """Unknown model should return None, not a fallback value."""
        cost = estimate_cost("unknown-model-xyz", input_tokens=1000, output_tokens=500)
        assert cost is None

    def test_zero_tokens(self):
        """Zero tokens should return zero cost."""
        cost = estimate_cost("openai/gpt-4o", input_tokens=0, output_tokens=0)
        assert cost is not None
        assert cost == 0.0

    def test_claude_sonnet_dash_variants_priced(self):
        """Dash- and dot-variant model IDs both resolve to a price.

        Some clients normalise periods to dashes (and vice versa) when
        composing OpenRouter model IDs. The pricing table must answer for
        both spellings so we don't silently drop costs from analytics.
        """
        for model_id in (
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-sonnet-4-6",
            "anthropic/claude-sonnet-4.5",
            "anthropic/claude-sonnet-4-5",
        ):
            cost = estimate_cost(model_id, input_tokens=1_000_000, output_tokens=0)
            assert cost is not None, f"Model {model_id} has no pricing entry"
            # All claude-sonnet variants are priced at $3 / 1M input.
            assert cost == pytest.approx(3.0, abs=0.01)

    def test_claude_opus_46_has_pricing(self):
        """claude-opus-4.6 should have correct pricing (not DEFAULT)."""
        cost = estimate_cost("claude-opus-4.6", input_tokens=1_000_000, output_tokens=0)
        assert cost is not None
        # Starting at $5/M input
        assert cost == pytest.approx(5.0, abs=0.01)

    def test_cache_read_discount_applies(self, tmp_path):
        """Cache reads should be charged at the configured cache_read rate.

        Uses a hermetic pricing fixture so the assertion isn't coupled to
        whatever rates ``pricing-updater`` pulled most recently. The
        formula is what's under test, not the published rates.
        """
        fixture = tmp_path / "pricing.json"
        fixture.write_text(
            json.dumps(
                {
                    "models": {
                        "anthropic/canon-opus": {
                            "input": 5.0,
                            "output": 25.0,
                            "cache_read": 0.5,
                            "cache_write": 5.0,
                        }
                    }
                }
            )
        )
        reload_pricing(fixture)
        try:
            cost = estimate_cost(
                "anthropic/canon-opus",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_read_input_tokens=200_000,
            )
        finally:
            reload_pricing()
        assert cost is not None
        # Fresh tokens: 800k * 5.0 = 4.0; cached tokens: 200k * 0.5 = 0.1
        assert cost == pytest.approx(4.1, abs=0.001)

    def test_reasoning_tokens_billed_at_output_rate(self, tmp_path):
        """Reasoning tokens must be billed at the output rate.

        Every provider that exposes ``reasoning_tokens`` (OpenAI o-series /
        GPT-5, Anthropic thinking, etc.) charges them at the same per-token
        rate as ``output_tokens``. Pre-fix, ``estimate_cost`` dropped them
        silently, understating cost for reasoning-effort comparisons (the
        bug surfaced when gpt-5.5 medium vs xhigh reports showed an 11% gap
        instead of the real ~21%).
        """
        fixture = tmp_path / "pricing.json"
        fixture.write_text(
            json.dumps({"models": {"test/reasoner": {"input": 1.0, "output": 10.0}}})
        )
        reload_pricing(fixture)
        try:
            cost = estimate_cost(
                "test/reasoner",
                input_tokens=0,
                output_tokens=500_000,
                reasoning_tokens=500_000,
            )
        finally:
            reload_pricing()
        assert cost is not None
        # (500k completion + 500k reasoning) * $10 = $10
        assert cost == pytest.approx(10.0, abs=0.001)

    def test_cache_creation_uses_cache_write_rate(self, tmp_path):
        """Cache writes should be charged using the configured cache_write rate.

        Hermetic fixture (see ``test_cache_read_discount_applies``).
        """
        fixture = tmp_path / "pricing.json"
        fixture.write_text(
            json.dumps(
                {
                    "models": {
                        "anthropic/canon-opus": {
                            "input": 5.0,
                            "output": 25.0,
                            "cache_read": 0.5,
                            "cache_write": 5.0,
                        }
                    }
                }
            )
        )
        reload_pricing(fixture)
        try:
            cost = estimate_cost(
                "anthropic/canon-opus",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_input_tokens=100_000,
            )
        finally:
            reload_pricing()
        assert cost is not None
        # Fresh tokens: 900k * 5.0 = 4.5; cache_write: 100k * 5.0 = 0.5
        assert cost == pytest.approx(5.0, abs=0.001)

    def test_inconsistent_cache_totals_warn_and_clamp(self, caplog):
        """Cache totals exceeding ``input_tokens`` is a usage-accounting bug.

        We must log loudly (per AGENTS.md "surface failures") rather than
        silently swallow the inconsistency. The fresh-input clamp to zero
        keeps cost bounded; the warning is the contract.
        """
        with caplog.at_level("WARNING", logger="tolokaforge.core.pricing"):
            cost = estimate_cost(
                "claude-opus-4.6",
                input_tokens=100_000,
                output_tokens=0,
                cache_read_input_tokens=80_000,
                cache_creation_input_tokens=50_000,  # 80k+50k > 100k
            )
        assert cost is not None
        messages = [rec.message for rec in caplog.records]
        assert any("inconsistent_cache_usage" in m for m in messages), messages

    def test_all_benchmark_models_have_pricing(self):
        """All 10 benchmark models should have pricing entries."""
        benchmark_models = [
            "claude-opus-4.6",
            "claude-sonnet-4.6",
            "gpt-5.4",
            "gpt-5.4-xhigh",
            "gemini-3.0-flash",
            "gemini-3.1-pro",
            "grok-4.2",
            "kimi-k2.5",
            "minimax-m2.7",
            "nova-2-lite",
        ]
        for model in benchmark_models:
            cost = estimate_cost(model, input_tokens=1_000_000, output_tokens=0)
            assert cost is not None, f"Model {model} has no pricing entry"
            assert cost > 0, f"Model {model} has zero input cost"


# ---------------------------------------------------------------------------
# Tests for JSON loading
# ---------------------------------------------------------------------------


class TestPricingDataLoading:
    """Test that pricing data loads from JSON correctly."""

    def test_model_pricing_is_populated(self):
        """MODEL_PRICING should have entries loaded from pricing.json."""
        assert len(MODEL_PRICING) > 0

    def test_pricing_json_exists(self):
        """The bundled pricing.json file should exist."""
        pricing_path = (
            Path(__file__).resolve().parents[2] / "tolokaforge" / "core" / "data" / "pricing.json"
        )
        assert pricing_path.exists(), f"pricing.json not found at {pricing_path}"

    def test_reload_from_custom_file(self):
        """reload_pricing should load from a custom file."""
        custom_data = {
            "models": {
                "test/model-a": {"input": 1.0, "output": 2.0},
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(custom_data, f)
            custom_path = Path(f.name)

        try:
            reload_pricing(custom_path)
            cost = estimate_cost("test/model-a", input_tokens=1_000_000, output_tokens=1_000_000)
            assert cost is not None
            assert cost == pytest.approx(3.0, abs=0.01)
        finally:
            # Restore original pricing
            reload_pricing()
            custom_path.unlink()

    def test_reload_missing_file_returns_empty(self):
        """reload_pricing with a missing file should result in empty table."""
        reload_pricing(Path("/tmp/nonexistent_pricing.json"))
        assert len(MODEL_PRICING) == 0
        # Restore
        reload_pricing()
        assert len(MODEL_PRICING) > 0


# ---------------------------------------------------------------------------
# Tests for query helpers
# ---------------------------------------------------------------------------


class TestQueryHelpers:
    """Test pricing query helpers."""

    def test_get_pricing_info_known(self):
        info = get_pricing_info("openai/gpt-4o")
        assert info is not None
        assert "input" in info
        assert "output" in info
        assert info["input"] > 0

    def test_get_pricing_info_unknown(self):
        info = get_pricing_info("unknown/model")
        assert info is None

    def test_list_supported_models(self):
        models = list_supported_models()
        assert len(models) > 0
        # Should be a copy, not the original
        models["fake/model"] = {"input": 0, "output": 0}
        assert "fake/model" not in MODEL_PRICING
