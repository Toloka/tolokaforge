"""Pure agreement metrics for rubric-judge calibration — no LLM, no IO.

Stage 6 of ``docs/RUBRIC_GRADING_DESIGN.md``: a rubric is NOT trustworthy until it
clears an agreement gate. This module is the *separable, unit-testable* core:
given the judge's per-criterion verdicts and the human ("golden") labels, it
computes per-criterion agreement (accuracy + Cohen's κ), extracts the concrete
disagreements, and applies the trust-gate threshold decision.

Everything here operates on already-collected verdicts — the LLM call lives in
``runner.py``. That separation is deliberate: the metric maths is the part most
worth pinning with deterministic tests, so it must not depend on inference.

**Binarisation.** Agreement is measured on the *binary met/not-met* verdict per
criterion. For a ``binary`` criterion that is the literal verdict; for a
``graded`` criterion both the judge's ``score`` and the human's ``score`` are
thresholded at :data:`GRADED_MET_THRESHOLD` (the same 0.5 bar the runner-side
``rubric.py`` uses for the required-gate) into a met/not-met label. Cohen's κ is
defined on categorical labels, so a single consistent binarisation keeps accuracy
and κ talking about the same thing.

**ERRORED fixtures fail loud.** A fixture whose judge run errored contributes NO
paired verdicts (you cannot agree with a verdict that does not exist) and is
recorded as a calibration *failure*. The trust gate fails if any fixture errored,
independently of the agreement numbers — a judge that crashes is not shippable
even if the fixtures it *did* grade agreed perfectly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Threshold at which a graded score counts as "met". Mirrors
#: ``tolokaforge.core.grading.rubric.GRADED_MET_THRESHOLD`` (0.5). Duplicated as a
#: plain constant so this pure module has no runner-side import; keep in sync.
GRADED_MET_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Inputs — one paired observation per (fixture, criterion)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionObservation:
    """One human-vs-judge paired verdict for a single criterion of one fixture.

    ``expected_met`` / ``judged_met`` are the binarised labels agreement is
    computed on. ``expected_raw`` / ``judged_raw`` keep the pre-binarisation
    values (bool for binary, float for graded) so disagreement reports can show
    what the labels actually were. ``justification`` is the judge's own reasoning
    for this criterion, surfaced in disagreement triage.
    """

    fixture_id: str
    criterion_id: str
    expected_met: bool
    judged_met: bool
    expected_raw: object = None
    judged_raw: object = None
    justification: str = ""

    @property
    def agrees(self) -> bool:
        return self.expected_met == self.judged_met


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Disagreement:
    """A single (fixture, criterion) where the judge disagreed with the human."""

    fixture_id: str
    criterion_id: str
    expected_met: bool
    judged_met: bool
    expected_raw: object
    judged_raw: object
    justification: str


@dataclass(frozen=True)
class CriterionAgreement:
    """Per-criterion agreement aggregated across all fixtures that have it.

    ``kappa`` is ``None`` when it is undefined: fewer than two observations, or a
    degenerate confusion matrix where one rater never varies AND they agree on
    everything (chance agreement = 1, so κ's ``(p_o - p_e)/(1 - p_e)`` divides by
    zero). The convention here: perfect agreement with no label variation reports
    ``accuracy == 1.0`` and ``kappa is None`` (κ cannot be computed, not 1.0) —
    callers must not silently treat ``None`` as agreement.
    """

    criterion_id: str
    n: int
    accuracy: float
    kappa: float | None


@dataclass(frozen=True)
class CalibrationReport:
    """Full calibration outcome over a set of fixtures."""

    per_criterion: tuple[CriterionAgreement, ...]
    disagreements: tuple[Disagreement, ...]
    errored_fixture_ids: tuple[str, ...]
    overall_accuracy: float
    overall_kappa: float | None
    total_observations: int

    @property
    def has_errors(self) -> bool:
        return bool(self.errored_fixture_ids)


# ---------------------------------------------------------------------------
# Core maths
# ---------------------------------------------------------------------------


def binarise(raw: bool | int | float, *, is_graded: bool) -> bool:
    """Binarise a raw verdict into a met/not-met label.

    Binary verdicts pass through; graded scores are thresholded at
    :data:`GRADED_MET_THRESHOLD`. Booleans are accepted for graded too (a human
    may label a graded criterion as a flat pass/fail).
    """
    if isinstance(raw, bool):
        return raw
    if not is_graded:
        raise ValueError(f"Binary criterion verdict must be a bool, got {type(raw).__name__}.")
    if not isinstance(raw, (int, float)):
        raise ValueError(f"Graded criterion verdict must be numeric, got {type(raw).__name__}.")
    return float(raw) >= GRADED_MET_THRESHOLD


def accuracy(observations: list[CriterionObservation]) -> float:
    """Fraction of observations where the judge and human agree.

    Raises on an empty input — an accuracy over zero observations is meaningless,
    and silently returning 0/1 would hide a calibration set that produced no
    pairs (e.g. every fixture errored). Fail loud (AGENTS rule 1).
    """
    if not observations:
        raise ValueError("Cannot compute accuracy over zero observations.")
    agree = sum(1 for o in observations if o.agrees)
    return agree / len(observations)


def cohen_kappa(observations: list[CriterionObservation]) -> float | None:
    """Cohen's κ for the binary met/not-met labels — chance-corrected agreement.

    κ = (p_o - p_e) / (1 - p_e), where ``p_o`` is observed agreement and ``p_e``
    is the agreement expected by chance from each rater's marginal label rates.

    Returns ``None`` when κ is undefined: fewer than two observations, or
    ``p_e == 1`` (a rater never varies and the two agree everywhere — the
    chance-correction denominator is zero). Perfect disagreement with balanced
    marginals yields the standard ``κ = -1``; perfect agreement with variation
    yields ``κ = 1``. Callers must treat ``None`` distinctly from a numeric κ.
    """
    n = len(observations)
    if n < 2:
        return None

    # Confusion counts over (expected, judged) ∈ {(T,T),(T,F),(F,T),(F,F)}.
    tt = sum(1 for o in observations if o.expected_met and o.judged_met)
    tf = sum(1 for o in observations if o.expected_met and not o.judged_met)
    ft = sum(1 for o in observations if not o.expected_met and o.judged_met)
    ff = sum(1 for o in observations if not o.expected_met and not o.judged_met)

    p_o = (tt + ff) / n

    # Marginal probabilities for "met" from each rater.
    expected_met_rate = (tt + tf) / n
    judged_met_rate = (tt + ft) / n
    p_e = expected_met_rate * judged_met_rate + (1 - expected_met_rate) * (1 - judged_met_rate)

    if p_e >= 1.0:
        # No variation and full agreement → chance agreement is total; κ undefined.
        return None
    return (p_o - p_e) / (1 - p_e)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _group_by_criterion(
    observations: list[CriterionObservation],
) -> dict[str, list[CriterionObservation]]:
    grouped: dict[str, list[CriterionObservation]] = {}
    for obs in observations:
        grouped.setdefault(obs.criterion_id, []).append(obs)
    return grouped


def extract_disagreements(observations: list[CriterionObservation]) -> list[Disagreement]:
    """Pull out every (fixture, criterion) where the judge differs from the human."""
    return [
        Disagreement(
            fixture_id=o.fixture_id,
            criterion_id=o.criterion_id,
            expected_met=o.expected_met,
            judged_met=o.judged_met,
            expected_raw=o.expected_raw,
            judged_raw=o.judged_raw,
            justification=o.justification,
        )
        for o in observations
        if not o.agrees
    ]


def build_report(
    observations: list[CriterionObservation],
    errored_fixture_ids: list[str],
) -> CalibrationReport:
    """Aggregate paired observations + errored fixtures into a full report.

    ``observations`` are the pairs from fixtures the judge graded successfully;
    ``errored_fixture_ids`` are fixtures whose judge run errored (counted as
    calibration failures, contributing no pairs). A report with zero observations
    is allowed *only* when every fixture errored — that is itself a meaningful
    (failing) calibration outcome; per-criterion and overall metrics are then
    empty / ``None`` rather than raising.
    """
    grouped = _group_by_criterion(observations)
    per_criterion = tuple(
        CriterionAgreement(
            criterion_id=cid,
            n=len(obs),
            accuracy=accuracy(obs),
            kappa=cohen_kappa(obs),
        )
        for cid, obs in sorted(grouped.items())
    )

    overall_accuracy = accuracy(observations) if observations else 0.0
    overall_kappa = cohen_kappa(observations) if observations else None

    return CalibrationReport(
        per_criterion=per_criterion,
        disagreements=tuple(extract_disagreements(observations)),
        errored_fixture_ids=tuple(errored_fixture_ids),
        overall_accuracy=overall_accuracy,
        overall_kappa=overall_kappa,
        total_observations=len(observations),
    )


# ---------------------------------------------------------------------------
# Trust gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """The trust-gate verdict — whether the rubric is shippable.

    The gate fails (``shippable == False``) if EITHER any fixture errored OR the
    chosen agreement metric is below ``threshold``. ``reasons`` lists every
    failing condition so the operator sees all of them, not just the first.
    """

    shippable: bool
    threshold: float
    metric: str
    observed: float | None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def decide_gate(
    report: CalibrationReport,
    *,
    threshold: float,
    metric: str = "kappa",
) -> GateDecision:
    """Apply the trust gate to a calibration report.

    ``metric`` selects which overall number is gated: ``"kappa"`` (default —
    chance-corrected, the stronger signal) or ``"accuracy"``. An errored fixture
    is an unconditional failure. A ``None`` overall metric (κ undefined, or no
    observations) cannot clear a numeric bar, so it also fails — surfaced
    explicitly rather than treated as a pass.

    Both metrics gate the *binarised* met/not-met labels (graded scores are
    thresholded at :data:`GRADED_MET_THRESHOLD`), so the gate measures label
    agreement, NOT graded-score magnitude — it does not catch graded-magnitude
    drift (judge 0.6 vs human 0.9 both count as "met"). A per-graded MAE metric
    would be the future addition for that.
    """
    if metric not in ("kappa", "accuracy"):
        raise ValueError(f"Unknown gate metric {metric!r}; expected 'kappa' or 'accuracy'.")

    observed = report.overall_kappa if metric == "kappa" else report.overall_accuracy
    reasons: list[str] = []

    if report.has_errors:
        reasons.append(
            f"{len(report.errored_fixture_ids)} fixture(s) errored during judging: "
            f"{', '.join(report.errored_fixture_ids)}"
        )

    if observed is None:
        reasons.append(
            f"overall {metric} is undefined (no variation / no observations) — "
            "cannot clear the agreement threshold"
        )
    elif observed < threshold:
        reasons.append(f"overall {metric} {observed:.3f} is below threshold {threshold:.3f}")

    return GateDecision(
        shippable=not reasons,
        threshold=threshold,
        metric=metric,
        observed=observed,
        reasons=tuple(reasons),
    )
