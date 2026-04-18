"""Provider pricing for token cost estimation.

Pricing data is loaded from ``tolokaforge/core/data/pricing.json`` which can be
refreshed from the OpenRouter API using the ``pricing-updater`` tool::

    uv run pricing-updater update

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
) -> float | None:
    """Estimate cost in USD for a model API call.

    Parameters
    ----------
    model
        Model identifier (e.g. ``"openai/gpt-4o"``, ``"claude-sonnet-4.6"``).
    input_tokens
        Number of input tokens.
    output_tokens
        Number of output tokens.

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

    # Calculate cost (pricing is per 1M tokens)
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    return input_cost + output_cost


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
    - ``"openrouter/anthropic/claude-sonnet-4-6"`` → ``"anthropic/claude-sonnet-4-6"``
    """
    # Strip routing provider prefix (openrouter is a proxy, not a model provider)
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
