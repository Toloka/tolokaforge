"""``CompositeGraderKind`` — reference impl over the shared composite fold.

Reference implementation of the composite grader kind: a thin wrapper over
:class:`~tolokaforge.core.grading.composite_fold.CompositeFold` that folds
pre-computed sub-component scores into a :class:`Grade`. The runner-side
composite dispatch at :meth:`RunnerServiceImpl._grade_trial_async` remains
the shipped production path; this class demonstrates the seam so a
downstream adapter can register its own composite variant.

The kind reads ``kind_config["components"]`` as the pre-computed
sub-component score dict (keys the composite fold consumes:
``hash_score``, ``jsonpath_score``, ``db_probe_score``, ``transcript_score``,
``trace_checks_score``, ``llm_judge_score``, ``custom_checks_score`` — the
runner-side fold's own field set). A caller migrating a runtime dispatch
through this reference resolves each sub-component through
``plugin_registry`` and passes the scores in via ``kind_config``; the kind
never reaches for the plugin registry itself, keeping the
``kinds/`` package free of runner / plug-in-registry imports.

Empty active set (no component with score ≥ 0) → ``evaluate`` returns
``None``, matching the composite dispatch's empty-active-set semantics.
Any :class:`SubstrateUnreachableError` from the substrate reads propagates
verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.composite_fold import CompositeFold
from tolokaforge.core.grading.kinds._protocol import GraderKindRefusedError
from tolokaforge.core.models.grade import Grade
from tolokaforge.core.models.grade_components import GradeComponents

if TYPE_CHECKING:
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.runner.models import RunnerGradingConfig

__all__ = ["CompositeGraderKind"]


_STATE_CHECK_SCORE_KEYS: tuple[str, ...] = ("hash_score", "jsonpath_score", "db_probe_score")
_COMPONENT_SCORE_KEYS: tuple[str, ...] = (
    "transcript_score",
    "trace_checks_score",
    "llm_judge_score",
    "custom_checks_score",
)


class CompositeGraderKind:
    """Reference composite grader kind — folds pre-computed component scores."""

    NAME: ClassVar[str] = "composite"

    def evaluate(
        self,
        *,
        substrate: GradingSubstrate,
        task_config: RunnerGradingConfig,
        kind_config: Mapping[str, Any] | None,
        trial_id: str,  # noqa: ARG002
        agent_tools: Mapping[str, Any],  # noqa: ARG002
        logger: StructuredLogger,  # noqa: ARG002
    ) -> Grade | None:
        # Touch the substrate so a SubstrateUnreachableError from an
        # unreachable topology propagates through this seam — the
        # composite grader's substrate contract.
        substrate.final_state()

        components_dict: dict[str, Any] = dict((kind_config or {}).get("components") or {})
        if not _any_active(components_dict):
            return None

        grading_dict = task_config.model_dump() if task_config is not None else {}
        state_config = grading_dict.get("state_checks") or {}
        hash_weight = state_config.get("hash_weight") if isinstance(state_config, Mapping) else None

        result = CompositeFold.finalise(
            components_dict=components_dict,
            grading_config_dict=grading_dict,
            hash_weight=hash_weight,
            judge_gate_failed=False,
            trace_gate_failed=False,
        )
        if result.refusal:
            raise GraderKindRefusedError(result.verdict_reason or "composite fold refused")
        return Grade(
            binary_pass=result.binary_pass,
            score=result.score,
            components=GradeComponents(
                state_checks=result.state_checks_component,
                transcript_rules=_score_or_none(components_dict.get("transcript_score")),
                trace_checks=_score_or_none(components_dict.get("trace_checks_score")),
                llm_judge=_score_or_none(components_dict.get("llm_judge_score")),
                custom_checks=_score_or_none(components_dict.get("custom_checks_score")),
            ),
            reasons=result.reasons,
        )


def _any_active(components: Mapping[str, Any]) -> bool:
    for key in (*_STATE_CHECK_SCORE_KEYS, *_COMPONENT_SCORE_KEYS):
        value = components.get(key)
        if isinstance(value, int | float) and value >= 0:
            return True
    return False


def _score_or_none(value: Any) -> float | None:
    if isinstance(value, int | float) and value >= 0:
        return float(value)
    return None
