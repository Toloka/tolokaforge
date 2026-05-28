"""Canonical contract for runtime per-call cost extraction.

Pins the priority ladder used by :class:`LLMClient.generate` to source
``cost_usd`` from a litellm completion response:

1. ``response._hidden_params["response_cost"]`` — set by litellm after
   every successful call against its bundled pricing catalog.
2. :func:`litellm.completion_cost` — re-derives the same value; raises
   for unknown models.
3. :func:`tolokaforge.core.pricing.estimate_cost` against the bundled
   ``pricing.json`` table — fallback for models litellm cannot price,
   and the canonical source for offline reanalysis.

Each test pins a concrete numerical outcome against a concrete recorded
response shape + a hermetic pricing fixture, so:

* a regression in our extractor (e.g. reading the wrong key on
  ``_hidden_params``) flips the test red on PR;
* a litellm contract change (e.g. moving ``response_cost`` elsewhere)
  flips the test red on PR;
* a drift in our cache-aware formula (e.g. forgetting to subtract
  cache reads from fresh tokens) flips the test red on PR.

Hermetic fixture: a tmp-path pricing.json + ``reload_pricing`` keeps the
test independent of the bundled pricing catalog. The bundled catalog is
covered by ``tests/unit/test_pricing.py::test_all_benchmark_models_have_pricing``.
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

pytestmark = pytest.mark.canonical


# ---------------------------------------------------------------------------
# Fixtures — hermetic pricing table + canned response shapes
# ---------------------------------------------------------------------------


_HERMETIC_PRICING: dict[str, dict[str, float]] = {
    # Mirrors the Anthropic Opus shape (input 5.0, output 25.0,
    # cache_read 0.5, cache_write 5.0). Pinning concrete numbers lets
    # the canonical tests assert exact-cents outcomes for the
    # cache-aware fallback.
    "anthropic/claude-opus-canon": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 5.0,
    },
    # Model with no cache rates — exercises the "fall back to input
    # rate" default in ``_compute_cost``.
    "openai/gpt-canon": {
        "input": 2.0,
        "output": 8.0,
    },
}


@pytest.fixture
def hermetic_pricing(tmp_path: Path) -> Path:
    """Write a hermetic pricing.json and reload the module against it.

    Reverts to the bundled pricing on teardown so canonical tests are
    order-independent.
    """
    fixture = tmp_path / "pricing.json"
    fixture.write_text(json.dumps({"models": _HERMETIC_PRICING}))
    reload_pricing(fixture)
    yield fixture
    reload_pricing()  # restore bundled


def _make_client(model_name: str) -> LLMClient:
    """Construct an LLMClient whose ``model_name`` matches ``model_name``.

    Splits ``"<provider>/<name>"`` so :meth:`LLMClient._format_model_name`
    leaves the slug unchanged — otherwise it would prepend a *second*
    provider segment and the pricing lookup would miss our hermetic
    fixture entries.
    """
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
        client = LLMClient(cfg)
    assert client.model_name == model_name, (
        f"client.model_name={client.model_name!r} drifted from requested "
        f"{model_name!r} — pricing lookup will miss the hermetic fixture."
    )
    return client


def _make_response(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    hidden_params: dict[str, Any] | None = None,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> Any:
    """Build a MagicMock-shaped litellm response with controllable
    ``_hidden_params`` and usage counters.
    """
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
    """Run a one-shot generate with ``litellm.completion`` mocked."""
    with patch("tolokaforge.core.llm.client.completion", return_value=response):
        return client.generate(
            system="You are helpful.",
            messages=[Message(role=MessageRole.USER, content="Hi")],
        )


# ---------------------------------------------------------------------------
# Priority ladder canon
# ---------------------------------------------------------------------------


class TestCostExtractionPriorityLadder:
    """Pin the source-of-truth ordering for ``GenerationResult.cost_usd``.

    The order matters because each tier carries different drift risk:
    hidden_params is a litellm-internal contract, completion_cost is a
    public litellm helper, and our table is the offline fallback. A
    silent reorder would change the cost for every benchmarked call.
    """

    def test_priority_1_hidden_params_response_cost_wins(
        self,
        hermetic_pricing: Path,
    ) -> None:
        """When hidden_params carries a positive ``response_cost``, the
        helper returns it verbatim — neither litellm.completion_cost nor
        the local pricing table is consulted."""
        client = _make_client("openai/gpt-canon")
        response = _make_response(
            prompt_tokens=100,
            completion_tokens=50,
            hidden_params={"response_cost": 0.001234},
        )

        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            side_effect=AssertionError("priority-1 must not consult completion_cost"),
        ):
            result = _generate(client, response)

        assert result.cost_usd == pytest.approx(0.001234, abs=1e-9)

    def test_priority_2_completion_cost_when_hidden_params_empty(
        self,
        hermetic_pricing: Path,
    ) -> None:
        """Empty hidden_params falls through to ``litellm.completion_cost``;
        local table is not consulted when litellm returns a positive value."""
        client = _make_client("openai/gpt-canon")
        response = _make_response(
            prompt_tokens=100,
            completion_tokens=50,
            hidden_params={},
        )

        # Mock litellm to return a deterministic value distinct from what
        # the local table would produce, so a silent fallback to the
        # local path would change the assertion outcome.
        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            return_value=0.005678,
        ):
            result = _generate(client, response)

        assert result.cost_usd == pytest.approx(0.005678, abs=1e-9)

    def test_priority_3_local_table_when_litellm_unpriced(
        self,
        hermetic_pricing: Path,
    ) -> None:
        """Both litellm paths exhausted → estimate_cost from hermetic table.

        100 input × $2/M + 50 output × $8/M = 0.0002 + 0.0004 = $0.0006.
        """
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

        # 100/1e6 * 2.0 + 50/1e6 * 8.0 = 0.0002 + 0.0004 = 0.0006
        assert result.cost_usd == pytest.approx(0.0006, abs=1e-9)

    def test_priority_3_cache_aware_fallback_anthropic_shape(
        self,
        hermetic_pricing: Path,
    ) -> None:
        """Cache-aware estimate uses the configured cache_read rate.

        prompt_tokens=1_000_000 (litellm-normalised total = fresh + cache)
        cache_read=200_000, completion=0.

        Fresh: 800_000 × $5/M = $4.00.
        Cache read: 200_000 × $0.5/M = $0.10.
        Total: $4.10.
        """
        client = _make_client("anthropic/claude-opus-canon")
        response = _make_response(
            prompt_tokens=1_000_000,
            completion_tokens=0,
            cache_read_input_tokens=200_000,
            hidden_params={},
        )

        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            side_effect=Exception("not in litellm catalog"),
        ):
            result = _generate(client, response)

        assert result.cost_usd == pytest.approx(4.10, abs=1e-6)

    def test_priority_3_cache_write_fallback(
        self,
        hermetic_pricing: Path,
    ) -> None:
        """Cache writes are charged at the configured cache_write rate.

        prompt_tokens=1_000_000, cache_creation=100_000, completion=0.
        Fresh 900k × $5/M = $4.50. Cache write 100k × $5/M = $0.50.
        Total $5.00.
        """
        client = _make_client("anthropic/claude-opus-canon")
        response = _make_response(
            prompt_tokens=1_000_000,
            completion_tokens=0,
            cache_creation_input_tokens=100_000,
            hidden_params={},
        )

        with patch(
            "tolokaforge.core.llm.client.litellm.completion_cost",
            side_effect=Exception("not in litellm catalog"),
        ):
            result = _generate(client, response)

        assert result.cost_usd == pytest.approx(5.00, abs=1e-6)

    def test_unknown_model_after_litellm_failure_returns_none(
        self,
        hermetic_pricing: Path,
    ) -> None:
        """If neither litellm nor our table prices the model, ``cost_usd`` is None.

        We must NOT silently emit 0.0 — that would falsely report the call
        as priced-and-free, distorting cost benchmarks.
        """
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

        assert result.cost_usd is None
