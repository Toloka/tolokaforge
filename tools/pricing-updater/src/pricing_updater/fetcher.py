"""Fetch model pricing from the OpenRouter API.

OpenRouter exposes ``GET /api/v1/models`` which returns pricing per-token as
string values.  This module fetches the data and converts to *USD per 1 M
tokens* which is the format used by ``tolokaforge/core/data/pricing.json``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Default output path — the bundled pricing data file inside the tolokaforge package.
DEFAULT_PRICING_PATH = (
    Path(__file__).resolve().parents[4] / "tolokaforge" / "core" / "data" / "pricing.json"
)


def fetch_openrouter_models(timeout: float = 30.0) -> list[dict]:
    """Fetch the full model list from OpenRouter.

    Returns
    -------
    list[dict]
        Raw model objects from the API response.
    """
    resp = httpx.get(OPENROUTER_MODELS_URL, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", [])


def convert_pricing(models: list[dict]) -> dict[str, dict[str, float]]:
    """Convert OpenRouter per-token pricing to USD per 1M tokens.

    Parameters
    ----------
    models
        Raw model objects from :func:`fetch_openrouter_models`.

    Returns
    -------
    dict[str, dict[str, float]]
        ``{model_id: {"input": price_per_1M, "output": price_per_1M}}``
    """
    result: dict[str, dict[str, float]] = {}

    for model in models:
        model_id = model.get("id")
        pricing = model.get("pricing")
        if not model_id or not pricing:
            continue

        try:
            # OpenRouter returns price per token as a string
            prompt_per_token = float(pricing.get("prompt", "0") or "0")
            completion_per_token = float(pricing.get("completion", "0") or "0")
        except (ValueError, TypeError):
            continue

        # Skip models with zero pricing (free/unknown)
        if prompt_per_token == 0.0 and completion_per_token == 0.0:
            continue

        # Convert per-token to per-1M-tokens
        input_per_1m = round(prompt_per_token * 1_000_000, 6)
        output_per_1m = round(completion_per_token * 1_000_000, 6)

        result[model_id] = {"input": input_per_1m, "output": output_per_1m}

    return result


def write_pricing_json(
    pricing: dict[str, dict[str, float]],
    output_path: Path,
    *,
    merge: bool = True,
) -> int:
    """Write the pricing table to a JSON file.

    Parameters
    ----------
    pricing
        ``{model_id: {"input": …, "output": …}}``
    output_path
        Target file path.
    merge
        When ``True``, merge with existing entries rather than replacing.
        Existing entries are updated, new entries are added.

    Returns
    -------
    int
        Number of models in the final file.
    """
    existing_models: dict[str, dict[str, float]] = {}

    if merge and output_path.exists():
        try:
            with open(output_path) as fh:
                data = json.load(fh)
            existing_models = data.get("models", {})
        except (json.JSONDecodeError, OSError):
            pass

    # Merge: new pricing overrides existing
    merged = {**existing_models, **pricing}

    payload = {
        "_meta": {
            "description": "Model pricing in USD per 1M tokens. Source: OpenRouter API.",
            "source_url": OPENROUTER_MODELS_URL,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            "notes": "Run `uv run pricing-updater update` to refresh from OpenRouter API.",
        },
        "models": dict(sorted(merged.items())),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")

    return len(merged)
