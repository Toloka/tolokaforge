"""Green-check for the resolve fix-loop.

Reads the compose step's ``decision.json`` (its ``fix_targets``) plus a reprobe
``findings.json``, and prints one token the workflow branches on:

* ``CONVERGED``  - every fix-target probe passed all its reprobe reps.
* ``RED:a;b;c``  - these fix-targets are still failing (refine next iteration).
* ``NO_TARGETS`` - the decision named no fix-targets (nothing to prove).
* ``PARSE_FAIL`` - a file was missing/unreadable.

The token-computing logic is :func:`evaluate` (pure, unit-tested); ``run`` loads the
two files, prints the token, and always exits 0 - the caller reads stdout.
"""

from __future__ import annotations

import json


def _load(path: str) -> dict:
    with open(path) as handle:
        return json.load(handle)


def evaluate(decision: dict, reprobe: dict) -> str:
    """Compute the green-check token from a decision + reprobe findings.

    ``CONVERGED`` when every fix-target probe passed all its reprobe reps, ``RED:<...>``
    (up to 8, sorted) when some are still failing, ``NO_TARGETS`` when the decision named
    no fix-targets. File-load failures are handled by :func:`run` (``PARSE_FAIL``)."""
    fix_targets = set(decision.get("fix_targets", []))
    passed = {
        p["probe"]: (p["passed"] == p["runs"] and p["runs"] > 0)
        for section in ("capability", "variants")
        for p in reprobe.get(section, {}).get("per_probe", [])
    }
    if not fix_targets:
        return "NO_TARGETS"
    red = [t for t in fix_targets if not passed.get(t, False)]
    return "CONVERGED" if not red else "RED:" + ";".join(sorted(red)[:8])


def run(decision_path: str, reprobe_path: str) -> int:
    """Load the two files, print the green-check token, and exit 0 (the caller reads
    stdout). A missing/unreadable file prints ``PARSE_FAIL``."""
    try:
        decision = _load(decision_path)
        reprobe = _load(reprobe_path)
    except (OSError, ValueError, IndexError):
        print("PARSE_FAIL")
        return 0
    print(evaluate(decision, reprobe))
    return 0
