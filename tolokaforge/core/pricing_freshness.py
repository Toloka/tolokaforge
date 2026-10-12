"""Whether the prices a run is about to use still match their source.

The shipped table is a copy. It names the source it was fetched from and the
moment it was fetched, refreshing it is a manual command in no CI workflow, and
nothing in the engine ever compared the two. A copy 16 days behind priced
``openai/gpt-5.6-sol`` at $5.00 per million input tokens against an actual
$2.00 and ``moonshotai/kimi-k3`` at $3.00 against $2.10, overstating a
harness sweep by 2.5x and 1.4x and inverting its cost ranking.

None of the signals already in the engine could catch that. The cache-rate
preflight asks whether a row is *complete*, the per-trial fallback flag asks
whether cache tokens were billed at the input rate, and the vendor cross-check
asks whether our arithmetic matches a CLI's own figure — all three passed,
because the arithmetic was right and only the inputs had aged. Staleness is a
question about the table, not about any trial, and it has to be asked of the
source.

Answering it needs the network, so a failure to ask is never an answer: an
unreachable source leaves the run alone and says so. Silence here means "not
checked", never "checked and current".
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib import request as urllib_request

from tolokaforge.core.pricing import MODEL_PRICING, normalize_model_name

__all__ = [
    "PriceDrift",
    "compare_against_source",
    "live_prices",
    "reconcile_with_pinned_providers",
]

_FETCH_TIMEOUT_S = 15.0
"""Seconds to wait on the source. Short: this runs before every priced run,
and a source too slow to answer is a source the run proceeds without."""

_TOLERANCE = 0.01
"""Relative difference treated as agreement — one percent.

Absorbs the rounding a per-million conversion introduces without absorbing a
real change: the two drifts that motivated this were 150% and 43%.
"""

_COMPARED_RATES = {
    "input": "prompt",
    "output": "completion",
    "cache_read": "input_cache_read",
    "cache_write": "input_cache_write",
}
"""Our rate name → the source's, for the four rates a trial is billed on."""


@dataclass(frozen=True)
class PriceDrift:
    """One rate that no longer matches the source."""

    model: str
    """The pricing key, as resolved — not as the config spelled it."""

    rate: str
    """Which rate drifted: ``input``, ``output``, ``cache_read``, ``cache_write``."""

    ours: float
    theirs: float

    @property
    def ratio(self) -> float:
        """How many times our rate is the source's. ``inf`` when theirs is free."""
        return self.ours / self.theirs if self.theirs else float("inf")

    def describe(self) -> str:
        return f"{self.model} {self.rate}: table ${self.ours} vs source ${self.theirs}"


def _per_million(pricing: Mapping[str, object]) -> dict[str, float]:
    """Our per-million rates from a source's per-token ``pricing`` block."""
    rates: dict[str, float] = {}
    for ours, theirs in _COMPARED_RATES.items():
        raw = pricing.get(theirs)
        if raw in (None, "", "0"):
            continue
        try:
            rates[ours] = float(raw) * 1_000_000  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return rates


def live_prices(source_url: str) -> dict[str, dict[str, float]] | None:
    """Per-million rates the source publishes, or ``None`` when it cannot be asked.

    ``None`` for any transport or shape failure. The caller must treat it as
    "not checked" — refusing a run because a price source was briefly
    unreachable would fail runs for a reason that has nothing to do with them.
    """
    try:
        with urllib_request.urlopen(source_url, timeout=_FETCH_TIMEOUT_S) as response:
            payload = json.loads(response.read())
    except Exception:  # noqa: BLE001 — every failure means "could not ask"
        return None
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return None
    prices: dict[str, dict[str, float]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        pricing = entry.get("pricing")
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        rates = _per_million(pricing)
        if rates:
            prices[model_id] = rates
    return prices or None


def compare_against_source(
    models: Iterable[str], source: Mapping[str, Mapping[str, float]]
) -> list[PriceDrift]:
    """Rates in the shipped table that *source* no longer agrees with.

    Only rates present on both sides are compared. A rate the table omits is
    the cache-rate preflight's question, and a rate the source omits is not
    evidence the table is wrong — neither is drift, and reporting them here
    would bury the real thing.
    """
    drifts: list[PriceDrift] = []
    for model in models:
        key = normalize_model_name(model)
        ours = MODEL_PRICING.get(key)
        theirs = source.get(key)
        if not ours or not theirs:
            continue
        for rate in _COMPARED_RATES:
            if rate not in ours or rate not in theirs:
                continue
            mine, yours = float(ours[rate]), float(theirs[rate])
            if yours and abs(mine - yours) / yours > _TOLERANCE:
                drifts.append(PriceDrift(model=key, rate=rate, ours=mine, theirs=yours))
    return drifts


def _provider_slug(text: str) -> str:
    """``"Z.AI"`` → ``"z-ai"``: the spelling ``provider_order`` entries use."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _endpoint_prices(source_url: str, model: str) -> dict[str, dict[str, float]] | None:
    """Per-million rates of each provider endpoint serving *model*, by provider slug.

    ``None`` when the per-model endpoints listing cannot be read, for the same
    reason :func:`live_prices` returns it: failing to ask is not an answer.
    """
    try:
        with urllib_request.urlopen(
            f"{source_url.rstrip('/')}/{model}/endpoints", timeout=_FETCH_TIMEOUT_S
        ) as response:
            payload = json.loads(response.read())
    except Exception:  # noqa: BLE001 — every failure means "could not ask"
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        return None
    prices: dict[str, dict[str, float]] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("pricing"), dict):
            continue
        rates = _per_million(endpoint["pricing"])
        if not rates:
            continue
        # ``tag`` is "z-ai" or "z-ai/fp8" (provider/quantization); a pin names
        # the provider, so every quantization of it is a candidate.
        for name in (endpoint.get("tag"), endpoint.get("provider_name")):
            if isinstance(name, str) and name:
                prices.setdefault(_provider_slug(name.split("/")[0]), rates)
    return prices or None


def reconcile_with_pinned_providers(
    drifts: Sequence[PriceDrift],
    pins: Mapping[str, Sequence[str]],
    source_url: str,
) -> list[PriceDrift]:
    """Drift that survives asking the providers a run is pinned to.

    The source's listing carries one rate per slug, but a run that pins
    ``provider_order`` with ``allow_fallbacks: false`` is billed by one of the
    pinned providers, not by whichever the listing shows. A pinned provider
    that is not the cheapest makes the listing disagree with a table that is
    exactly what the run pays. So for each drifting model with a pin, the table
    is compared against the pinned providers' own endpoints and agreeing with
    any one of them is not drift.

    Models without a pin keep their drift unchanged. A pinned model whose
    endpoints cannot be read, or none of whose pinned providers serve it, is
    "not checked" and reports nothing: the slug-level rate is known to be the
    wrong thing to compare it with.

    *pins* maps the resolved pricing key to the pinned provider slugs.
    """
    kept: list[PriceDrift] = []
    verdicts: dict[str, list[PriceDrift]] = {}
    for drift in drifts:
        if drift.model not in pins:
            kept.append(drift)
            continue
        if drift.model not in verdicts:
            endpoints = _endpoint_prices(source_url, drift.model)
            pinned = {
                slug: endpoints[slug]
                for slug in (_provider_slug(name) for name in pins[drift.model])
                if endpoints and slug in endpoints
            }
            per_provider = [
                compare_against_source([drift.model], {drift.model: rates})
                for rates in pinned.values()
            ]
            agrees = not per_provider or any(not found for found in per_provider)
            verdicts[drift.model] = [] if agrees else per_provider[0]
            kept.extend(verdicts[drift.model])
    return kept
