"""Reference impl of :class:`RubricEvaluator` — wraps
:class:`~tolokaforge.core.grading.judge.LLMJudge`.

Registered under the name ``llm_judge`` in the
``tolokaforge.rubric_evaluators`` entry-point group. Constructs the
:class:`~tolokaforge.core.grading.judge.LLMJudge` per ``.evaluate()`` call
using the :class:`~tolokaforge.core.grading.judge_model_provider.JudgeModelProvider`
handed via :class:`~tolokaforge.core.grading.rubric_evaluator.RubricEvaluatorContext`,
and delegates the run.

This module holds the only concrete impl of :class:`RubricEvaluator` in
the shipping distribution, so the ``.importlinter`` contract can forbid
composite from importing it without also forbidding the Protocol module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.judge import LLMJudge
from tolokaforge.core.grading.rubric_evaluator import (
    RubricEvaluator,
    RubricEvaluatorContext,
)

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.judge_result import JudgeResult
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "LLMJudgeRubricEvaluator",
]


class LLMJudgeRubricEvaluator:
    """Build the :class:`LLMJudge` per :meth:`evaluate` and delegate to its ``run``.

    The judge model itself is built lazily from :attr:`judge_model_provider`
    inside :meth:`evaluate` — the provider takes the run-level
    :class:`~tolokaforge.core.models.ModelConfig` and returns a
    :class:`~tolokaforge.core.grading.judge_model_provider.JudgeModel` the
    judge injects into its loop, so a downstream transport swap (LiteLLM,
    OpenAI-direct, Vertex, …) requires no change to this evaluator.
    """

    def __init__(
        self,
        judge_model_provider: JudgeModelProvider,
        *,
        disable_knowledge_search: bool = False,
        custom_system_prompt: str | None = None,
        include_agent_system_prompt: bool = True,
        logger: StructuredLogger | None = None,
    ) -> None:
        self._judge_model_provider = judge_model_provider
        self._disable_knowledge_search = disable_knowledge_search
        self._custom_system_prompt = custom_system_prompt
        self._include_agent_system_prompt = include_agent_system_prompt
        self._logger = logger

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
        judge_model = self._judge_model_provider.build(judge_model_config)
        return LLMJudge(
            judge_model_config,
            disable_knowledge_search=self._disable_knowledge_search,
            custom_system_prompt=self._custom_system_prompt,
            include_agent_system_prompt=self._include_agent_system_prompt,
            llm_client=judge_model,
            logger=self._logger,
        ).run(
            rubric=rubric,
            agent_system_prompt=agent_system_prompt,
            transcript=transcript,
            db_reader=substrate.db_reader(),
            kb_search=substrate.knowledge_search(),
            extra_read_tools=list(extra_read_tools),
            workspace_dir=substrate.filesystem_root(),
            state_diff=state_diff,
        )


def _llm_judge_rubric_evaluator_factory(
    ctx: RubricEvaluatorContext,
) -> RubricEvaluator:
    """Entry-point factory. Adapts :class:`RubricEvaluatorContext` to the
    :class:`LLMJudgeRubricEvaluator` constructor."""
    return LLMJudgeRubricEvaluator(
        ctx.judge_model_provider,
        disable_knowledge_search=ctx.disable_knowledge_search,
        custom_system_prompt=ctx.custom_system_prompt,
        include_agent_system_prompt=ctx.include_agent_system_prompt,
        logger=ctx.logger,
    )
