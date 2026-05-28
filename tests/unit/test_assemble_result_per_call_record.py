"""Per-call ``ProviderRawCall`` population in ``LLMClient._assemble_result``.

Pins the contract that every ``GenerationResult`` carries exactly one
``ProviderRawCall`` in ``result.usage.calls``, and that the call's
``cost_source`` faithfully records which pricing path filled
``cost_usd``:

* ``"litellm"`` — populated from ``response._hidden_params['response_cost']``
  or :func:`litellm.completion_cost` (provider-authoritative, cache-aware).
* ``"local"`` — populated from the bundled :data:`MODEL_PRICING` table
  when both litellm paths fail.
* ``"unknown"`` — neither could price; ``cost_usd is None``.

Companion to :mod:`tests.canonical.test_cost_extraction_canon`, which
pins the cost values themselves; this file pins the **provenance**
travelled with each call record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.core.pricing import reload_pricing

pytestmark = pytest.mark.unit


_HERMETIC_PRICING: dict[str, dict[str, float]] = {
    "openai/gpt-canon": {"input": 2.0, "output": 8.0},
}


@pytest.fixture
def hermetic_pricing(tmp_path: Path) -> Path:
    fixture = tmp_path / "pricing.json"
    fixture.write_text(json.dumps({"models": _HERMETIC_PRICING}))
    reload_pricing(fixture)
    yield fixture
    reload_pricing()


def _make_client(model_name: str) -> LLMClient:
    if "/" in model_name:
        provider, _, slug = model_name.partition("/")
    else:
        provider, slug = "openai", model_name
    cfg = ModelConfig(
        provider=provider,
        name=slug,
        temperature=0.0,
        reasoning=ReasoningConfig(mode="off"),
    )
    with patch.dict("os.environ", {}, clear=False):
        return LLMClient(cfg)


def _make_response(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    hidden_params: dict[str, Any] | None = None,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> Any:
    mock_message = MagicMock()
    mock_message.content = "ok"
    mock_message.tool_calls = None
    mock_message.reasoning_content = None

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    response = MagicMock()
    response.choices = [mock_choice]
    response.usage = MagicMock()
    response.usage.prompt_tokens = prompt_tokens
    response.usage.completion_tokens = completion_tokens
    response.usage.cache_read_input_tokens = cache_read_input_tokens
    response.usage.cache_creation_input_tokens = cache_creation_input_tokens
    response.usage.prompt_tokens_details = None
    response.usage.completion_tokens_details = None
    response._hidden_params = hidden_params if hidden_params is not None else {}
    return response


def _generate(client: LLMClient, response: Any) -> Any:
    with patch("tolokaforge.core.llm.client.completion", return_value=response):
        return client.generate(
            system="You are helpful.",
            messages=[Message(role=MessageRole.USER, content="Hi")],
        )


class TestAssembleResultPerCallRecord:
    """Every assembled ``GenerationResult`` carries one source-tagged call."""

    def test_litellm_hidden_params_populates_call_with_litellm_source(
        self, hermetic_pricing: Path
    ) -> None:
        client = _make_client("openai/gpt-canon")
        response = _make_response(
            prompt_tokens=100,
            completion_tokens=50,
            hidden_params={"response_cost": 0.001234},
        )
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            side_effect=AssertionError("must not consult completion_cost"),
        ):
            result = _generate(client, response)

        assert len(result.usage.calls) == 1
        call = result.usage.calls[0]
        assert call.cost_usd == pytest.approx(0.001234, abs=1e-9)
        assert call.cost_source == "litellm"
        assert call.prompt_tokens == 100
        assert call.completion_tokens == 50
        assert call.latency_s == result.latency_s
        assert call.latency_s >= 0.0

    def test_litellm_completion_cost_populates_call_with_litellm_source(
        self, hermetic_pricing: Path
    ) -> None:
        client = _make_client("openai/gpt-canon")
        response = _make_response(
            prompt_tokens=100,
            completion_tokens=50,
            hidden_params={},
        )
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            return_value=0.005678,
        ):
            result = _generate(client, response)

        assert len(result.usage.calls) == 1
        call = result.usage.calls[0]
        assert call.cost_usd == pytest.approx(0.005678, abs=1e-9)
        assert call.cost_source == "litellm"

    def test_local_table_fallback_populates_call_with_local_source(
        self, hermetic_pricing: Path
    ) -> None:
        client = _make_client("openai/gpt-canon")
        response = _make_response(
            prompt_tokens=100,
            completion_tokens=50,
            hidden_params={},
        )
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            side_effect=Exception("not in litellm catalog"),
        ):
            result = _generate(client, response)

        # 100/1e6 * 2.0 + 50/1e6 * 8.0 = 0.0006
        assert len(result.usage.calls) == 1
        call = result.usage.calls[0]
        assert call.cost_usd == pytest.approx(0.0006, abs=1e-9)
        assert call.cost_source == "local"

    def test_unknown_model_records_unknown_source_and_none_cost(
        self, hermetic_pricing: Path
    ) -> None:
        client = _make_client("nonsense/never-existed")
        response = _make_response(
            prompt_tokens=100,
            completion_tokens=50,
            hidden_params={},
        )
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            side_effect=Exception("not in litellm catalog"),
        ):
            result = _generate(client, response)

        assert len(result.usage.calls) == 1
        call = result.usage.calls[0]
        assert call.cost_usd is None
        assert call.cost_source == "unknown"

    def test_call_token_fields_match_response_usage(self, hermetic_pricing: Path) -> None:
        """Per-call record's token fields mirror the flat ``Usage`` totals
        for a single call; aggregation downstream can rely on either."""
        client = _make_client("openai/gpt-canon")
        response = _make_response(
            prompt_tokens=321,
            completion_tokens=123,
            hidden_params={"response_cost": 0.01},
        )
        result = _generate(client, response)

        call = result.usage.calls[0]
        assert call.prompt_tokens == result.usage.prompt_tokens == 321
        assert call.completion_tokens == result.usage.completion_tokens == 123
