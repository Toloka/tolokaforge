"""Provider pricing for token cost estimation.

Pricing data is loaded from ``tolokaforge/core/data/pricing.json`` which can be
refreshed from the OpenRouter API using the ``pricing-updater`` tool::

    uv run pricing-updater update

Values reflect *OpenRouter-routed* rates, which is what every Anthropic-family
call in this harness is billed at — :class:`LLMClient` always prefixes the
litellm model id with ``openrouter/`` for ``provider="openrouter"`` model
configs (see ``_format_model_name``) and sets
``custom_llm_provider="openrouter"`` (see ``_build_kwargs``); no preset
configures direct Anthropic API. The OR-discounted Opus 4.5+ tier
(``$5/$25`` input/output per 1M tokens — verified live against
``openrouter.ai/api/v1/models``) is therefore the correct billed rate, not a
data error. If a future preset adds a direct-Anthropic route, the
``openrouter/`` strip in :func:`normalize_model_name` will need a sibling
``anthropic_direct/...`` namespace; today the strip is correct because it
collapses route+model into a single key whose price is unambiguous.

When a model is not found in the pricing table, :func:`estimate_cost` returns
``None`` so callers can distinguish *unknown cost* from *zero cost*.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load pricing data from JSON
# ---------------------------------------------------------------------------

_PRICING_DATA_PATH = Path(__file__).parent / "data" / "pricing.json"

MODEL_PRICING: dict[str, dict[str, float]] = {}


def _load_pricing(path: Path | None = None) -> dict[str, dict[str, float]]:
    """Load pricing data from a JSON file.

    Parameters
    ----------
    path
        Path to ``pricing.json``.  Defaults to the bundled package data file.

    Returns
    -------
    dict[str, dict[str, float]]
        ``{model_id: {"input": price_per_1M, "output": price_per_1M}}``
    """
    target = path or _PRICING_DATA_PATH
    try:
        with open(target) as fh:
            data = json.load(fh)
        models = data.get("models", data)  # support bare dict or {"models": {...}}
        if not isinstance(models, dict):
            logger.error("pricing_data_invalid: %s", target)
            return {}
        return models
    except FileNotFoundError:
        logger.error("pricing_data_not_found: %s", target)
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("pricing_data_load_error: %s — %s", target, exc)
        return {}


def reload_pricing(path: Path | None = None) -> None:
    """Reload the pricing table, optionally from a custom file.

    This is useful for testing or when the pricing data has been updated.
    Mutates :data:`MODEL_PRICING` in-place so that existing references see
    the updated data.
    """
    MODEL_PRICING.clear()
    MODEL_PRICING.update(_load_pricing(path))


# Initialise on import
MODEL_PRICING = _load_pricing()


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float | None:
    """Estimate cost in USD against the bundled pricing table.

    This is the **fallback** path; runtime cost is sourced from
    :func:`litellm.completion_cost` (see ``LLMClient.generate``). The bundled
    table powers offline reanalysis where we have token counts but no live
    response object.

    ``input_tokens`` is the litellm-normalised prompt total — it already
    includes ``cache_read_input_tokens`` and ``cache_creation_input_tokens``,
    matching what ``UsageExtractor`` produces from ``response.usage``.

    Parameters
    ----------
    model
        Model identifier (e.g. ``"openai/gpt-4o"``, ``"claude-sonnet-4.6"``).
    input_tokens
        Total prompt tokens (including any cache reads + writes).
    output_tokens
        Completion tokens produced by the call.
    cache_read_input_tokens
        Tokens read from prompt cache. Charged at ``cache_read`` if present
        in the pricing entry; otherwise the input rate (a known overestimate
        for providers whose cached-read rate is below their input rate — see
        ``pricing.json`` for which entries carry per-cache rates).
    cache_creation_input_tokens
        Tokens written into prompt cache. Charged at ``cache_write``.
    reasoning_tokens
        Hidden chain-of-thought tokens. Billed at the output rate by every
        provider that exposes them (OpenAI o-series / GPT-5, Anthropic
        thinking, etc.). Summed with ``output_tokens`` before pricing.

    Returns
    -------
    float | None
        Estimated cost in USD, or ``None`` if pricing is unknown for the model.
    """
    model_key = normalize_model_name(model)

    pricing = MODEL_PRICING.get(model_key)
    if pricing is None:
        logger.warning(
            "unknown_model_pricing: no pricing entry for model %s (normalised: %s). "
            "Run `uv run pricing-updater update` to refresh the pricing table.",
            model,
            model_key,
        )
        return None

    return _compute_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens + reasoning_tokens,
        cache_read=cache_read_input_tokens,
        cache_write=cache_creation_input_tokens,
        pricing=pricing,
        context=model_key,
    )


def _compute_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    pricing: dict[str, float],
    context: str,
) -> float:
    """Cache-aware cost formula shared by runtime estimate + offline reanalysis.

    ``context`` is included in the inconsistency warning so a stale or buggy
    metrics record can be traced back to its source (model id or trial id).
    """
    fresh_input = input_tokens - cache_read - cache_write
    if fresh_input < 0:
        logger.warning(
            "inconsistent_cache_usage: cache_read+cache_write (%d+%d) > input_tokens (%d) "
            "for %s — usage accounting bug; clamping fresh_input to 0",
            cache_read,
            cache_write,
            input_tokens,
            context,
        )
        fresh_input = 0

    input_rate = pricing["input"]
    output_rate = pricing["output"]
    cache_read_rate = pricing.get("cache_read", input_rate)
    cache_write_rate = pricing.get("cache_write", input_rate)

    return (
        (fresh_input / 1_000_000) * input_rate
        + (cache_read / 1_000_000) * cache_read_rate
        + (cache_write / 1_000_000) * cache_write_rate
        + (output_tokens / 1_000_000) * output_rate
    )


# ---------------------------------------------------------------------------
# Model name normalization
# ---------------------------------------------------------------------------


def normalize_model_name(model: str) -> str:
    """Normalize model name to match pricing table keys.

    Handles various formats:

    - ``"gpt-4o"`` → ``"openai/gpt-4o"``
    - ``"claude-sonnet-4.5"`` → ``"anthropic/claude-sonnet-4.5"``
    - ``"minimax-m2.7"`` → ``"minimax/minimax-m2.7"``
    - ``"openai/gpt-4o"`` → ``"openai/gpt-4o"`` (no change)
    - ``"openrouter/openai/gpt-4o"`` → ``"openai/gpt-4o"`` (strip routing prefix)
    """
    # Strip provider routing prefix added by litellm for OpenRouter calls
    # e.g. "openrouter/anthropic/claude-sonnet-4.6" → "anthropic/claude-sonnet-4.6"
    if model.startswith("openrouter/"):
        model = model[len("openrouter/") :]

    # If already has provider prefix, return as is
    if "/" in model:
        return model

    # Try to infer provider from model name
    model_lower = model.lower()

    if (
        model_lower.startswith("gpt")
        or model_lower.startswith("o1")
        or model_lower.startswith("o3")
    ):
        return f"openai/{model}"
    elif model_lower.startswith("claude"):
        return f"anthropic/{model}"
    elif model_lower.startswith("gemini") or model_lower.startswith("gemma"):
        return f"google/{model}"
    elif (
        model_lower.startswith("mistral")
        or model_lower.startswith("mixtral")
        or model_lower.startswith("codestral")
        or model_lower.startswith("devstral")
        or model_lower.startswith("magistral")
        or model_lower.startswith("ministral")
        or model_lower.startswith("pixtral")
    ):
        return f"mistralai/{model}"
    elif "llama" in model_lower:
        return f"meta-llama/{model}"
    elif model_lower.startswith("deepseek"):
        return f"deepseek/{model}"
    elif model_lower.startswith("grok"):
        return f"x-ai/{model}"
    elif model_lower.startswith("qwen"):
        return f"qwen/{model}"
    elif model_lower.startswith("minimax"):
        return f"minimax/{model}"
    elif model_lower.startswith("kimi"):
        return f"moonshot/{model}"
    elif model_lower.startswith("nova") or "nova" in model_lower:
        return f"nova/{model}"
    elif "embedding" in model_lower:
        return f"openai/{model}"

    # Unknown model, return as is
    return model


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_pricing_info(model: str) -> dict[str, float] | None:
    """Get pricing information for a model.

    Parameters
    ----------
    model
        Model identifier.

    Returns
    -------
    dict[str, float] | None
        ``{"input": …, "output": …}`` pricing per 1M tokens, or ``None``.
    """
    model_key = normalize_model_name(model)
    return MODEL_PRICING.get(model_key)


def list_supported_models() -> dict[str, dict[str, float]]:
    """Get all models with known pricing.

    Returns
    -------
    dict[str, dict[str, float]]
        Copy of the full pricing table.
    """
    return MODEL_PRICING.copy()
