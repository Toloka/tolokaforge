#!/usr/bin/env python3
"""Fail a harness matrix job whose bundle shows the run measured nothing.

The matrix has always *run* every shipped harness against real packs and
*asserted nothing*: it invoked the engine, uploaded ``results/`` and exited 0.
A trial reporting no cost, or scoring against a repository its agent never
touched, left the job green. Every defect the harness telemetry work found
would have passed this gate — they were found by a person reading bundles.

What this checks is the **floor**, not the score. A trial may legitimately fail
its task and a harness may legitimately be bad at it; what none of them may do
is come back unmeasured while the job reports success.

Two things are deliberately *not* failures:

* **An overstated cost.** Most models the matrix runs resolve to rows carrying
  no cache-write rate — ``anthropic/claude-sonnet-4-6`` carries neither cache
  rate — so ``cost_cache_rate_fallback`` is the normal state for four of the
  seven jobs. Failing on it would red the matrix permanently and teach everyone
  to ignore it. It is annotated instead, which is what the flag is for.
* **A harness named in** :data:`UNMEASURED_HARNESSES`. Each entry records a
  measurement gap we have diagnosed and cannot close from this repo, with the
  reason inline. The exemption is by name, so a harness that regresses into
  being unmeasured still fails here.

Usage::

    python scripts/ci/assert_harness_run_is_measured.py <results-dir> [harness]
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

UNMEASURED_HARNESSES: dict[str, str] = {
    "gemini-cli": (
        "speaks Google's generateContent, and the LiteLLM gateway that "
        "translates it reports real counts on the unary path and zeros on the "
        "streamed one — which is the path the CLI takes. The engine records "
        "that as unmeasured rather than as zero spend, correctly. Closing it "
        "needs a fix on the gateway side, not here."
    ),
}
"""Harnesses known not to report usage, with why, and nothing else.

Mirrors ``UNSHIELDED_HARNESSES`` in ``tests/unit/test_credential_gateway_schema.py``:
the exemption is named and reasoned, so a *documented* gap does not fail while
a *silent* regression still does. Empty is the goal.
"""


def main(argv: list[str]) -> int:
    if not 2 <= len(argv) <= 3:
        print(f"usage: {argv[0]} <results-dir> [harness]", file=sys.stderr)
        return 2
    root = Path(argv[1])
    harness = argv[2] if len(argv) == 3 else None

    trials = sorted(root.glob("*/trials/*/*/metrics.yaml"))
    if not trials:
        print(f"::error::no trial bundle under {root} — the run produced nothing to check")
        return 1

    exemption = UNMEASURED_HARNESSES.get(harness or "")
    failures: list[str] = []
    notes: list[str] = []

    for metrics_path in trials:
        name = metrics_path.relative_to(root).parent.as_posix()
        metrics = yaml.safe_load(metrics_path.read_text()) or {}
        usage = metrics.get("usage") or {}
        tokens = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        cost = metrics.get("cost_usd")

        if not tokens:
            message = (
                f"{name}: reported no tokens. Either the CLI never reached a provider, "
                "or this harness has no measurement tap wired up."
            )
            (notes if exemption else failures).append(
                f"{message} Exempt: {exemption}" if exemption else message
            )
        elif cost is None:
            failures.append(
                f"{name}: reported {tokens} tokens and no cost. A trial that spent "
                "tokens has a price; a null here reads as free in every aggregate."
            )
        elif cost == 0:
            failures.append(
                f"{name}: reported cost exactly 0 on {tokens} tokens — "
                "'not measured' rendered as 'measured as zero'."
            )

        if metrics.get("cost_cache_rate_fallback"):
            notes.append(
                f"{name}: priced off a row carrying no cache rate, so its cost is an "
                "overestimate. Expected where the provider publishes no cache rate."
            )

    for note in notes:
        print(f"::warning::{note}")
    for failure in failures:
        print(f"::error::{failure}")
    print(f"checked {len(trials)} trial(s); {len(failures)} problem(s), {len(notes)} note(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
