#!/usr/bin/env python
"""Green-check for the resolve fix-loop.

Reads the compose step's ``decision.json`` (its ``fix_targets``) plus a reprobe
``findings.json``, and prints one token the workflow branches on:

* ``CONVERGED``  - every fix-target probe passed all its reprobe reps.
* ``RED:a;b;c``  - these fix-targets are still failing (refine next iteration).
* ``NO_TARGETS`` - the decision named no fix-targets (nothing to prove).
* ``PARSE_FAIL`` - a file was missing/unreadable.

Kept as a tiny script (not inline ``python -c``) so YAML indentation cannot turn the
program into an IndentationError. Exit code is always 0; the caller reads stdout.
"""

from __future__ import annotations

import json
import sys


def _load(path: str) -> dict:
    with open(path) as handle:
        return json.load(handle)


def main() -> None:
    try:
        decision = _load(sys.argv[1])
        reprobe = _load(sys.argv[2])
    except (OSError, ValueError, IndexError):
        print("PARSE_FAIL")
        return
    fix_targets = set(decision.get("fix_targets", []))
    passed = {
        p["probe"]: (p["passed"] == p["runs"] and p["runs"] > 0)
        for section in ("capability", "variants")
        for p in reprobe.get(section, {}).get("per_probe", [])
    }
    if not fix_targets:
        print("NO_TARGETS")
        return
    red = [t for t in fix_targets if not passed.get(t, False)]
    print("CONVERGED" if not red else "RED:" + ";".join(sorted(red)[:8]))


if __name__ == "__main__":
    main()
