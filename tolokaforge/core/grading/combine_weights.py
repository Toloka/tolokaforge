"""What a component's declared weight is required to be, and what a fold with no
weighted component decides instead of aggregating.

Substrate-neutral by construction: stdlib-only pure functions over component names
and weights, so the core grading engine and the runner reach the same verdict for a
configuration that counted nothing rather than each falling back to its own
arithmetic default. Kept beside :mod:`combine_method`, which decides the
aggregation once there is something to aggregate.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from tolokaforge.core.grading.combine_method import CombineMethod

_NO_DEFENSIBLE_WEIGHT = (
    "{name} was scored and combine.weights declares no weight for it, so the fold cannot "
    "say what share it carries — there is no defensible default, since 1.0 invents a share "
    "the author never gave it and 0.0 discards a verdict the substrate produced. Declare "
    "combine.weights.{name}, or stop configuring {name} so nothing scores it."
)

_NOTHING_COUNTED = "no scored component carries any weight, so the trial earned nothing: {because}"

_NOTHING_ASKED = (
    "no component was configured and no weight names one, so nothing was scored and "
    "nothing was owed"
)

_PRODUCED_NO_VERDICT = "{names} produced no verdict"

_WEIGHTS_SUM_TO_ZERO = (
    "combine.method: weighted averages the scored components by combine.weights and theirs "
    "sum to zero ({shares}), so there is nothing to average"
)

_WEIGHTS_NAME_NOTHING_CONFIGURED = (
    "combine.weights declares {keys} and the config asks for none of them, so no component "
    "was scored"
)


class MissingComponentWeight(ValueError):
    """A component a substrate scored whose share ``combine.weights`` does not declare."""


@dataclass(frozen=True)
class FoldedGrade:
    """A fold's verdict, and the sentence a fold that counted nothing owes the author.

    ``reason`` is ``None`` for every verdict an aggregation produced: those are already
    described by the components' own reasons. It is populated only where the fold decided
    without reading a score, because nothing else in the grade would then say why.
    """

    score: float
    binary_pass: bool
    reason: str | None = None


def require_component_weight(name: str, weights: Mapping[str, float]) -> float:
    """The declared share of a scored component, or raise naming it and both fixes.

    Neither substrate may pick a value. This is the fold's own guard rather than a
    consequence of the authoring gate refusing the shape: ``state_checks.hash`` is an
    untyped dict nothing stops a caller mutating after validation, and every unit test and
    the substrate differential construct a config directly rather than through YAML.
    """
    if name not in weights:
        raise MissingComponentWeight(_NO_DEFENSIBLE_WEIGHT.format(name=name))
    return weights[name]


def resolve_uncounted_fold(
    *,
    scored: Collection[str],
    requested: Collection[str],
    weights: Mapping[str, float],
    method: CombineMethod,
) -> FoldedGrade | None:
    """The verdict for a configuration no weighted component was scored under.

    ``None`` where the fold has something to aggregate and the caller's own aggregation
    decides. Two answers otherwise. A config asking for nothing *and* weighting nothing
    passes: nothing was asked for, so nothing is owed — the deliberately non-scoring pack.
    A config that asked for something, or weighted something, and counted none of it fails,
    with a reason naming what it asked for, because a bare ``0.0`` beside components that
    all read as passing is the defect this rule exists to remove.

    The zero-total-weight half is ``weighted``-only: ``all`` and ``any`` aggregate the
    component *set* and never read a weight, so a share of ``0.0`` under them is an inert
    key rather than a statement about the fold, and a component scored ``0.0`` there still
    has to pass.

    Every scored component's weight must already have been resolved through
    :func:`require_component_weight`, so a ``KeyError`` here is a caller that skipped that
    guard rather than a map to be defaulted around.
    """
    if not requested and not weights:
        return FoldedGrade(score=1.0, binary_pass=True, reason=_NOTHING_ASKED)
    if not _carries_no_weight(scored=scored, weights=weights, method=method):
        return None
    return FoldedGrade(
        score=0.0,
        binary_pass=False,
        reason=_NOTHING_COUNTED.format(
            because="; ".join(
                _why_nothing_counted(scored=scored, requested=requested, weights=weights)
            )
        ),
    )


def _carries_no_weight(
    *, scored: Collection[str], weights: Mapping[str, float], method: CombineMethod
) -> bool:
    if not scored:
        return True
    if method != "weighted":
        return False
    return sum(weights[name] for name in scored) == 0.0


def _why_nothing_counted(
    *, scored: Collection[str], requested: Collection[str], weights: Mapping[str, float]
) -> list[str]:
    """Every fact that holds about the fold, so no verdict here ever names nothing.

    At least one clause always applies: an unscored request produces the first, scored
    components whose shares cancel produce the second, and a config requesting nothing at
    all has weights (or :func:`resolve_uncounted_fold` returned the free pass) and produces
    the third.
    """
    clauses = []
    if unevaluated := sorted(set(requested) - set(scored)):
        clauses.append(_PRODUCED_NO_VERDICT.format(names=", ".join(unevaluated)))
    if scored:
        shares = ", ".join(f"{name}={weights[name]}" for name in sorted(scored))
        clauses.append(_WEIGHTS_SUM_TO_ZERO.format(shares=shares))
    if not requested:
        clauses.append(_WEIGHTS_NAME_NOTHING_CONFIGURED.format(keys=", ".join(sorted(weights))))
    return clauses
