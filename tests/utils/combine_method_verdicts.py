"""What each declared ``combine.method`` owes on one split pair of components.

Read by the in-process cross-substrate differential
(``tests/canonical/test_grading_substrate_parity.py``) and by the wire suite
(``tests/integration/test_docker_grading_combine_method.py``), so both tiers hold the
same prediction. A tier restating the table for itself is the shape that drifts: the
canonical answer stays green while the runner answers something else over gRPC.

The verdicts are written out rather than recomputed from the implementation. One
shared dispatch makes the substrates agree however wrong that dispatch is, so what
carries either lock is the value pinned per method plus the three scores being
distinct — and they are distinct only because the components below have three
different numbers for their min, mean and max.
"""

from __future__ import annotations

COMBINE_METHOD_COMPONENTS: dict[str, float] = {"state_checks": 0.0, "transcript_rules": 1.0}
"""The two deterministic components each tier's fixture must score.

Both carry a declared weight and neither is judge- nor probe-graded, so the two
substrates build the same component map and the method is the only variable. On equal
components every method returns one number and no table over them measures anything.
"""

COMBINE_METHOD_PASS_THRESHOLD = 0.8
"""The ``combine.pass_threshold`` the verdicts below answer.

Asserted against each fixture's loaded config: at another threshold the pass flags are
answers to a different question.
"""

COMBINE_METHOD_VERDICTS: dict[str, tuple[float, bool]] = {
    "weighted": (0.5, False),
    "all": (0.0, False),
    "any": (1.0, True),
}
"""``(score, binary_pass)`` each declared method owes on those components.

Keyed by method, with the canonical suite asserting the key set against
``COMBINE_METHODS`` — a human-written table against the declared domain — so a fourth
declared method cannot land without an answer here.
"""
