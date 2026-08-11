"""Ensure the candidate model has a pricing.json entry (best-effort, minimal diff).

Runs BEFORE observe so ``COST_USD_POPULATED`` can find a price for a model litellm
may not know natively (the pricing.json fallback).

Modes:
  (default)  If the candidate's litellm name is missing from pricing.json ``models``,
             fetch OpenRouter ``/api/v1/models``; if the model is listed with
             non-zero pricing, insert ONLY that one key (field-level, minimal diff).
             Best-effort: exit 0 on any failure (model not on OpenRouter, fetch
             error) - the finalize agent tops it up and the auto-merge price gate is
             the backstop.
  --check    Do not fetch/write. Exit 0 if the name is already priced, 1 if not.
             Used as the auto-merge precondition (the "price check").

Units: USD per 1M tokens (OpenRouter reports per-token; multiply by 1e6), matching
``tolokaforge_models/src/tolokaforge_models/data/pricing.json`` and ``tools/pricing-updater``.

The pure helpers (``entry_for`` etc.) are unit-tested; ``run`` does the I/O and
returns an exit code.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_TIMEOUT = 30

DEFAULT_PRICING_FILE = "tolokaforge_models/src/tolokaforge_models/data/pricing.json"


def _models(pricing_file: Path) -> dict:
    try:
        return json.loads(pricing_file.read_text()).get("models") or {}
    except (OSError, ValueError):
        return {}


def _fetch_openrouter() -> list[dict]:
    req = urllib.request.Request(_OPENROUTER_MODELS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 (fixed https URL)
        return json.loads(resp.read().decode("utf-8")).get("data", [])


def entry_for(models: list[dict], name: str) -> dict | None:
    """Per-1M-USD ``{input, output[, cache_read, cache_write]}`` for ``name``, or None.

    Mirrors ``tools/pricing-updater`` convert: per-token prompt/completion * 1e6, and
    cache rates only when non-zero (a literal ``cache_read: 0`` would tell the cost
    engine cached reads are free; a missing key falls back to ``input``).
    """
    for model in models:
        if model.get("id") != name:
            continue
        pricing = model.get("pricing") or {}
        try:
            prompt = float(pricing.get("prompt", "0") or "0")
            completion = float(pricing.get("completion", "0") or "0")
        except (ValueError, TypeError):
            return None
        if prompt == 0.0 and completion == 0.0:
            return None
        entry = {
            "input": round(prompt * 1_000_000, 6),
            "output": round(completion * 1_000_000, 6),
        }
        for api_key, our_key in (
            ("input_cache_read", "cache_read"),
            ("input_cache_write", "cache_write"),
        ):
            try:
                rate = float(pricing.get(api_key, "0") or "0")
            except (ValueError, TypeError):
                rate = 0.0
            if rate > 0.0:
                entry[our_key] = round(rate * 1_000_000, 6)
        return entry
    return None


def _insert(pricing_file: Path, name: str, entry: dict) -> None:
    data = json.loads(pricing_file.read_text())
    data.setdefault("models", {})[name] = entry
    data["models"] = dict(sorted(data["models"].items()))
    data.setdefault("_meta", {})["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    pricing_file.write_text(json.dumps(data, indent=2) + "\n")


def run(name: str, pricing_file: str = DEFAULT_PRICING_FILE, check: bool = False) -> int:
    """Ensure ``name`` is priced in ``pricing_file``. Returns an exit code.

    Default mode is best-effort (always exit 0): fetch OpenRouter and insert the entry
    if found. ``--check`` mode does no fetch/write and exits 1 if the name is unpriced.
    """
    pf = Path(pricing_file)
    priced = name in _models(pf)

    if check:
        print(f"ensure_pricing --check: '{name}' priced={priced}")
        return 0 if priced else 1
    if priced:
        print(f"ensure_pricing: '{name}' already priced - no change")
        return 0
    try:
        entry = entry_for(_fetch_openrouter(), name)
    except Exception as exc:  # best-effort: a pricing miss must not block observe
        print(
            f"::warning::ensure_pricing: OpenRouter fetch failed ({exc}); finalize agent must fill '{name}'"
        )
        return 0
    if not entry:
        print(
            f"::warning::ensure_pricing: '{name}' not on OpenRouter with non-zero pricing; "
            "finalize agent must fill it"
        )
        return 0
    _insert(pf, name, entry)
    print(f"ensure_pricing: added '{name}' = {entry}")
    return 0
