#!/usr/bin/env python3
"""Fail a harness matrix job whose bundle shows the run measured nothing.

The matrix has always *run* every shipped harness against real packs and
*asserted nothing*: it invoked the engine, uploaded ``results/`` and exited 0.
A trial reporting no cost, or scoring against a repository its agent never
touched, left the job green. Every defect the harness telemetry work found
would have passed this gate — they were found by a person reading bundles.

What this checks is the floor, not the score: a trial may legitimately fail its
task, and a harness may legitimately be bad at it. What it may not do is come
back unmeasured while the job reports success.

Usage::

    python scripts/ci/assert_harness_run_is_measured.py results/
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


def _trials(results_root: Path) -> list[Path]:
    return sorted(results_root.glob("*/trials/*/*/metrics.yaml"))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <results-dir>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    trials = _trials(root)
    if not trials:
        print(f"::error::no trial bundle under {root} — the run produced nothing to check")
        return 1

    failures: list[str] = []
    for metrics_path in trials:
        name = metrics_path.relative_to(root).parent.as_posix()
        metrics = yaml.safe_load(metrics_path.read_text()) or {}
        usage = metrics.get("usage") or {}
        prompt = usage.get("prompt_tokens") or 0
        completion = usage.get("completion_tokens") or 0
        cost = metrics.get("cost_usd")

        if not prompt and not completion:
            failures.append(
                f"{name}: reported no tokens. Either the CLI never reached a provider, "
                "or this harness has no measurement tap wired up."
            )
        elif cost is None:
            failures.append(
                f"{name}: reported {prompt + completion} tokens and no cost. A trial that "
                "spent tokens has a price; a null here reads as free in every aggregate."
            )
        elif cost == 0:
            failures.append(
                f"{name}: reported cost exactly 0 on {prompt + completion} tokens — "
                "'not measured' rendered as 'measured as zero'."
            )
        if metrics.get("cost_cache_rate_fallback"):
            failures.append(
                f"{name}: priced off a row carrying no cache rate, so its cost is "
                "overstated. Fix the model's rates before trusting this number."
            )

    for failure in failures:
        print(f"::error::{failure}")
    print(f"checked {len(trials)} trial(s); {len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
