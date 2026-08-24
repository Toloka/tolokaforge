"""Deterministic :class:`RubricEvaluator` for the composite-dispatch canonical test.

Ignores the LLM path entirely — no ``LLMJudge``, no network, no substrate
read. Returns a :class:`JudgeResult` whose score is derived by hashing the
joined ``criterion.description`` fields into ``[0.0, 1.0]``, proving the
seam's registry lookup + Protocol dispatch reach an external evaluator
with no framework change.

The file name deliberately avoids the ``test_`` prefix — pytest's
``python_files = ["test_*.py"]`` would otherwise collect this fixture as
a test module. The REGISTERED entry-point name in the dispatch canonical
is ``test_rubric_evaluator``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.judge_result import (
    JudgeResult,
    JudgeStatus,
    JudgeUsage,
)
from tolokaforge.core.grading.rubric_evaluator import (
    RubricEvaluator,
    RubricEvaluatorContext,
)
from tolokaforge.runner.models import CriterionResult

if TYPE_CHECKING:
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "DeterministicRubricEvaluator",
    "_hash_to_score",
    "_rubric_signature",
    "_test_rubric_evaluator_factory",
]


def _hash_to_score(text: str) -> float:
    """Fold ``text`` into ``[0.0, 1.0]`` via a SHA-256 prefix.

    Deterministic across processes so the canonical test asserts the exact
    score, not just presence.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / (1 << 64)


def _rubric_signature(rubric: Rubric) -> str:
    """Stable text projection of a rubric — joined criterion descriptions."""
    return "\n".join(criterion.description for criterion in rubric.criteria)


class DeterministicRubricEvaluator:
    """Returns a :class:`JudgeResult` whose weighted score is
    :func:`_hash_to_score` over :func:`_rubric_signature`.

    Every criterion carries the same score; the rubric-level ``score`` is
    trivially equal. ``criterion_results`` is populated so the composite's
    downstream wire assembly sees the same shape a real judge produces.
    """

    def evaluate(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        substrate: GradingSubstrate,
        judge_model_config: ModelConfig,
        extra_read_tools: list[Tool],
        state_diff: str | None,
    ) -> JudgeResult:
        score = _hash_to_score(_rubric_signature(rubric))
        criterion_results = tuple(
            CriterionResult(
                id=criterion.id,
                met=score >= 0.5,
                score=score,
                justification=f"deterministic hash score {score:.3f}",
            )
            for criterion in rubric.criteria
        )
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=JudgeUsage(),
            reasons=f"Deterministic hash score {score:.3f}",
            score=score,
            binary_pass=score >= 0.5,
            criterion_results=criterion_results,
        )


def _test_rubric_evaluator_factory(
    ctx: RubricEvaluatorContext,
) -> RubricEvaluator:
    """Entry-point factory for the demo evaluator. Ignores ``ctx`` — the
    deterministic evaluator has no build-time dependency to accept."""
    del ctx
    return DeterministicRubricEvaluator()
