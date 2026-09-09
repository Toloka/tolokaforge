"""Reference impl of :class:`JudgeKind` — wraps :class:`LLMJudge` in one shot.

Registered under the name ``single_shot_rubric`` in the
``tolokaforge.judge_kinds`` entry-point group. Builds the judge model
from the caller-supplied :class:`JudgeModelProvider`, constructs an
:class:`LLMJudge` with the caller-supplied per-trial customization, and
runs it once over the trial's rubric evidence.

Byte-identity anchor: the ``LLMJudge`` construction below matches the
pre-seam :class:`LLMJudgeRubricEvaluator.evaluate` call one-for-one, so
:meth:`SingleShotRubricJudgeKind.evaluate` produces the same
:class:`JudgeResult` the pre-seam path produced from the same inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge import LLMJudge

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.judge_result import JudgeResult
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "SingleShotRubricJudgeKind",
]


class SingleShotRubricJudgeKind:
    """Grade one rubric with one :class:`LLMJudge` invocation."""

    NAME: ClassVar[str] = "single_shot_rubric"

    def evaluate(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None,
        kb_search: KnowledgeSearch | None,
        workspace_dir: Path | None,
        extra_read_tools: list[Tool],
        state_diff: str | None,
        judge_model_config: ModelConfig,
        judge_model_provider: JudgeModelProvider,
        disable_knowledge_search: bool,
        custom_system_prompt: str | None,
        include_agent_system_prompt: bool,
        kind_config: Mapping[str, Any] | None,
        logger: StructuredLogger,
    ) -> JudgeResult:
        del kind_config  # reserved on the Protocol for downstream kinds
        judge_model = judge_model_provider.build(judge_model_config)
        return LLMJudge(
            judge_model_config,
            disable_knowledge_search=disable_knowledge_search,
            custom_system_prompt=custom_system_prompt,
            include_agent_system_prompt=include_agent_system_prompt,
            llm_client=judge_model,
            logger=logger,
        ).run(
            rubric=rubric,
            agent_system_prompt=agent_system_prompt,
            transcript=transcript,
            db_reader=db_reader,
            kb_search=kb_search,
            extra_read_tools=list(extra_read_tools),
            workspace_dir=workspace_dir,
            state_diff=state_diff,
        )
