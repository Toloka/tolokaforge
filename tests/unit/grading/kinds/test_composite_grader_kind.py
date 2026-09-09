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


# ---------------------------------------------------------------------------
# Full offline recompute mode — issue #1465. Kind reads task_description /
# trajectory / judge_model_config from the substrate (v1.1 bundle parts) and
# recomputes every sub-component; the fold reproduces what the runner's own
# composite dispatch would have produced.
# ---------------------------------------------------------------------------


def _minimal_task_description() -> dict:
    """Minimum ``TaskDescription`` shape for offline-recompute tests."""
    return {
        "task_id": "task-1",
        "name": "task-1",
        "category": "test",
        "description": "a task",
        "adapter_type": "native",
        "system_prompt": "You are a helpful assistant.",
        "initial_state": {},
    }


class _OfflineV11Substrate:
    """Snapshot-shaped substrate stub exposing v1.1 accessors non-None."""

    def __init__(
        self,
        *,
        task_description: dict,
        trajectory: dict | None = None,
        judge_model_config: dict | None = None,
    ) -> None:
        self._task_description = task_description
        self._trajectory = trajectory or {"messages": [], "termination_reason": "agent_done"}
        self._judge_model_config = judge_model_config

    def final_state(self) -> dict:
        return {}

    def initial_state(self) -> dict:
        return {}

    def final_state_stable(self) -> dict:
        return {}

    def filesystem_root(self) -> None:
        return None

    def filesystem_state(self) -> dict:
        return {}

    def knowledge_search(self) -> None:
        return None

    def db_reader(self) -> MagicMock:
        return MagicMock()

    def db_probe(self, dsn: str, query: str) -> list:
        raise SubstrateUnreachableError(f"db_probe offline (dsn={dsn!r})")

    def trajectory(self) -> dict:
        return self._trajectory

    def task_description(self) -> dict:
        return self._task_description

    def judge_model_config(self) -> dict | None:
        return self._judge_model_config


def test_offline_dispatch_refuses_hash_enabled_tasks() -> None:
    task_config = RunnerGradingConfig(
        combine_method="weighted",
        weights={"state_checks": 1.0},
        pass_threshold=0.5,
        state_checks=RunnerStateChecksConfig(hash_enabled=True, expect_initial_state=True),
    )
    substrate = _OfflineV11Substrate(task_description=_minimal_task_description())
    from tolokaforge.core.grading.kinds import GraderKindRefusedError

    with pytest.raises(GraderKindRefusedError, match="cannot execute hash-based grading"):
        CompositeGraderKind().evaluate(
            substrate=substrate,  # type: ignore[arg-type]
            task_config=task_config,
            kind_config=None,
            trial_id="task:0",
            agent_tools={},
            logger=_logger(),  # type: ignore[arg-type]
        )


def test_offline_dispatch_refuses_when_bundle_lacks_task_description() -> None:
    """A v1.0 bundle whose substrate returns ``None`` for
    ``task_description()`` falls back to reference-impl mode: empty active
    set → ``None``. Locks the backward-compat shim."""
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


def test_offline_dispatch_refuses_when_judge_config_missing_for_declared_judge() -> None:
    """A task declaring ``llm_judge`` but no ``judge_model_config.json`` in
    the bundle refuses actionably, naming the missing v1.1 part."""
    from tolokaforge.core.grading.kinds import GraderKindRefusedError
    from tolokaforge.runner.models import Criterion, LLMJudgeConfig, Rubric

    task_config = RunnerGradingConfig(
        combine_method="weighted",
        weights={"llm_judge": 1.0},
        pass_threshold=0.5,
        llm_judge=LLMJudgeConfig(
            rubric=Rubric(
                criteria=[
                    Criterion(id="c1", description="Response addresses the ask", kind="binary")
                ]
            )
        ),
    )
    substrate = _OfflineV11Substrate(
        task_description=_minimal_task_description(),
        judge_model_config=None,  # v1.0 bundle or run without judge — this triggers refusal
    )
    with pytest.raises(GraderKindRefusedError, match="requires judge_model_config.json"):
        CompositeGraderKind().evaluate(
            substrate=substrate,  # type: ignore[arg-type]
            task_config=task_config,
            kind_config=None,
            trial_id="task:0",
            agent_tools={},
            logger=_logger(),  # type: ignore[arg-type]
        )
