"""Unit tests for ``auto_integration.greencheck.evaluate`` (the resolve fix-loop token).

``evaluate`` is pure: given a decision (its ``fix_targets``) and a reprobe findings dict
(``capability`` / ``variants`` -> ``per_probe`` of ``{probe, passed, runs}``), it returns
CONVERGED (every fix-target passed all reps), RED:<...> (some still failing), or
NO_TARGETS (nothing to prove). The PARSE_FAIL token is ``run``'s job, not ``evaluate``'s.
"""

from __future__ import annotations

import auto_integration.greencheck as greencheck


def _reprobe(capability=None, variants=None):
    return {
        "capability": {"per_probe": capability or []},
        "variants": {"per_probe": variants or []},
    }


def test_converged_when_all_fix_targets_pass_every_rep():
    decision = {"fix_targets": ["test_a[x]", "test_b[y]"]}
    reprobe = _reprobe(
        capability=[
            {"probe": "test_a[x]", "passed": 15, "runs": 15},
            {"probe": "test_b[y]", "passed": 10, "runs": 10},
        ]
    )
    assert greencheck.evaluate(decision, reprobe) == "CONVERGED"


def test_red_when_a_fix_target_is_still_failing():
    decision = {"fix_targets": ["test_a[x]", "test_b[y]"]}
    reprobe = _reprobe(
        capability=[
            {"probe": "test_a[x]", "passed": 15, "runs": 15},
            {"probe": "test_b[y]", "passed": 9, "runs": 10},  # not all reps passed
        ]
    )
    assert greencheck.evaluate(decision, reprobe) == "RED:test_b[y]"


def test_variants_section_counts_toward_green():
    decision = {"fix_targets": ["test_variant_z[q]"]}
    reprobe = _reprobe(variants=[{"probe": "test_variant_z[q]", "passed": 5, "runs": 5}])
    assert greencheck.evaluate(decision, reprobe) == "CONVERGED"


def test_zero_runs_is_not_a_pass():
    decision = {"fix_targets": ["test_a[x]"]}
    reprobe = _reprobe(capability=[{"probe": "test_a[x]", "passed": 0, "runs": 0}])
    assert greencheck.evaluate(decision, reprobe) == "RED:test_a[x]"


def test_missing_probe_in_reprobe_is_red():
    decision = {"fix_targets": ["test_never_ran[x]"]}
    assert greencheck.evaluate(decision, _reprobe()) == "RED:test_never_ran[x]"


def test_no_targets_when_decision_names_none():
    assert greencheck.evaluate({"fix_targets": []}, _reprobe()) == "NO_TARGETS"
    # missing key entirely is also NO_TARGETS (parse-safe on an empty decision)
    assert greencheck.evaluate({}, {}) == "NO_TARGETS"


def test_red_list_is_sorted_and_capped_at_eight():
    targets = [f"test_p{i}[x]" for i in range(10)]
    decision = {"fix_targets": targets}
    token = greencheck.evaluate(decision, _reprobe())  # none present -> all red
    assert token.startswith("RED:")
    reds = token[len("RED:") :].split(";")
    assert reds == sorted(targets)[:8]
    assert len(reds) == 8
