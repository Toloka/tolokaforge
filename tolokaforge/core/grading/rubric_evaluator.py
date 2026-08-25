"""Rubric evaluator seam — Protocol + Context + FactoryAlias.

A :class:`RubricEvaluator` grades one trial's rubric evidence into a
:class:`~tolokaforge.core.grading.judge_result.JudgeResult`. It consumes a
:class:`~tolokaforge.core.grading.substrate.GradingSubstrate` for state /
KB / filesystem reads and ignores deterministic-oracle fields by
construction — the same narrow input surface
:class:`~tolokaforge.core.grading.judge.Judge` enforces.

Two-phase construction: factories accept a :class:`RubricEvaluatorContext`
carrying the :class:`~tolokaforge.core.grading.judge_model_provider.JudgeModelProvider`
plus policy flags; the evaluator instance receives the per-trial evidence
at ``.evaluate()`` time. Discovery goes through
:func:`~tolokaforge.core.plugin_registry.load_rubric_evaluator` over the
``tolokaforge.rubric_evaluators`` entry-point group; a downstream package
registers an alternative evaluator alongside the shipping
``llm_judge`` reference impl without a framework PR.

The reference impl lives in
:mod:`tolokaforge.core.grading.default_rubric_evaluator` — this Protocol
module carries no behaviour so the composite dispatch can name
:class:`RubricEvaluator` without ever reaching the reference impl through
it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.judge_result import JudgeResult
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "RubricEvaluator",
    "RubricEvaluatorContext",
    "RubricEvaluatorFactory",
]


@runtime_checkable
class RubricEvaluator(Protocol):
    """Grade one trial's rubric evidence into a :class:`JudgeResult`.

    The per-trial *evidence* surface only. How an evaluator is built —
    judge model provider, budgets, customization flags — is a
    construction-time concern of the concrete impl carried in
    :class:`RubricEvaluatorContext`, NOT part of this contract. Never the
    deterministic-oracle fields (``golden_actions`` /
    ``expect_initial_state`` / ``jsonpath_checks``) — they are not on the
    Protocol surface.
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
    ) -> JudgeResult: ...


@dataclass(frozen=True)
class RubricEvaluatorContext:
    """Everything a rubric-evaluator factory may need at construction.

    The reference impl reads :attr:`judge_model_provider` plus the three
    policy flags; a downstream evaluator is free to ignore fields it does
    not use. :attr:`logger` is optional so a caller can defer to the
    evaluator's own :func:`~tolokaforge.core.logging.get_logger` fallback
    — the shipping reference impl does that when ``None`` reaches
    :class:`~tolokaforge.core.grading.judge.LLMJudge`.
    """

    judge_model_provider: JudgeModelProvider
    logger: StructuredLogger | None = None
    disable_knowledge_search: bool = False
    custom_system_prompt: str | None = None
    include_agent_system_prompt: bool = True


RubricEvaluatorFactory = Callable[[RubricEvaluatorContext], RubricEvaluator]
