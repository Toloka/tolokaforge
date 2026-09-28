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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib import request as urllib_request

from tolokaforge.core.pricing import MODEL_PRICING, normalize_model_name

__all__ = ["PriceDrift", "compare_against_source", "live_prices"]

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
        rates: dict[str, float] = {}
        for ours, theirs in _COMPARED_RATES.items():
            raw = pricing.get(theirs)
            if raw in (None, "", "0"):
                continue
            try:
                rates[ours] = float(raw) * 1_000_000
            except (TypeError, ValueError):
                continue
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
