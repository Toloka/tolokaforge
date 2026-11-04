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
from tolokaforge.runner.models import EvidenceInput

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge_kinds.options import JudgeTrialOptions
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.judge_result import JudgeResult
    from tolokaforge.core.grading.judge_tools import EvidenceOwnedTool
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric

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
        options: JudgeTrialOptions,
        logger: StructuredLogger | None = None,
    ) -> None:
        self._judge_model_provider = judge_model_provider
        self._options = options
        self._logger = logger

    def evaluate(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        substrate: GradingSubstrate,
        judge_model_config: ModelConfig,
        extra_read_tools: list[EvidenceOwnedTool],
        state_diff: str | None,
    ) -> JudgeResult:
        judge_model = self._judge_model_provider.build(judge_model_config)
        evidence = self._options.customization.evidence
        return LLMJudge(
            judge_model_config,
            customization=self._options.customization,
            tool_effects=self._options.tool_effects,
            llm_client=judge_model,
            logger=self._logger,
        ).run(
            rubric=rubric,
            agent_system_prompt=agent_system_prompt,
            transcript=transcript,
            db_reader=substrate.db_reader() if evidence.needs(EvidenceInput.DB) else None,
            kb_search=substrate.knowledge_search(),
            extra_read_tools=list(extra_read_tools),
            workspace_dir=(
                substrate.filesystem_root() if evidence.needs(EvidenceInput.WORKSPACE) else None
            ),
            state_diff=state_diff,
        )


def _llm_judge_rubric_evaluator_factory(
    ctx: RubricEvaluatorContext,
) -> RubricEvaluator:
    """Entry-point factory. Adapts :class:`RubricEvaluatorContext` to the
    :class:`LLMJudgeRubricEvaluator` constructor."""
    return LLMJudgeRubricEvaluator(ctx.judge_model_provider, options=ctx.options, logger=ctx.logger)
