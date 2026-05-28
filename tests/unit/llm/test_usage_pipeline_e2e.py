"""End-to-end pipeline test for Stage 5 (P7) — Usage wire-through.

Asserts that token + cache + reasoning counters flow cleanly from

    litellm.completion(...)  →  response.usage
                               →  UsageExtractor
                               →  GenerationResult.usage
                               →  Metrics.usage (via Usage.__add__)

across two accumulated calls. This is the Stage 5h "wire-through" test —
it guards the full pipeline, not just isolated components.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import Message, MessageRole, Metrics, ModelConfig

pytestmark = pytest.mark.unit

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _ns(obj: Any) -> Any:
    """Recursively wrap JSON-dict in nested SimpleNamespaces (litellm-shape)."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items() if k != "_comment"})
    if isinstance(obj, list):
        return [_ns(x) for x in obj]
    return obj


def _build_mock_response(fixture_name: str) -> MagicMock:
    """Assemble a ``ModelResponse``-shaped mock from a JSON fixture.

    The fixture carries only the ``usage`` block; we stitch on the minimum
    ``choices[0].message`` surface needed by :meth:`LLMClient.generate`.
    """
    payload = json.loads((FIXTURE_DIR / fixture_name).read_text())
    payload.pop("_comment", None)

    # Message with empty assistant content + no tool_calls + no reasoning.
    message = MagicMock()
    message.content = "Acknowledged."
    message.tool_calls = None
    message.reasoning_content = None
    # Strip any thinking attrs the reasoning_codec might peek at.
    del message.thinking_blocks

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    response.usage = _ns(payload["usage"])
    return response


def _make_client(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    """Build an ``LLMClient`` whose env is safe for offline testing."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-wire-through")
    config = ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7")
    return LLMClient(config)


class TestUsagePipelineEndToEnd:
    """Stage 5h: full wire-through from litellm response to accumulated Metrics."""

    def test_single_call_surfaces_every_usage_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        mock_response = _build_mock_response("anthropic_usage_with_cache.json")

        with patch("tolokaforge.core.llm.client.completion", return_value=mock_response):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0042):
                result = client.generate(
                    system="you are helpful",
                    messages=[Message(role=MessageRole.USER, content="hi")],
                )

        assert isinstance(result.usage, Usage)
        assert result.usage.prompt_tokens == 200
        assert result.usage.completion_tokens == 100
        assert result.usage.reasoning_tokens == 250
        assert result.usage.cached_tokens == 1500
        assert result.usage.cache_creation_input_tokens == 1500
        assert result.usage.cache_read_input_tokens == 800
        assert result.cost_usd == pytest.approx(0.0042)

    def test_two_calls_accumulate_into_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Runner-style accumulation: ``metrics.usage + result.usage`` twice."""
        client = _make_client(monkeypatch)
        response_anthropic = _build_mock_response("anthropic_usage_with_cache.json")
        response_openai = _build_mock_response("openai_gpt5_usage_with_reasoning.json")

        metrics = Metrics()

        with patch(
            "tolokaforge.core.llm.client.completion",
            side_effect=[response_anthropic, response_openai],
        ):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                first = client.generate(
                    system="s", messages=[Message(role=MessageRole.USER, content="a")]
                )
                metrics.usage = metrics.usage + first.usage

                second = client.generate(
                    system="s", messages=[Message(role=MessageRole.USER, content="b")]
                )
                metrics.usage = metrics.usage + second.usage

        # Field-wise sum across the two fixture responses.
        assert metrics.usage.prompt_tokens == 200 + 1600
        assert metrics.usage.completion_tokens == 100 + 420
        assert metrics.usage.reasoning_tokens == 250 + 180
        assert metrics.usage.cached_tokens == 1500 + 320
        assert metrics.usage.cache_creation_input_tokens == 1500  # only Anthropic
        # OpenAI/OpenRouter ``cached_tokens`` now also flows into
        # ``cache_read_input_tokens`` (extractor unifies the OpenAI-canonical
        # cache-read counter with the Anthropic top-level field). 800 from
        # Anthropic + 320 from the OpenAI fixture = 1120.
        assert metrics.usage.cache_read_input_tokens == 800 + 320

    def test_metrics_dump_round_trip_preserves_all_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After accumulation, ``metrics.yaml`` shape survives a YAML-style round trip."""
        client = _make_client(monkeypatch)
        mock_response = _build_mock_response("anthropic_usage_with_cache.json")

        metrics = Metrics()
        with patch("tolokaforge.core.llm.client.completion", return_value=mock_response):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                result = client.generate(
                    system="s", messages=[Message(role=MessageRole.USER, content="a")]
                )
        metrics.usage = metrics.usage + result.usage

        dumped = metrics.model_dump(mode="json")
        assert dumped["usage"]["reasoning_tokens"] == 250
        assert dumped["usage"]["cache_read_input_tokens"] == 800

        # Round-trip back into a fresh Metrics to prove YAML readers recover the full Usage.
        restored = Metrics.model_validate(dumped)
        assert restored.usage.reasoning_tokens == 250
        assert restored.usage.cache_read_input_tokens == 800
        assert restored.usage.cache_creation_input_tokens == 1500
