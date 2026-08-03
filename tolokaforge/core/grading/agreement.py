"""Pure agreement maths over paired met/not-met labels — no LLM, no IO.

Given the same criterion labelled twice — once by a **reference** labeller and
once by a **candidate** labeller — this module computes agreement (accuracy +
Cohen's κ), extracts the concrete disagreements, and applies a threshold gate.

The two sides are named `reference` and `candidate`, not by what produces them:
the maths is identical whichever side is the judge. The rubric calibrator pairs a
human label (reference) against the judge's verdict (candidate).

**Binarisation.** Agreement is measured on the *binary met/not-met* verdict per
criterion. For a ``binary`` criterion that is the literal verdict; for a
``graded`` criterion both sides' scores are thresholded at
:data:`~tolokaforge.core.grading.rubric.GRADED_MET_THRESHOLD` — the same bar the
required-gate uses — into a met/not-met label. Cohen's κ is defined on
categorical labels, so a single consistent binarisation keeps accuracy and κ
talking about the same thing.

**κ undefined is never a pass.** A label-invariant corpus (one side never varies
and the two agree everywhere) has total chance agreement, so κ is ``None`` while
accuracy reads ``1.0``. Callers must treat ``None`` as *no evidence*, not as
agreement; :func:`decide_gate` fails on it explicitly.

**An errored source fails loud.** A source whose labelling errored contributes NO
paired observations (you cannot agree with a verdict that does not exist) and is
recorded as a failure. The gate fails if any source errored, independently of the
agreement numbers — a labeller that crashes is not shippable even if the sources
it did label agreed perfectly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tolokaforge.core.grading.rubric import GRADED_MET_THRESHOLD

__all__ = [
    "GRADED_MET_THRESHOLD",
    "CalibrationReport",
    "CriterionAgreement",
    "CriterionObservation",
    "Disagreement",
    "GateDecision",
    "accuracy",
    "binarise",
    "build_report",
    "cohen_kappa",
    "decide_gate",
    "extract_disagreements",
]


# ---------------------------------------------------------------------------
# Inputs — one paired observation per (source, criterion)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionObservation:
    """Two labels for a single criterion of one observed source.

    ``observation_id`` identifies the source the labels were taken from (a
    fixture, a trial bundle). ``reference_met`` / ``candidate_met`` are the
    binarised labels agreement is computed on; ``reference_raw`` /
    ``candidate_raw`` keep the pre-binarisation values (bool for binary, float
    for graded) so disagreement reports can show what the labels actually were.
    ``justification`` is the candidate labeller's own reasoning for this
    criterion, surfaced in disagreement triage.
    """

    observation_id: str
    criterion_id: str
    reference_met: bool
    candidate_met: bool
    reference_raw: object = None
    candidate_raw: object = None
    justification: str = ""

    @property
    def agrees(self) -> bool:
        return self.reference_met == self.candidate_met


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Disagreement:
    """A single (source, criterion) where the two labellers differ."""

    observation_id: str
    criterion_id: str
    reference_met: bool
    candidate_met: bool
    reference_raw: object
    candidate_raw: object
    justification: str


@dataclass(frozen=True)
class CriterionAgreement:
    """Per-criterion agreement aggregated across every source that has it.

    ``kappa`` is ``None`` when it is undefined: fewer than two observations, or a
    degenerate confusion matrix where one labeller never varies AND the two agree
    on everything (chance agreement = 1, so κ's ``(p_o - p_e)/(1 - p_e)`` divides
    by zero). The convention here: perfect agreement with no label variation
    reports ``accuracy == 1.0`` and ``kappa is None`` (κ cannot be computed, not
    1.0) — callers must not silently treat ``None`` as agreement.
    """

    criterion_id: str
    n: int
    accuracy: float
    kappa: float | None


@dataclass(frozen=True)
class CalibrationReport:
    """Full agreement outcome over a set of observed sources."""

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
    :data:`~tolokaforge.core.grading.rubric.GRADED_MET_THRESHOLD`. Booleans are
    accepted for graded too (a human may label a graded criterion as a flat
    pass/fail).
    """
    if isinstance(raw, bool):
        return raw
    if not is_graded:
        raise ValueError(f"Binary criterion verdict must be a bool, got {type(raw).__name__}.")
    if not isinstance(raw, (int, float)):
        raise ValueError(f"Graded criterion verdict must be numeric, got {type(raw).__name__}.")
    return float(raw) >= GRADED_MET_THRESHOLD


def accuracy(observations: list[CriterionObservation]) -> float:
    """Fraction of observations where the two labellers agree.

    Raises on an empty input — an accuracy over zero observations is meaningless,
    and silently returning 0/1 would hide a corpus that produced no pairs (e.g.
    every source errored). Fail loud (AGENTS rule 1).
    """
    if not observations:
        raise ValueError("Cannot compute accuracy over zero observations.")
    agree = sum(1 for o in observations if o.agrees)
    return agree / len(observations)


def cohen_kappa(observations: list[CriterionObservation]) -> float | None:
    """Cohen's κ for the binary met/not-met labels — chance-corrected agreement.

    κ = (p_o - p_e) / (1 - p_e), where ``p_o`` is observed agreement and ``p_e``
    is the agreement expected by chance from each labeller's marginal label rates.

    Returns ``None`` when κ is undefined: fewer than two observations, or
    ``p_e == 1`` (a labeller never varies and the two agree everywhere — the
    chance-correction denominator is zero). Perfect disagreement with balanced
    marginals yields the standard ``κ = -1``; perfect agreement with variation
    yields ``κ = 1``. Callers must treat ``None`` distinctly from a numeric κ.
    """
    n = len(observations)
    if n < 2:
        return None

    # Confusion counts over (reference, candidate) ∈ {(T,T),(T,F),(F,T),(F,F)}.
    tt = sum(1 for o in observations if o.reference_met and o.candidate_met)
    tf = sum(1 for o in observations if o.reference_met and not o.candidate_met)
    ft = sum(1 for o in observations if not o.reference_met and o.candidate_met)
    ff = sum(1 for o in observations if not o.reference_met and not o.candidate_met)

    p_o = (tt + ff) / n

    # Marginal probabilities for "met" from each labeller.
    reference_met_rate = (tt + tf) / n
    candidate_met_rate = (tt + ft) / n
    p_e = reference_met_rate * candidate_met_rate + (1 - reference_met_rate) * (
        1 - candidate_met_rate
    )

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
    """Pull out every (source, criterion) where the two labellers differ."""
    return [
        Disagreement(
            observation_id=o.observation_id,
            criterion_id=o.criterion_id,
            reference_met=o.reference_met,
            candidate_met=o.candidate_met,
            reference_raw=o.reference_raw,
            candidate_raw=o.candidate_raw,
            justification=o.justification,
        )
        for o in observations
        if not o.agrees
    ]


def build_report(
    observations: list[CriterionObservation],
    errored_fixture_ids: list[str],
) -> CalibrationReport:
    """Aggregate paired observations + errored sources into a full report.

    ``observations`` are the pairs from sources labelled successfully;
    ``errored_fixture_ids`` are sources whose labelling errored (counted as
    failures, contributing no pairs). A report with zero observations is allowed
    *only* when every source errored — that is itself a meaningful (failing)
    outcome; per-criterion and overall metrics are then empty / ``None`` rather
    than raising.
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
    """The trust-gate verdict — whether the labeller under test is shippable.

    The gate fails (``shippable == False``) if EITHER any source errored OR the
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
    """Apply the trust gate to an agreement report.

    ``metric`` selects which overall number is gated: ``"kappa"`` (default —
    chance-corrected, the stronger signal) or ``"accuracy"``. An errored source is
    an unconditional failure. A ``None`` overall metric (κ undefined, or no
    observations) cannot clear a numeric bar, so it also fails — surfaced
    explicitly rather than treated as a pass.

    Both metrics gate the *binarised* met/not-met labels (graded scores are
    thresholded at :data:`~tolokaforge.core.grading.rubric.GRADED_MET_THRESHOLD`),
    so the gate measures label agreement, NOT graded-score magnitude — it does not
    catch graded-magnitude drift (0.6 vs 0.9 both count as "met"). A per-graded MAE
    metric would be the future addition for that.

    The gate is blind to the *direction* of a disagreement: κ is identical for a
    permissive and a strict disagreement of the same count, so a decision that
    turns on which way the two labellers differ needs a second condition beside
    this one.
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
