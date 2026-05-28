"""Unit tests for :class:`UsageExtractor` against captured response shapes.

Covers:
- Full response with all fields populated
- Missing ``usage`` block
- Missing ``*_details`` sub-objects
- Raw Anthropic response fixture with ``cache_*_input_tokens``
- JSON fixtures mirroring real litellm ``ModelResponse.usage`` shapes
- :meth:`Usage.__add__` field-wise accumulation (Stage 5)
- :class:`tolokaforge.core.llm.client.GenerationResult` default / mock construction
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage, UsageExtractor

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_response(name: str) -> SimpleNamespace:
    """Load a JSON fixture and wrap it in nested ``SimpleNamespace`` objects.

    Mirrors litellm's ``ModelResponse`` attribute-access shape so the same
    ``UsageExtractor`` code path runs in tests and in production.
    """
    payload = json.loads((FIXTURE_DIR / name).read_text())

    def _ns(obj: Any) -> Any:
        if isinstance(obj, dict):
            return SimpleNamespace(**{k: _ns(v) for k, v in obj.items() if k != "_comment"})
        if isinstance(obj, list):
            return [_ns(x) for x in obj]
        return obj

    return _ns(payload)


def _full_usage_response() -> SimpleNamespace:
    """Shape matching a fully populated litellm ModelResponse.usage."""
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=1200,
            completion_tokens=350,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=150),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=80),
        )
    )


def _anthropic_response() -> SimpleNamespace:
    """Anthropic-shaped response with cache_* token fields on the usage object."""
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=18_000,
            completion_tokens=500,
            cache_creation_input_tokens=17_500,
            cache_read_input_tokens=17_500,
            prompt_tokens_details=SimpleNamespace(cached_tokens=17_500),
            completion_tokens_details=None,
        )
    )


class TestUsageExtractor:
    def test_full_response(self) -> None:
        extractor = UsageExtractor()
        usage = extractor.extract(_full_usage_response())
        assert usage.prompt_tokens == 1200
        assert usage.completion_tokens == 350
        assert usage.cached_tokens == 150
        assert usage.reasoning_tokens == 80
        assert usage.cache_creation_input_tokens == 0
        # ``cached_tokens`` is the OpenAI-canonical cache-read counter; the
        # extractor mirrors it into ``cache_read_input_tokens`` so downstream
        # observability is routing-agnostic.
        assert usage.cache_read_input_tokens == 150

    def test_missing_usage_returns_zero_usage(self) -> None:
        extractor = UsageExtractor()
        usage = extractor.extract(SimpleNamespace())
        assert usage == Usage()

    def test_none_usage_returns_zero_usage(self) -> None:
        extractor = UsageExtractor()
        assert extractor.extract(SimpleNamespace(usage=None)) == Usage()

    def test_missing_details_defaults_to_zero(self) -> None:
        """No ``prompt_tokens_details`` / ``completion_tokens_details`` → 0."""
        extractor = UsageExtractor()
        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=500,
                completion_tokens=100,
            )
        )
        usage = extractor.extract(response)
        assert usage.prompt_tokens == 500
        assert usage.completion_tokens == 100
        assert usage.cached_tokens == 0
        assert usage.reasoning_tokens == 0

    def test_anthropic_cache_fields(self) -> None:
        extractor = UsageExtractor()
        usage = extractor.extract(_anthropic_response())
        assert usage.prompt_tokens == 18_000
        assert usage.cache_creation_input_tokens == 17_500
        assert usage.cache_read_input_tokens == 17_500
        assert usage.cached_tokens == 17_500

    def test_dict_usage_is_also_handled(self) -> None:
        """A plain dict response falls through the same extraction paths."""
        extractor = UsageExtractor()
        response = {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
                "cache_creation_input_tokens": 1,
                "cache_read_input_tokens": 4,
            }
        }
        usage = extractor.extract(response)
        assert usage.prompt_tokens == 10
        assert usage.completion_tokens == 5
        assert usage.cached_tokens == 3
        assert usage.reasoning_tokens == 2
        assert usage.cache_creation_input_tokens == 1
        assert usage.cache_read_input_tokens == 4

    def test_provider_raw_non_empty_after_successful_extract(self) -> None:
        extractor = UsageExtractor()
        usage = extractor.extract(_full_usage_response())
        assert usage.provider_raw  # non-empty dict dumped from the usage object


# ---------------------------------------------------------------------------
# Fixture-driven tests against real provider shapes
# ---------------------------------------------------------------------------


class TestUsageExtractorFixtures:
    """Verify :class:`UsageExtractor` against JSON snapshots of real responses."""

    def test_anthropic_usage_with_cache(self) -> None:
        usage = UsageExtractor().extract(_load_response("anthropic_usage_with_cache.json"))
        assert usage.prompt_tokens == 200
        assert usage.completion_tokens == 100
        assert usage.reasoning_tokens == 250
        assert usage.cached_tokens == 1500
        assert usage.cache_creation_input_tokens == 1500
        assert usage.cache_read_input_tokens == 800

    def test_openai_gpt5_usage_with_reasoning(self) -> None:
        usage = UsageExtractor().extract(_load_response("openai_gpt5_usage_with_reasoning.json"))
        assert usage.prompt_tokens == 1600
        assert usage.completion_tokens == 420
        assert usage.reasoning_tokens == 180
        assert usage.cached_tokens == 320
        # OpenAI does not populate the Anthropic-specific ``cache_creation``
        # field. ``cache_read_input_tokens`` mirrors ``cached_tokens`` so
        # downstream observability is provider-agnostic.
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 320

    def test_minimal_usage(self) -> None:
        usage = UsageExtractor().extract(_load_response("minimal_usage.json"))
        assert usage.prompt_tokens == 42
        assert usage.completion_tokens == 17
        # No *_details sub-objects → every derived counter defaults to zero.
        assert usage.reasoning_tokens == 0
        assert usage.cached_tokens == 0
        assert usage.cache_creation_input_tokens == 0
        assert usage.cache_read_input_tokens == 0

    def test_openrouter_anthropic_usage_surfaces_cache_write_tokens(self) -> None:
        """OpenRouter-routed Anthropic stores cache writes under
        ``prompt_tokens_details.cache_write_tokens`` while direct Anthropic uses the
        top-level ``cache_creation_input_tokens``. UsageExtractor must read both.

        Cold-start turn: only writes happened, so ``cached_tokens`` (the OpenAI-shape
        cache-read counter) is zero.
        """
        usage = UsageExtractor().extract(_load_response("openrouter_anthropic_usage.json"))
        assert usage.cache_creation_input_tokens == 8234, (
            "OpenRouter cache_write_tokens must land in Usage.cache_creation_input_tokens; "
            f"got {usage.cache_creation_input_tokens}"
        )
        assert usage.cache_read_input_tokens == 0
        assert usage.reasoning_tokens == 1200
        assert usage.cached_tokens == 0
        assert usage.prompt_tokens == 1800
        assert usage.completion_tokens == 150

    def test_openrouter_anthropic_real_shape_surfaces_cached_tokens_as_cache_read(self) -> None:
        """Real OpenRouter shape: cache reads land under ``prompt_tokens_details.cached_tokens``
        (singular, OpenAI-canonical). There is no ``cache_read_tokens`` key in production.

        UsageExtractor must therefore fall back to ``cached_tokens`` for
        ``cache_read_input_tokens`` when the top-level Anthropic field is zero.
        Live probe of openrouter/anthropic/claude-opus-4.7 confirms this is the
        only field carrying cache-hit information for OpenRouter-routed calls.
        """
        usage = UsageExtractor().extract(_load_response("openrouter_anthropic_usage_real.json"))
        assert usage.cache_read_input_tokens == 14031, (
            "OpenRouter cached_tokens must surface as Usage.cache_read_input_tokens; "
            f"got {usage.cache_read_input_tokens}"
        )
        assert usage.cached_tokens == 14031
        assert usage.cache_creation_input_tokens == 0
        assert usage.prompt_tokens == 17377

    def test_openrouter_anthropic_top_level_wins_when_nonzero(self) -> None:
        """Top-level ``cache_creation_input_tokens`` must win when populated, even
        if nested ``prompt_tokens_details.cache_write_tokens`` disagrees. Protects
        the direct-Anthropic path from accidental override by the OpenRouter fallback."""
        response = {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 100,
                "cache_creation_input_tokens": 4000,
                "cache_read_input_tokens": 2000,
                "prompt_tokens_details": {
                    "cached_tokens": 9999,  # should be ignored — top-level wins
                    "cache_write_tokens": 9999,  # should be ignored
                },
            }
        }
        usage = UsageExtractor().extract(response)
        assert usage.cache_creation_input_tokens == 4000
        assert usage.cache_read_input_tokens == 2000


# ---------------------------------------------------------------------------
# Usage.__add__ — field-wise accumulation (Stage 5d)
# ---------------------------------------------------------------------------


class TestUsageAdd:
    """``Usage + Usage`` sums every field; ``provider_raw`` follows latest-wins."""

    def test_simple_sum(self) -> None:
        left = Usage(prompt_tokens=10)
        right = Usage(prompt_tokens=5, cache_read_input_tokens=3)
        total = left + right
        assert total.prompt_tokens == 15
        assert total.cache_read_input_tokens == 3
        assert total.completion_tokens == 0  # untouched on both operands

    def test_full_field_sum(self) -> None:
        left = Usage(
            prompt_tokens=100,
            completion_tokens=50,
            reasoning_tokens=20,
            cached_tokens=10,
            cache_creation_input_tokens=5,
            cache_read_input_tokens=5,
        )
        right = Usage(
            prompt_tokens=200,
            completion_tokens=80,
            reasoning_tokens=40,
            cached_tokens=30,
            cache_creation_input_tokens=15,
            cache_read_input_tokens=25,
        )
        total = left + right
        assert total.prompt_tokens == 300
        assert total.completion_tokens == 130
        assert total.reasoning_tokens == 60
        assert total.cached_tokens == 40
        assert total.cache_creation_input_tokens == 20
        assert total.cache_read_input_tokens == 30

    def test_provider_raw_latest_wins(self) -> None:
        """Per the Usage docstring: __add__ keeps ``other.provider_raw``."""
        left = Usage(provider_raw={"turn": 1, "stale": True})
        right = Usage(provider_raw={"turn": 2, "fresh": True})
        total = left + right
        assert total.provider_raw == {"turn": 2, "fresh": True}

    def test_provider_raw_empty_right_clears(self) -> None:
        """Empty ``other.provider_raw`` yields an empty dict, not left's dict."""
        left = Usage(provider_raw={"turn": 1})
        right = Usage()
        total = left + right
        assert total.provider_raw == {}

    def test_add_non_usage_returns_not_implemented(self) -> None:
        """Adding something that is not a :class:`Usage` raises ``TypeError``."""
        usage = Usage(prompt_tokens=10)
        with pytest.raises(TypeError):
            _ = usage + 5  # type: ignore[operator]

    def test_zero_usage_is_identity(self) -> None:
        """``Usage() + u == u`` (ignoring provider_raw semantics)."""
        u = Usage(prompt_tokens=7, completion_tokens=3)
        result = Usage() + u
        assert result.prompt_tokens == 7
        assert result.completion_tokens == 3


# ---------------------------------------------------------------------------
# GenerationResult — Stage 5b migration: usage: Usage (no dict)
# ---------------------------------------------------------------------------


class TestGenerationResultUsageField:
    def test_default_usage_is_empty_usage_instance(self) -> None:
        result = GenerationResult(text="hello")
        assert isinstance(result.usage, Usage)
        assert result.usage == Usage()

    def test_explicit_usage_preserved(self) -> None:
        usage = Usage(prompt_tokens=42, completion_tokens=7, reasoning_tokens=3)
        result = GenerationResult(text="hi", usage=usage)
        assert result.usage is usage

    def test_none_usage_coerced_to_empty(self) -> None:
        """Passing ``usage=None`` explicitly still yields a valid :class:`Usage`."""
        result = GenerationResult(text="", usage=None)
        assert isinstance(result.usage, Usage)
        assert result.usage.prompt_tokens == 0
