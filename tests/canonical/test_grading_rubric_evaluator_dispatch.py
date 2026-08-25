"""``load_rubric_evaluator`` discovery + composite dispatch through the seam.

Locks two seams the :class:`RubricEvaluator` design commits to:

1. :func:`~tolokaforge.core.plugin_registry.load_rubric_evaluator` resolves
   an evaluator registered via ``importlib.metadata`` entry-points. The
   dispatch case injects a synthetic entry-point pointing at
   :class:`DeterministicRubricEvaluator` under the group
   ``tolokaforge.rubric_evaluators`` and asserts the loader returns the
   deterministic factory. No wheel pollution: the demo stays discoverable
   only under the monkeypatched mapping.

2. **Composite dispatch through the Protocol.**
   :func:`composite.grade_llm_judge` drives the resolved evaluator and
   its ``JudgeResult.score`` matches the deterministic hash the demo
   emits — proving the composite reaches the plug-in evaluator without
   ever touching :class:`LLMJudge`.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.utils.rubric_evaluator_demo import (
    DeterministicRubricEvaluator,
    _hash_to_score,
    _rubric_signature,
    _test_rubric_evaluator_factory,
)
from tolokaforge.core.grading import composite
from tolokaforge.core.grading.judge_result import JudgeStatus
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.plugin_registry import (
    RUBRIC_EVALUATORS_GROUP,
    _clear_discovery_cache,
    load_rubric_evaluator,
)
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    Rubric,
)

pytestmark = pytest.mark.canonical


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _EntryPointStub:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the discovery scan.

    Enumerates ``name`` / ``dist`` and returns ``value`` on ``load()`` — the
    surface :func:`discover_entry_points` reads.
    """

    def __init__(self, name: str, value: Any, dist_name: str = "tests-fixture") -> None:
        self.name = name
        self.value = value

        class _Dist:
            def __init__(self, dn: str) -> None:
                self.name = dn

        self.dist = _Dist(dist_name)

    def load(self) -> Any:
        return self.value


def _inject_demo_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the demo evaluator alongside the shipped ``llm_judge``.

    Shape-identical to :func:`test_load_grading_substrate_resolves_a_monkeypatched_entry_point`
    in the substrate suite — the fail-loud registry discovery contract."""
    _clear_discovery_cache()
    shipped = list(importlib.metadata.entry_points(group=RUBRIC_EVALUATORS_GROUP))
    injected = _EntryPointStub("test_rubric_evaluator", _test_rubric_evaluator_factory)

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == RUBRIC_EVALUATORS_GROUP:
            return [*shipped, injected]
        return list(importlib.metadata.entry_points(group=group))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    _clear_discovery_cache()


def _rubric() -> Rubric:
    return Rubric(
        criteria=[
            Criterion(
                id="refund_done",
                description="Refund issued to the customer",
                kind="binary",
                weight=2.0,
            ),
            Criterion(
                id="tone",
                description="Polite tone throughout the exchange",
                kind="graded",
                weight=1.0,
            ),
        ]
    )


def _substrate() -> InProcessGradingSubstrate:
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state={},
    )


def _logger() -> StructuredLogger:
    return StructuredLogger(name="test-rubric-evaluator-dispatch")


def test_loader_resolves_the_monkeypatched_demo_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_demo_evaluator(monkeypatch)
    try:
        factory = load_rubric_evaluator("test_rubric_evaluator")
        assert factory is _test_rubric_evaluator_factory
        instance = factory(MagicMock())
        assert isinstance(instance, DeterministicRubricEvaluator)
    finally:
        _clear_discovery_cache()


def test_composite_dispatches_through_the_resolved_demo_evaluator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composite's ``grade_llm_judge`` produces a :class:`JudgeResult`
    whose score is the deterministic hash the demo emits — end-to-end
    proof the seam threads the resolved evaluator through the dispatch.
    """
    _inject_demo_evaluator(monkeypatch)
    try:
        rubric = _rubric()
        config = LLMJudgeConfig(rubric=rubric)
        evaluator = load_rubric_evaluator("test_rubric_evaluator")(MagicMock())

        result = composite.grade_llm_judge(
            trial_id="task:0",
            config=config,
            substrate=_substrate(),
            rubric_evaluator=evaluator,
            llm_messages=[
                {"role": "system", "content": "policy"},
                {"role": "user", "content": "please refund me"},
            ],
            judge_model_config=_JUDGE_MODEL,
            extra_read_tools=[],
            state_diff=None,
            logger=_logger(),
        )

        expected_score = _hash_to_score(_rubric_signature(rubric))
        assert result.status is JudgeStatus.COMPLETED
        assert result.score == pytest.approx(expected_score)
        assert {cr.id for cr in result.criterion_results} == {"refund_done", "tone"}
    finally:
        _clear_discovery_cache()
