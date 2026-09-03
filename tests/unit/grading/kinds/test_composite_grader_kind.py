"""``CompositeGraderKind`` — reference-impl fold behaviour.

The composite kind is a reference impl over
:class:`~tolokaforge.core.grading.composite_fold.CompositeFold` — it folds
caller-supplied sub-component scores into a :class:`Grade` without
resolving the sub-component seams itself. The runner-side production
composite path (:meth:`RunnerServiceImpl._grade_trial_async`) is untouched
by this stage. These tests lock the seam's three invariants:

1. Given a task with ``weights: {state_checks: 1.0}`` and a pre-computed
   ``jsonpath_score`` in ``kind_config['components']``, the kind's returned
   ``Grade.score`` matches what
   :func:`~tolokaforge.core.grading.composite_fold.combine_grade_components`
   produces standalone.
2. A task with no scoring components → ``evaluate`` returns ``None``.
3. :class:`SubstrateUnreachableError` from a substrate read propagates
   verbatim through the kind.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.composite_fold import combine_grade_components
from tolokaforge.core.grading.kinds import CompositeGraderKind
from tolokaforge.core.grading.substrate import (
    InProcessGradingSubstrate,
    SubstrateUnreachableError,
)
from tolokaforge.runner.models import RunnerGradingConfig, RunnerStateChecksConfig

pytestmark = pytest.mark.unit


def _substrate(final_state: dict | None = None) -> InProcessGradingSubstrate:
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state=final_state if final_state is not None else {},
    )


def _logger() -> logging.Logger:
    return logging.getLogger("test-composite-grader-kind")


def test_composite_kind_scores_match_combine_grade_components_standalone() -> None:
    task_config = RunnerGradingConfig(
        combine_method="weighted",
        weights={"state_checks": 1.0},
        pass_threshold=0.5,
        state_checks=RunnerStateChecksConfig(jsonpath_checks=[]),
    )
    kind_config = {"components": {"jsonpath_score": 0.75}}

    grade = CompositeGraderKind().evaluate(
        substrate=_substrate(),
        task_config=task_config,
        kind_config=kind_config,
        trial_id="task:0",
        agent_tools={},
        logger=_logger(),  # type: ignore[arg-type]
    )
    reference = combine_grade_components(
        {"jsonpath_score": 0.75},
        task_config.model_dump(),
    )

    assert grade is not None
    assert grade.score == pytest.approx(reference.score)
    assert grade.binary_pass == reference.binary_pass
    assert grade.components.state_checks == pytest.approx(0.75)


def test_composite_kind_returns_none_when_no_active_components() -> None:
    task_config = RunnerGradingConfig(combine_method="weighted", weights={}, pass_threshold=0.5)
    grade = CompositeGraderKind().evaluate(
        substrate=_substrate(),
        task_config=task_config,
        kind_config=None,
        trial_id="task:0",
        agent_tools={},
        logger=_logger(),  # type: ignore[arg-type]
    )
    assert grade is None


def test_composite_kind_reraises_substrate_unreachable_verbatim() -> None:
    class _UnreachableSubstrate:
        def final_state(self) -> dict:
            raise SubstrateUnreachableError("live grader lost the runner")

    task_config = RunnerGradingConfig(
        combine_method="weighted",
        weights={"state_checks": 1.0},
        pass_threshold=0.5,
    )
    with pytest.raises(SubstrateUnreachableError, match="live grader lost the runner"):
        CompositeGraderKind().evaluate(
            substrate=_UnreachableSubstrate(),  # type: ignore[arg-type]
            task_config=task_config,
            kind_config={"components": {"jsonpath_score": 0.5}},
            trial_id="task:0",
            agent_tools={},
            logger=_logger(),  # type: ignore[arg-type]
        )
