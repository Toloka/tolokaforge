"""Green-check + decision parsing for the resolve fix-loop.

Reads the compose step's ``decision.json`` (its ``fix_targets``) plus a reprobe
``findings.json``, and prints one token the workflow branches on:

* ``CONVERGED``  - every fix-target probe passed all its reprobe reps.
* ``RED:a;b;c``  - these fix-targets are still failing (refine next iteration).
* ``NO_TARGETS`` - the decision named no fix-targets (nothing to prove).
* ``PARSE_FAIL`` - a file was missing/unreadable.

``decision_targets`` is the loop's OTHER stdout-token seam: it parses the decision's
``fix_targets`` up front, so that a malformed ``decision.json`` (the agent died
mid-write, or deviated from the schema) reads as ``PARSE_FAIL`` - a stall to retry -
and never as the empty-targets ``NO_TARGETS``, which is the all-ceiling CONVERGENCE
verdict. An inline ``|| echo ''`` extraction cannot tell those apart, and the
difference is an integration shipping with every failure recorded as a ceiling
nobody actually judged.

The token-computing logic is pure and unit-tested; the ``run*`` wrappers load files,
print the token, and always exit 0 - the caller reads stdout.
"""

from __future__ import annotations

import json


def _load(path: str) -> dict:
    with open(path) as handle:
        return json.load(handle)


def decision_targets(decision: dict) -> str:
    """The loop's target token for one decision: ``PARSE_FAIL`` / ``NO_TARGETS`` /
    ``TARGETS:<comma-joined>``.

    Fail-closed: ``fix_targets`` missing, not a list, or containing anything but
    non-empty comma-free strings is ``PARSE_FAIL`` (a comma would corrupt the joined
    form; junit probe ids never contain one). Only a well-formed EMPTY list is the
    all-ceiling ``NO_TARGETS``.
    """
    targets = decision.get("fix_targets")
    if not isinstance(targets, list):
        return "PARSE_FAIL"
    cleaned = []
    for target in targets:
        if not isinstance(target, str) or not target.strip() or "," in target:
            return "PARSE_FAIL"
        cleaned.append(target.strip())
    return "NO_TARGETS" if not cleaned else "TARGETS:" + ",".join(cleaned)


def run_decision_targets(decision_path: str) -> int:
    """Load ``decision.json``, print the target token, exit 0 (the caller reads
    stdout). Missing/unreadable/non-object files print ``PARSE_FAIL``."""
    try:
        decision = _load(decision_path)
    except (OSError, ValueError):
        print("PARSE_FAIL")
        return 0
    if not isinstance(decision, dict):
        print("PARSE_FAIL")
        return 0
    print(decision_targets(decision))
    return 0


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
