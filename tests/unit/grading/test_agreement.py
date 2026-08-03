"""Unit tests for tolokaforge/core/grading/agreement.py — pure agreement maths.

Pins the numbers every agreement decision rests on: accuracy, Cohen's κ
(including the chance-correction edge cases), disagreement extraction, errored
source → failure, and the threshold gate. Tested deterministically with
hand-built observations — no LLM.

The load-bearing case is κ on a **label-invariant** corpus: it is ``None`` while
accuracy reads ``1.0``, so "perfect agreement" and "no evidence" are the same
input and only κ tells them apart.
"""

import math

import pytest

from tolokaforge.core.grading import rubric
from tolokaforge.core.grading.agreement import (
    GRADED_MET_THRESHOLD,
    CriterionObservation,
    accuracy,
    binarise,
    build_report,
    cohen_kappa,
    decide_gate,
    extract_disagreements,
)

pytestmark = pytest.mark.unit


def _obs(observation, criterion, reference, candidate, *, raw_r=None, raw_c=None, just=""):
    return CriterionObservation(
        observation_id=observation,
        criterion_id=criterion,
        reference_met=reference,
        candidate_met=candidate,
        reference_raw=reference if raw_r is None else raw_r,
        candidate_raw=candidate if raw_c is None else raw_c,
        justification=just,
    )


# ---------------------------------------------------------------------------
# The threshold is single-sourced
# ---------------------------------------------------------------------------


def test_graded_met_threshold_is_the_engine_constant():
    assert GRADED_MET_THRESHOLD is rubric.GRADED_MET_THRESHOLD


# ---------------------------------------------------------------------------
# binarise
# ---------------------------------------------------------------------------


def test_binarise_graded_threshold():
    assert binarise(0.5, is_graded=True) is True
    assert binarise(0.49, is_graded=True) is False
    assert binarise(1.0, is_graded=True) is True


def test_binarise_binary_passthrough():
    assert binarise(True, is_graded=False) is True
    assert binarise(False, is_graded=False) is False


def test_binarise_binary_rejects_number():
    with pytest.raises(ValueError):
        binarise(0.7, is_graded=False)


# ---------------------------------------------------------------------------
# accuracy
# ---------------------------------------------------------------------------


def test_accuracy_perfect():
    obs = [_obs("f1", "c", True, True), _obs("f2", "c", False, False)]
    assert accuracy(obs) == 1.0


def test_accuracy_half():
    obs = [_obs("f1", "c", True, True), _obs("f2", "c", True, False)]
    assert accuracy(obs) == 0.5


def test_accuracy_empty_raises():
    with pytest.raises(ValueError):
        accuracy([])


# ---------------------------------------------------------------------------
# Cohen's kappa — including the chance-correction edge cases
# ---------------------------------------------------------------------------


def test_kappa_perfect_agreement_with_variation_is_one():
    obs = [
        _obs("f1", "c", True, True),
        _obs("f2", "c", False, False),
        _obs("f3", "c", True, True),
        _obs("f4", "c", False, False),
    ]
    assert cohen_kappa(obs) == pytest.approx(1.0)


def test_kappa_total_disagreement_balanced_is_minus_one():
    # Both labellers use both labels equally but always disagree → κ = -1.
    obs = [
        _obs("f1", "c", True, False),
        _obs("f2", "c", False, True),
        _obs("f3", "c", True, False),
        _obs("f4", "c", False, True),
    ]
    assert cohen_kappa(obs) == pytest.approx(-1.0)


def test_kappa_undefined_when_no_variation_and_full_agreement():
    # Both labellers say True everywhere: p_e == 1, κ undefined → None.
    obs = [_obs("f1", "c", True, True), _obs("f2", "c", True, True)]
    assert cohen_kappa(obs) is None


def test_kappa_undefined_below_two_observations():
    assert cohen_kappa([_obs("f1", "c", True, True)]) is None
    assert cohen_kappa([]) is None


def test_one_sided_corpus_is_perfect_accuracy_and_no_kappa_evidence():
    # Five sources, every label not-met on both sides: accuracy cannot distinguish
    # this from evidence of agreement, and κ is the only signal that does.
    obs = [_obs(f"t{i}", "c", False, False) for i in range(5)]
    assert accuracy(obs) == 1.0
    assert cohen_kappa(obs) is None
    report = build_report(obs, errored_fixture_ids=[])
    assert report.total_observations == 5
    assert decide_gate(report, threshold=0.6, metric="accuracy").shippable is True
    assert decide_gate(report, threshold=0.6, metric="kappa").shippable is False


def test_kappa_chance_level_near_zero():
    # Candidate agrees at exactly the chance rate → κ ≈ 0. Reference: 2T/2F;
    # candidate picks T,F,T,F independent of it, giving 2 agreements out of 4
    # (p_o=0.5), with marginals 0.5/0.5 → p_e=0.5 → κ=0.
    obs = [
        _obs("f1", "c", True, True),
        _obs("f2", "c", True, False),
        _obs("f3", "c", False, True),
        _obs("f4", "c", False, False),
    ]
    k = cohen_kappa(obs)
    assert k is not None and math.isclose(k, 0.0, abs_tol=1e-9)


def test_kappa_is_blind_to_disagreement_direction():
    # Balanced n=7 with exactly one disagreement, permissive vs strict: κ is
    # identical, so a κ threshold cannot protect against one direction.
    balanced = [
        _obs("f1", "c", True, True),
        _obs("f2", "c", True, True),
        _obs("f3", "c", True, True),
        _obs("f4", "c", False, False),
        _obs("f5", "c", False, False),
        _obs("f6", "c", False, False),
    ]
    permissive = [*balanced, _obs("f7", "c", False, True)]
    strict = [*balanced, _obs("f7", "c", True, False)]
    assert cohen_kappa(permissive) == pytest.approx(cohen_kappa(strict))
    assert cohen_kappa(permissive) == pytest.approx(0.720, abs=5e-4)


# ---------------------------------------------------------------------------
# disagreement extraction
# ---------------------------------------------------------------------------


def test_extract_disagreements_only_mismatches():
    obs = [
        _obs("f1", "c1", True, True),
        _obs("f2", "c2", True, False, raw_r=0.9, raw_c=0.2, just="candidate saw nothing"),
    ]
    dis = extract_disagreements(obs)
    assert len(dis) == 1
    assert dis[0].observation_id == "f2"
    assert dis[0].criterion_id == "c2"
    assert dis[0].reference_met is True
    assert dis[0].candidate_met is False
    assert dis[0].reference_raw == 0.9
    assert dis[0].candidate_raw == 0.2
    assert dis[0].justification == "candidate saw nothing"


# ---------------------------------------------------------------------------
# build_report — per-criterion aggregation + errored sources
# ---------------------------------------------------------------------------


def test_build_report_per_criterion_and_overall():
    obs = [
        _obs("f1", "refund", True, True),
        _obs("f2", "refund", False, False),
        _obs("f1", "tone", True, False),
        _obs("f2", "tone", True, True),
    ]
    report = build_report(obs, errored_fixture_ids=[])
    by_crit = {c.criterion_id: c for c in report.per_criterion}
    assert by_crit["refund"].accuracy == 1.0
    assert by_crit["tone"].accuracy == 0.5
    assert report.total_observations == 4
    assert report.overall_accuracy == 0.75
    assert not report.has_errors


def test_build_report_errored_source_counts_as_failure():
    obs = [_obs("f1", "c", True, True)]
    report = build_report(obs, errored_fixture_ids=["f2"])
    assert report.has_errors
    assert report.errored_fixture_ids == ("f2",)
    # The errored source contributes no observations.
    assert report.total_observations == 1


def test_build_report_all_errored_no_observations():
    report = build_report([], errored_fixture_ids=["f1", "f2"])
    assert report.total_observations == 0
    assert report.overall_accuracy == 0.0
    assert report.overall_kappa is None
    assert report.per_criterion == ()
    assert report.has_errors


# ---------------------------------------------------------------------------
# trust gate
# ---------------------------------------------------------------------------


def _report_with_kappa(kappa_target_pass: bool):
    if kappa_target_pass:
        obs = [_obs("f1", "c", True, True), _obs("f2", "c", False, False)]
    else:
        obs = [_obs("f1", "c", True, False), _obs("f2", "c", False, True)]
    return build_report(obs, errored_fixture_ids=[])


def test_gate_passes_above_threshold():
    report = _report_with_kappa(kappa_target_pass=True)  # κ=1.0
    gate = decide_gate(report, threshold=0.6, metric="kappa")
    assert gate.shippable is True
    assert gate.reasons == ()
    assert gate.observed == pytest.approx(1.0)


def test_gate_fails_below_threshold():
    report = _report_with_kappa(kappa_target_pass=False)  # κ=-1.0
    gate = decide_gate(report, threshold=0.6, metric="kappa")
    assert gate.shippable is False
    assert any("below threshold" in r for r in gate.reasons)


def test_gate_fails_on_errored_source_even_with_perfect_agreement():
    report = build_report(
        [_obs("f1", "c", True, True), _obs("f1", "c2", False, False)],
        errored_fixture_ids=["f2"],
    )
    gate = decide_gate(report, threshold=0.0, metric="accuracy")
    assert gate.shippable is False
    assert any("errored" in r for r in gate.reasons)


def test_gate_fails_when_metric_undefined():
    # No variation + full agreement → κ undefined → cannot clear a numeric bar.
    report = build_report(
        [_obs("f1", "c", True, True), _obs("f2", "c", True, True)],
        errored_fixture_ids=[],
    )
    gate = decide_gate(report, threshold=0.6, metric="kappa")
    assert gate.shippable is False
    assert any("undefined" in r for r in gate.reasons)


def test_gate_accuracy_metric():
    report = build_report(
        [_obs("f1", "c", True, True), _obs("f2", "c", True, False)],  # acc=0.5
        errored_fixture_ids=[],
    )
    assert decide_gate(report, threshold=0.4, metric="accuracy").shippable is True
    assert decide_gate(report, threshold=0.6, metric="accuracy").shippable is False


def test_gate_rejects_unknown_metric():
    report = _report_with_kappa(kappa_target_pass=True)
    with pytest.raises(ValueError):
        decide_gate(report, threshold=0.5, metric="f1")
