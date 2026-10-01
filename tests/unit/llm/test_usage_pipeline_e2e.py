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


class TestUpstreamProviderIsRecorded:
    """Which machine served a call is part of the call record, not a re-run away.

    A model slug on OpenRouter resolves to one of many upstreams of differing
    quantisation, chosen per request. Without the name on the record, a
    suspect result can only be re-sampled — and re-sampling draws afresh.
    """

    def test_the_serving_upstream_lands_on_the_call(self) -> None:
        from tolokaforge.core.llm.usage import extract_upstream_provider

        class _Response:
            model_extra = {"provider": "CoreWeave"}

        assert extract_upstream_provider(_Response()) == "CoreWeave"

    def test_a_direct_route_names_no_upstream(self) -> None:
        from tolokaforge.core.llm.usage import extract_upstream_provider

        class _Response:
            model_extra: dict[str, object] = {}

        assert extract_upstream_provider(_Response()) is None

    def test_a_response_without_the_attribute_is_not_an_error(self) -> None:
        """Telemetry, not control flow — an unreadable shape reads as absent."""
        from tolokaforge.core.llm.usage import extract_upstream_provider

        assert extract_upstream_provider(object()) is None


class TestReasoningLossIsObservable:
    """Reasoning that never reaches the model back leaves a mark on the trial.

    ``moonshotai/kimi-k2.7-code`` reasoned on turn 1 and never again, because
    its codec replayed nothing and the model copied the reasoning-free history
    it was shown. Every artifact of those runs looked healthy. These two
    observations are what makes that visible without a live probe.
    """

    def _client(self, codec):
        from unittest.mock import MagicMock

        from tolokaforge.core.llm.client import LLMClient
        from tolokaforge.core.models import ModelConfig

        client = LLMClient.__new__(LLMClient)
        client.config = ModelConfig(provider="openrouter", name="openrouter/acme/widget")
        client.provider = "openrouter"
        client.model_name = "openrouter/acme/widget"
        client.capabilities = MagicMock()
        client.capabilities.reasoning_codec = codec
        client.capabilities.cache_policy.apply_messages.side_effect = lambda m: m
        client.logger = MagicMock()
        return client

    def test_a_codec_that_replays_nothing_marks_the_request(self) -> None:
        from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
        from tolokaforge.core.llm.reasoning_codec import OpenAIReasoningCodec
        from tolokaforge.core.models import Message, MessageRole

        client = self._client(OpenAIReasoningCodec())
        history = [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                reasoning=StructuredReasoning(
                    blocks=(ReasoningBlock(type="summary_text", text="I checked the logs."),)
                ),
            )
        ]

        assert client._reasoning_replay_dropped_for(history) is True

    def test_a_codec_that_replays_leaves_no_mark(self) -> None:
        from tolokaforge_models.policies.deepseek import OpenAISummaryReplayReasoningCodec

        from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
        from tolokaforge.core.models import Message, MessageRole

        client = self._client(OpenAISummaryReplayReasoningCodec())
        history = [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                reasoning=StructuredReasoning(
                    blocks=(ReasoningBlock(type="summary_text", text="I checked the logs."),)
                ),
            )
        ]

        converted = client._convert_messages(None, history)

        assert client._reasoning_replay_dropped_for(history) is False
        assert converted[0]["reasoning_details"][0]["text"] == "I checked the logs."

    def test_a_turn_with_no_reasoning_at_all_is_not_a_drop(self) -> None:
        """Most turns of most models; the flag must not fire on them."""
        from tolokaforge.core.llm.reasoning_codec import OpenAIReasoningCodec
        from tolokaforge.core.models import Message, MessageRole

        client = self._client(OpenAIReasoningCodec())

        turn = [Message(role=MessageRole.ASSISTANT, content="done")]

        assert client._reasoning_replay_dropped_for(turn) is False

    def test_billed_reasoning_the_codec_did_not_surface_is_recorded(self) -> None:
        """The predicate, stated directly: charged for thinking, captured none."""
        from tolokaforge.core.llm.client import GenerationResult
        from tolokaforge.core.llm.usage import Usage

        billed = GenerationResult(
            text=" ",
            usage=Usage(reasoning_tokens=42),
            reasoning=None,
            reasoning_billed_not_captured=True,
        )
        clean = GenerationResult(text="ok", usage=Usage(reasoning_tokens=0))

        assert billed.reasoning_billed_not_captured is True
        assert clean.reasoning_billed_not_captured is False


class TestTheReplayObservationRidesTheCallNotTheClient:
    """One ``LLMClient`` serves every concurrent trial in a run.

    ``orchestrator.py`` builds the agent client once and hands the same object
    to every worker in the trial pool, which ``runner.py`` states outright:
    "The ``LLMClient`` is shared across concurrent trials — the identity must
    ride the call, not the client." Per-request state kept on ``self`` is read
    by whichever trial reaches ``_assemble_result`` next, so a trial that
    dropped reasoning can have the fact recorded against a different trial —
    and ``Metrics.reasoning_replay_dropped`` is sticky, so a false positive
    never clears.
    """

    def test_the_client_holds_no_per_request_replay_state(self) -> None:
        from tolokaforge.core.llm.client import LLMClient

        leaked = [n for n in vars(LLMClient).get("__annotations__", {}) if "replay_dropped" in n]

        assert not leaked, f"{leaked} is per-request state on a shared client"

    def test_two_interleaved_histories_each_get_their_own_answer(self) -> None:
        """The interleaving that the old instance flag got wrong."""
        from unittest.mock import MagicMock

        from tolokaforge.core.llm.client import LLMClient
        from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
        from tolokaforge.core.llm.reasoning_codec import OpenAIReasoningCodec
        from tolokaforge.core.models import Message, MessageRole, ModelConfig

        client = LLMClient.__new__(LLMClient)
        client.config = ModelConfig(provider="openrouter", name="openrouter/acme/widget")
        client.provider = "openrouter"
        client.model_name = "openrouter/acme/widget"
        client.capabilities = MagicMock()
        client.capabilities.reasoning_codec = OpenAIReasoningCodec()
        client.logger = MagicMock()

        with_reasoning = [
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                reasoning=StructuredReasoning(
                    blocks=(ReasoningBlock(type="summary_text", text="I read the log."),)
                ),
            )
        ]
        without = [Message(role=MessageRole.ASSISTANT, content="done")]

        # trial A asks, trial B asks before A reads its answer
        a = client._reasoning_replay_dropped_for(with_reasoning)
        b = client._reasoning_replay_dropped_for(without)

        assert a is True, "the history that dropped reasoning must say so"
        assert b is False, "the history that dropped none must not inherit A's answer"
