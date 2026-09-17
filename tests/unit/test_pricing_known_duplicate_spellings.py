"""The shipped pricing table's known duplicate spellings, written down.

This suite **records a wart; it does not demand a fix.** The table carries
several models under two spellings whose rate structures disagree, so which
spelling a run config pins decides its bill:

    anthropic/claude-sonnet-4.6  → input, output, cache_read, cache_write
    anthropic/claude-sonnet-4-6  → input, output

On a trial that read 93 282 of its 124 831 prompt tokens from cache, those
two rows differ by 2.5x — and the dash row is the overstatement, because
``_compute_cost`` falls back to the *input* rate for cache tokens a row has
no rate for.

Repricing the table is not in scope: rates and slug resolution are inputs to
every cost number the engine has ever produced and to the models fingerprint
stamped into run state. The countermeasures live elsewhere — configs pin the
spelling whose row is complete (``docs/CLI.md`` § Custom pricing overlay),
``Orchestrator.load_tasks`` warns when a configured model resolves to an
incomplete row, and ``Metrics.cost_cache_rate_fallback`` marks a trial whose
cost actually hit the fallback.

What this suite adds is visibility: the inventory below is the list nobody
had, so a duplicate that appears, disappears, or changes shape shows up as a
diff on this file instead of as a surprise in a cost column.
"""

from __future__ import annotations

import json
from collections import defaultdict

import pytest

from tolokaforge.core.model_data import bundled_pricing_path
from tolokaforge.core.pricing import estimate_cost

pytestmark = pytest.mark.unit

_OPENROUTER_PREFIX = "openrouter/"

_KNOWN_DISAGREEMENTS: dict[tuple[str, str], str] = {
    # The observed defect. The dash row was hand-added for clients that
    # normalise "." to "-" (see ``test_claude_sonnet_dash_variants_priced``)
    # and copied only input/output, so it prices cache reads 10x high and
    # cache writes 20% low.
    ("anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-4.6"): "dash row has no cache rates",
    # Same shape, one release older; latent only because nothing pins 4.5.
    ("anthropic/claude-sonnet-4-5", "anthropic/claude-sonnet-4.5"): "dash row has no cache rates",
    # ``moonshot/`` is not a namespace the OpenRouter refresh maintains, so
    # this row is frozen hand-curated data — and it is the row a bare
    # ``kimi-*`` name resolves to (``normalize_model_name``), while the
    # refreshed ``moonshotai/`` row carries the cache rate.
    ("moonshot/kimi-k2.5", "moonshotai/kimi-k2.5"): "moonshot/ row has no cache_read",
    # The four ``amazon/`` vs ``nova/`` pairs are NOT one model spelled
    # twice: ``amazon/nova-*`` is Amazon Nova routed through OpenRouter,
    # ``nova/nova-*`` the direct Nova API (``providers.yaml`` → ``nova``,
    # the one provider whose wire slug is bare). Two routes, two published
    # price lists, differing by up to 8x. They are listed here because the
    # fold that catches the real duplicates cannot tell them apart, not
    # because they are wrong.
    ("amazon/nova-lite-v1", "nova/nova-lite-v1"): "separate routes, separate price lists",
    ("amazon/nova-micro-v1", "nova/nova-micro-v1"): "separate routes, separate price lists",
    ("amazon/nova-premier-v1", "nova/nova-premier-v1"): "separate routes, separate price lists",
    ("amazon/nova-pro-v1", "nova/nova-pro-v1"): "separate routes, separate price lists",
}
"""Every pair of table keys that fold to one model yet price differently.

Keyed by the sorted pair; the value says what the disagreement is. Adding to
this list is how a new duplicate gets acknowledged; removing from it is how a
repriced row gets recorded.
"""


def _folded(model_id: str) -> str:
    """The model id with every cosmetic spelling difference folded away.

    The four axes that have produced a duplicate row in practice: a leading
    ``openrouter/`` route marker, the vendor namespace, ``.`` vs ``-`` in a
    version number, and case.
    """
    stripped = (
        model_id[len(_OPENROUTER_PREFIX) :] if model_id.startswith(_OPENROUTER_PREFIX) else model_id
    )
    _, _, basename = stripped.rpartition("/")
    return basename.lower().replace(".", "-")


def _disagreeing_pairs() -> dict[tuple[str, str], tuple[dict, dict]]:
    """Pairs of shipped keys that fold together but carry different rates."""
    models = json.loads(bundled_pricing_path().read_text(encoding="utf-8"))["models"]

    by_fold: dict[str, list[str]] = defaultdict(list)
    for model_id in models:
        by_fold[_folded(model_id)].append(model_id)

    pairs: dict[tuple[str, str], tuple[dict, dict]] = {}
    for spellings in by_fold.values():
        ordered = sorted(spellings)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if models[left] != models[right]:
                    pairs[(left, right)] = (models[left], models[right])
    return pairs


def test_the_inventory_of_duplicate_spellings_that_disagree_is_unchanged() -> None:
    """The recorded list is exactly what the shipped table carries.

    Not a gate on the data — a gate on the *record* of the data. A new
    duplicate lands as a failure here, which is the point: today's list was
    discovered by hand after a cost column read 2.5x high.
    """
    found = _disagreeing_pairs()

    unrecorded = {pair: rates for pair, rates in found.items() if pair not in _KNOWN_DISAGREEMENTS}
    assert not unrecorded, (
        "the table carries a duplicate spelling nobody has acknowledged — a caller's "
        f"spelling decides its bill: {unrecorded}. Add it to _KNOWN_DISAGREEMENTS with "
        "what the disagreement is, or reprice the row."
    )

    resolved = sorted(pair for pair in _KNOWN_DISAGREEMENTS if pair not in found)
    assert not resolved, (
        f"these pairs no longer disagree: {resolved}. Drop them from "
        "_KNOWN_DISAGREEMENTS so the record matches the data."
    )


def test_the_two_sonnet_46_spellings_bill_a_cache_heavy_trial_2_5x_apart() -> None:
    """The consequence, in the numbers that were measured.

    Usage from one real coding-harness trial: 124 831 prompt tokens of which
    93 282 cache reads and 31 544 cache writes, 386 completion. The vendor
    billed 0.15207959999999998 for it, which the dotted row reproduces to
    nine decimal places — the pricing *formula* is right, and only the row
    the spelling resolves to is wrong.
    """
    usage = {
        "input_tokens": 124_831,
        "output_tokens": 386,
        "cache_read_input_tokens": 93_282,
        "cache_creation_input_tokens": 31_544,
    }

    dotted = estimate_cost("anthropic/claude-sonnet-4.6", **usage)
    dashed = estimate_cost("anthropic/claude-sonnet-4-6", **usage)

    assert dotted == pytest.approx(0.1520796, abs=1e-9)
    assert dashed == pytest.approx(0.380283, abs=1e-9)
    # Stated as a ratio too, so the size of the error is part of the record.
    assert dashed / dotted == pytest.approx(2.5, abs=0.01)


def test_the_openrouter_route_prefix_does_not_change_which_row_is_billed() -> None:
    """``openrouter/<vendor>/<model>`` is the wire form a run config pins.

    ``normalize_model_name`` strips one leading ``openrouter/``, so the route
    marker is not what selects the row — which is why aligning a config's
    spelling is a change to the vendor/model part of the name, not to the
    prefix.
    """
    usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 500_000}

    assert estimate_cost("openrouter/anthropic/claude-sonnet-4.6", **usage) == estimate_cost(
        "anthropic/claude-sonnet-4.6", **usage
    )
    assert estimate_cost("openrouter/anthropic/claude-sonnet-4-6", **usage) == estimate_cost(
        "anthropic/claude-sonnet-4-6", **usage
    )
