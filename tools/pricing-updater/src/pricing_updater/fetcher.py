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


SIGNIFICANT_PRICING_CHANGE_FACTOR = 3.0


def convert_pricing(models: list[dict]) -> dict[str, dict[str, float]]:
    """Convert OpenRouter per-token pricing to USD per 1M tokens.

    OpenRouter's ``pricing`` block carries the canonical fields ``prompt``
    and ``completion`` plus optional ``input_cache_read`` /
    ``input_cache_write`` for prompt-cache-aware models (see
    https://openrouter.ai/docs/api/api-reference/models/get-models). We map
    them to the schema consumed by ``tolokaforge.core.pricing`` —
    ``input``, ``output``, ``cache_read``, ``cache_write``.

    Cache fields are emitted only when non-zero. A literal ``cache_read: 0``
    in pricing.json would tell the cost engine that cached reads are *free*
    on a caching-aware model — the engine treats a missing key as "no
    explicit rate, fall back to ``input``", which is conservative and
    matches the no-cache-info baseline.

    Parameters
    ----------
    models
        Raw model objects from :func:`fetch_openrouter_models`.

    Returns
    -------
    dict[str, dict[str, float]]
        ``{model_id: {"input": …, "output": …, ["cache_read": …,
        "cache_write": …]}}``
    """
    result: dict[str, dict[str, float]] = {}

    for model in models:
        model_id = model.get("id")
        pricing = model.get("pricing")
        if not model_id or not pricing:
            continue

        try:
            prompt_per_token = float(pricing.get("prompt", "0") or "0")
            completion_per_token = float(pricing.get("completion", "0") or "0")
        except (ValueError, TypeError):
            continue

        if prompt_per_token == 0.0 and completion_per_token == 0.0:
            continue

        entry: dict[str, float] = {
            "input": round(prompt_per_token * 1_000_000, 6),
            "output": round(completion_per_token * 1_000_000, 6),
        }

        for api_key, our_key in (
            ("input_cache_read", "cache_read"),
            ("input_cache_write", "cache_write"),
        ):
            try:
                rate_per_token = float(pricing.get(api_key, "0") or "0")
            except (ValueError, TypeError):
                continue
            if rate_per_token > 0.0:
                entry[our_key] = round(rate_per_token * 1_000_000, 6)

        result[model_id] = entry

    return result


def _pricing_change_ratio(old: float, new: float) -> float:
    """Symmetric multiplicative ratio: ``max(new/old, old/new)``.

    Returns 0.0 when either side is non-positive — those models are skipped
    rather than treated as infinite swings.
    """
    if old <= 0 or new <= 0:
        return 0.0
    return max(new / old, old / new)


def find_significant_pricing_changes(
    existing: dict[str, dict[str, float]],
    pricing: dict[str, dict[str, float]],
    threshold: float = SIGNIFICANT_PRICING_CHANGE_FACTOR,
) -> list[tuple[str, dict[str, float], dict[str, float], dict[str, float]]]:
    """Return models whose input or output pricing changed by ≥ ``threshold``×.

    Catches the regression class where a unit-conversion bug, a fetcher
    field-mapping error, or an upstream re-pricing event produces an
    order-of-magnitude swing in the snapshot. Models present only in one
    side are skipped (additions/removals are not "changes" — listing them
    here would drown the signal).

    Returns
    -------
    list[tuple[model_id, old_values, new_values, ratios]]
        ``ratios`` is ``{"input_ratio": …, "output_ratio": …}`` —
        symmetric, so a 3× drop and a 3× rise both report ``3.0``.
    """
    changes: list[tuple[str, dict[str, float], dict[str, float], dict[str, float]]] = []

    for model_id, new_values in pricing.items():
        old_values = existing.get(model_id)
        if not old_values:
            continue

        old_input = float(old_values.get("input", 0) or 0)
        old_output = float(old_values.get("output", 0) or 0)
        new_input = float(new_values.get("input", 0) or 0)
        new_output = float(new_values.get("output", 0) or 0)

        input_ratio = _pricing_change_ratio(old_input, new_input)
        output_ratio = _pricing_change_ratio(old_output, new_output)

        if input_ratio >= threshold or output_ratio >= threshold:
            changes.append(
                (
                    model_id,
                    old_values,
                    new_values,
                    {"input_ratio": input_ratio, "output_ratio": output_ratio},
                )
            )

    return changes


def write_pricing_json(
    pricing: dict[str, dict[str, float]],
    output_path: Path,
    *,
    merge: bool = True,
) -> int:
    """Write the pricing table to a JSON file.

    Merge semantics
    ---------------
    With ``merge=True`` the merge is **field-level** within each model
    entry, not wholesale: any field the API didn't refresh (e.g. a
    hand-curated ``cache_read`` for a direct-API rate that differs from
    OpenRouter's effective rate after markup) survives the update. Without
    field-level merge, every refresh would silently obliterate every
    cache rate that the bundled :mod:`pricing_updater` doesn't itself
    fetch — see PR #104 review for the regression that motivated this.

    Parameters
    ----------
    pricing
        ``{model_id: {"input": …, "output": …, [cache_read, cache_write]}}``
    output_path
        Target file path.
    merge
        When ``True`` (default), field-level merge with existing entries.
        When ``False``, write the new pricing table verbatim.

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
            existing_models = data.get("models", {}) or {}
        except (json.JSONDecodeError, OSError):
            pass

    if merge:
        merged: dict[str, dict[str, float]] = {k: dict(v) for k, v in existing_models.items()}
        for model_id, new_fields in pricing.items():
            if model_id in merged:
                merged[model_id].update(new_fields)
            else:
                merged[model_id] = dict(new_fields)
    else:
        merged = {k: dict(v) for k, v in pricing.items()}

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
