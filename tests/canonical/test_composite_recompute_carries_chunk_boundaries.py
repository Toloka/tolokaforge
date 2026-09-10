"""Fifth populator site — ``CompositeGraderKind._run_composite`` threads
``JudgeResult.chunk_boundaries`` into the inline ``Grade(...)``.

The composite grader kind's offline recompute mode constructs its own
inline :class:`Grade` (distinct from ``build_replay_grade``); a missed
populator edit at that construction site would leave chunked bundles
regraded via ``tolokaforge grade`` with empty
``Grade.judge_chunk_boundaries``. This test locks that path directly by
driving :meth:`CompositeGraderKind._run_composite` with a stubbed
composite module surface + a stubbed judge kind that emits a
:class:`JudgeResult` whose ``chunk_boundaries`` is non-empty, and
asserts the returned Grade carries the boundaries verbatim.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.grade_components import CompositeGradeComponents
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.grading.kinds.composite import CompositeGraderKind
from tolokaforge.core.models.grade import CustomCheckDetail
from tolokaforge.core.models.grade import JudgeStatus as HostJudgeStatus
from tolokaforge.core.models.grade_components import GradeComponents
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    Rubric,
    RunnerGradingConfig,
    TraceChecksSummary,
    TraceConstraintResult,
    TracePathResult,
)

pytestmark = pytest.mark.canonical


def _judge_result(chunk_boundaries: tuple[tuple[str, ...], ...]) -> JudgeResult:
    return JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(calls=1, cost_usd=0.0),
        reasons="stub",
        score=1.0,
        binary_pass=True,
        criterion_results=(),
        chunk_boundaries=chunk_boundaries,
    )


def _task_config_with_llm_judge() -> RunnerGradingConfig:
    return RunnerGradingConfig(
        weights={"llm_judge": 1.0},
        pass_threshold=0.5,
        llm_judge=LLMJudgeConfig(
            rubric=Rubric(
                criteria=[
                    Criterion(id="a", description="a", kind="binary"),
                    Criterion(id="b", description="b", kind="binary"),
                    Criterion(id="c", description="c", kind="binary"),
                ]
            ),
            judge_kind="chunked_rubric",
            kind_config={"chunk_size": 2},
        ),
    )


def _stub_composite_mod(judge_result: JudgeResult) -> SimpleNamespace:
    """Stub the ``composite`` module surface ``_run_composite`` reads —
    only ``grade_llm_judge`` and ``grade_custom_checks`` fire on this
    task_config shape (llm_judge only, no state_checks / transcript_rules /
    trace_checks / custom_checks)."""
    return SimpleNamespace(
        grade_llm_judge=MagicMock(return_value=judge_result),
        grade_custom_checks=MagicMock(return_value=(-1.0, [], "")),
        build_judge_state_diff=MagicMock(return_value=None),
    )


def _drive_run_composite(judge_result: JudgeResult):
    """Call ``_run_composite`` with everything but the judge stubbed;
    return the resulting host ``Grade``."""

    class _StubJudgeKind:
        def evaluate(self, **_kwargs: Any) -> JudgeResult:
            return judge_result

    def _load_judge_kind(_name: str) -> type[_StubJudgeKind]:
        return _StubJudgeKind

    return CompositeGraderKind()._run_composite(
        substrate=MagicMock(),
        task_config=_task_config_with_llm_judge(),
        task_description=MagicMock(),
        judge_model_config=MagicMock(),
        llm_messages=[{"role": "user", "content": "hello"}],
        timeline=MagicMock(),
        id_fields={},
        unstable_fields=set(),
        initial_state_schemas=[],
        artifacts_dir=None,
        trial_id="task:0",
        state_check_backends={},
        transcript_rule_matcher=MagicMock(),
        check_executor=MagicMock(),
        judge_model_provider=MagicMock(),
        logger=logging.getLogger("test-composite-recompute-chunk"),
        composite_mod=_stub_composite_mod(judge_result),
        composite_components_cls=CompositeGradeComponents,
        load_judge_kind=_load_judge_kind,
        judge_status_cls=HostJudgeStatus,
        trace_summary_cls=TraceChecksSummary,
        trace_constraint_cls=TraceConstraintResult,
        trace_path_cls=TracePathResult,
        custom_detail_cls=CustomCheckDetail,
    )


class TestChunkedJudgeResultPopulatesGrade:
    def test_non_empty_chunk_boundaries_lands_on_grade(self) -> None:
        grade = _drive_run_composite(_judge_result(chunk_boundaries=(("a", "b"), ("c",))))
        assert grade is not None
        assert grade.judge_chunk_boundaries == [["a", "b"], ["c"]]

    def test_empty_chunk_boundaries_maps_to_none(self) -> None:
        grade = _drive_run_composite(_judge_result(chunk_boundaries=()))
        assert grade is not None
        assert grade.judge_chunk_boundaries is None


class TestGradeComponentsFallbacks:
    def test_grade_carries_pydantic_grade_components(self) -> None:
        """Regression guard — ``Grade.components`` still slots the fold's
        llm_judge score even with everything stubbed."""
        grade = _drive_run_composite(_judge_result(chunk_boundaries=(("a", "b"), ("c",))))
        assert grade is not None
        assert isinstance(grade.components, GradeComponents)
