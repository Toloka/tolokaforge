"""Robust aggregators over K per-sample rubric verdicts.

Pure math — no coupling to :class:`JudgeKind`, :class:`LLMJudge`, or any
LLM-dispatch surface. Shared by :class:`VotedRubricJudgeKind` (this issue)
and ``jury_rubric`` (#1602, cross-model panels) unchanged.

``per_sample_scores`` is always ``n_samples`` rows × ``n_criteria`` columns,
one row per K-sampled judge verdict, columns ordered by the rubric's
criteria. ``"median"`` aggregates each column (criterion) independently.
``"geometric_median"`` treats each row as one point in R^n_criteria and
returns the point minimizing the sum of Euclidean distances to the K
sample points (Weiszfeld/Vardi-Zhang), so one sample whose *entire*
verdict set is contaminated (sycophancy, mode collapse) is down-weighted
as a unit rather than diluted criterion-by-criterion — the property that
makes it distinct from ``"median"`` (whose 1-D geometric median is
algebraically identical to the per-criterion median).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tolokaforge.runner.models import Criterion

__all__ = [
    "AGGREGATOR_NAMES",
    "CONVERGENCE_TOLERANCE",
    "MAX_ITERATIONS",
    "aggregate_scores",
    "require_binary_only",
    "validate_aggregator_name",
    "validate_sample_count",
]

#: Accepted ``aggregator`` values; anything else raises via :func:`validate_aggregator_name`.
AGGREGATOR_NAMES: frozenset[str] = frozenset({"majority", "median", "geometric_median"})

#: Weiszfeld iteration cap for ``"geometric_median"`` — exceeding it without
#: converging is a fail-loud :class:`RuntimeError`, never a stale midpoint.
MAX_ITERATIONS = 100

#: Convergence is declared once consecutive iterates move less than this
#: (Euclidean distance) between steps.
CONVERGENCE_TOLERANCE = 1e-8

#: Below this distance from the current iterate, a sample point is treated as
#: coincident with it (the Vardi-Zhang degenerate-iterate case), avoiding a
#: division by (near-)zero distance.
_COINCIDENCE_EPSILON = 1e-9


def validate_aggregator_name(name: str) -> None:
    """Raise ``ValueError`` naming ``name`` and the accepted set unless it is a known aggregator."""
    if name not in AGGREGATOR_NAMES:
        raise ValueError(f"unknown aggregator {name!r}; must be one of {sorted(AGGREGATOR_NAMES)}")


def validate_sample_count(n_samples: int, *, aggregator: str) -> None:
    """Raise ``ValueError`` if ``n_samples`` is undersized, or even under ``majority``."""
    if n_samples < 2:
        raise ValueError(f"n_samples={n_samples} is invalid: K<2 makes voting undefined")
    if aggregator == "majority" and n_samples % 2 == 0:
        raise ValueError(
            f"aggregator='majority' requires an odd n_samples to avoid ties, got {n_samples}"
        )


def require_binary_only(criteria: Sequence[Criterion]) -> None:
    """Raise ``ValueError`` naming every non-``binary`` criterion id.

    Callers invoke this only when ``aggregator == "majority"`` — a majority
    vote over a graded (0-1 gradient) criterion has no defined threshold.
    """
    non_binary_ids = [c.id for c in criteria if c.kind != "binary"]
    if non_binary_ids:
        raise ValueError(
            f"aggregator='majority' requires an all-binary rubric; "
            f"non-binary criteria: {non_binary_ids}"
        )


def aggregate_scores(
    *, aggregator: str, per_sample_scores: Sequence[Sequence[float]]
) -> list[float]:
    """Fold ``per_sample_scores`` (K samples × N criteria) into one score per criterion.

    Raises ``RuntimeError`` (not a silent stale midpoint) if
    ``aggregator == "geometric_median"`` fails to converge within
    ``MAX_ITERATIONS`` at ``CONVERGENCE_TOLERANCE``.
    """
    validate_aggregator_name(aggregator)
    if aggregator == "majority":
        return _aggregate_majority(per_sample_scores)
    if aggregator == "median":
        return _aggregate_median(per_sample_scores)
    return _aggregate_geometric_median(per_sample_scores)


def _aggregate_majority(per_sample_scores: Sequence[Sequence[float]]) -> list[float]:
    n_samples = len(per_sample_scores)
    n_criteria = len(per_sample_scores[0]) if per_sample_scores else 0
    result: list[float] = []
    for j in range(n_criteria):
        votes_met = sum(1 for row in per_sample_scores if row[j] >= 0.5)
        if votes_met * 2 == n_samples:
            raise ValueError(
                f"majority vote tied on criterion index {j} with n_samples={n_samples}; "
                "caller must guarantee an odd n_samples via validate_sample_count"
            )
        result.append(1.0 if votes_met * 2 > n_samples else 0.0)
    return result


def _aggregate_median(per_sample_scores: Sequence[Sequence[float]]) -> list[float]:
    n_criteria = len(per_sample_scores[0]) if per_sample_scores else 0
    return [statistics.median(row[j] for row in per_sample_scores) for j in range(n_criteria)]


def _euclidean_distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b, strict=True)))


def _aggregate_geometric_median(per_sample_scores: Sequence[Sequence[float]]) -> list[float]:
    points = [list(row) for row in per_sample_scores]
    n_criteria = len(points[0]) if points else 0
    if n_criteria == 0:
        return []

    y = [sum(p[j] for p in points) / len(points) for j in range(n_criteria)]

    for _ in range(MAX_ITERATIONS):
        distances = [_euclidean_distance(y, p) for p in points]
        coincident = [i for i, d in enumerate(distances) if d < _COINCIDENCE_EPSILON]
        if len(coincident) == len(points):
            return y

        non_coincident = [i for i in range(len(points)) if i not in coincident]
        inv_distances = [1.0 / distances[i] for i in non_coincident]
        weight_sum = sum(inv_distances)
        t_y = [
            sum(points[i][j] * w for i, w in zip(non_coincident, inv_distances, strict=True))
            / weight_sum
            for j in range(n_criteria)
        ]

        eta = len(coincident)
        if eta == 0:
            y_next = t_y
        else:
            r_vector = [
                sum((points[i][j] - y[j]) / distances[i] for i in non_coincident)
                for j in range(n_criteria)
            ]
            r_norm = math.sqrt(sum(v * v for v in r_vector))
            if r_norm < _COINCIDENCE_EPSILON:
                # The pull from every non-coincident point cancels out — y is
                # already the minimizer (Vardi-Zhang stationarity test).
                return y
            gamma = min(1.0, eta / r_norm)
            y_next = [max(0.0, 1.0 - gamma) * t_y[j] + gamma * y[j] for j in range(n_criteria)]

        movement = _euclidean_distance(y, y_next)
        y = y_next
        if movement < CONVERGENCE_TOLERANCE:
            return y

    raise RuntimeError(
        f"geometric_median failed to converge within MAX_ITERATIONS={MAX_ITERATIONS} "
        f"iterations at CONVERGENCE_TOLERANCE={CONVERGENCE_TOLERANCE}; last iterate={y}"
    )
