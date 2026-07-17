"""Calibrate how much request parallelism the candidate model tolerates (429 staircase).

New / low-capacity OpenRouter models often rate-limit (or destabilize) under the observe
stage's default 10-wide probe pool, which contaminates the observation and lands the PR in
needs-human for infra reasons. This probe measures the candidate BEFORE the heavy stages:
fire ``waves`` waves of ``level`` concurrent minimal completions at increasing concurrency
levels; a level is CLEAN when it produced zero HTTP 429 and at most one transient non-429
error. The recommendation is the highest clean level.

Safety property: the recommendation is capped by the configured ceiling (today's default),
so calibration can only LOWER parallelism for a weak model, never raise it above the
configured value; the floor keeps a totally-throttled model from going below a workable
minimum. The workflow treats a missing/failed calibration as fail-open (defaults stay).

Deterministic orchestration (``staircase``) is separated from the network call
(``_request_once``) so the logic is unit-testable with a fake requester.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import urllib.error
import urllib.request

import typer

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_LEVELS = "2,4,6,8,10"
DEFAULT_WAVES = 2


def classify(status: int | None) -> str:
    """One outcome bucket per request: ok / rate_limited / error (incl. no-status = transport)."""
    if status == 429:
        return "rate_limited"
    if status is not None and 200 <= status < 300:
        return "ok"
    return "error"


def level_clean(counts: dict[str, int]) -> bool:
    """A level passes with ZERO 429s and at most one transient non-429 error (a lone flake
    must not halve the parallelism of the whole observe)."""
    return counts.get("rate_limited", 0) == 0 and counts.get("error", 0) <= 1


def choose(clean_levels: list[int], floor: int, ceiling: int) -> int:
    """Highest clean level, clamped to [floor, ceiling]; no clean level at all -> floor."""
    best = max(clean_levels) if clean_levels else floor
    return max(floor, min(best, ceiling))


def staircase(
    request_fn,
    levels: list[int],
    waves: int,
    floor: int,
    ceiling: int,
) -> dict:
    """Probe increasing concurrency levels until one is dirty; recommend the last clean one.

    ``request_fn`` is a zero-arg callable returning a classification bucket (see
    :func:`classify`); each level fires ``waves`` waves of ``level`` concurrent calls.
    Levels above the ceiling are not probed (their answer could not be used anyway).
    """
    results: list[dict] = []
    clean: list[int] = []
    for level in sorted(levels):
        if level > ceiling:
            break
        counts = {"ok": 0, "rate_limited": 0, "error": 0}
        for _ in range(waves):
            with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:
                for outcome in pool.map(lambda _i: request_fn(), range(level)):
                    counts[outcome] = counts.get(outcome, 0) + 1
        results.append({"level": level, **counts})
        if not level_clean(counts):
            break
        clean.append(level)
    return {
        "recommended": choose(clean, floor, ceiling),
        "levels": results,
        "floor": floor,
        "ceiling": ceiling,
    }


def _request_once(name: str, api_key: str, timeout: float) -> str:
    """One minimal completion against the candidate (max_tokens=1); returns its bucket."""
    payload = json.dumps(
        {"model": name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    ).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return classify(resp.status)
    except urllib.error.HTTPError as exc:
        return classify(exc.code)
    except Exception:
        return classify(None)


def cli(
    name: str = typer.Option(..., "--name", help="candidate model slug (e.g. meta/muse-spark-1.1)"),
    ceiling: int = typer.Option(
        10, "--ceiling", help="configured parallelism (the cap; calibration only lowers)"
    ),
    floor: int = typer.Option(
        2, "--floor", help="minimum recommendation even when fully throttled"
    ),
    levels: str = typer.Option(
        DEFAULT_LEVELS, "--levels", help="comma-separated concurrency levels to probe"
    ),
    waves: int = typer.Option(
        DEFAULT_WAVES, "--waves", help="waves of concurrent requests per level"
    ),
    timeout: float = typer.Option(45.0, "--timeout", help="per-request timeout seconds"),
) -> None:
    """Print a JSON parallelism recommendation for the candidate ({"recommended": N, ...}).

    Always exits 0: calibration is advisory and must never fail the pipeline; a missing key
    or an unusable probe yields {"recommended": null} and the workflow keeps its defaults.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print(json.dumps({"recommended": None, "error": "OPENROUTER_API_KEY is not set"}))
        raise typer.Exit(0)
    try:
        level_list = [int(x) for x in levels.split(",") if x.strip()]
        result = staircase(
            lambda: _request_once(name, api_key, timeout), level_list, waves, floor, ceiling
        )
    except Exception as exc:  # advisory step: degrade to "no recommendation", never fail
        result = {"recommended": None, "error": str(exc)}
    print(json.dumps(result))
    raise typer.Exit(0)
