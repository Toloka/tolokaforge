"""Per-criterion-rubric impl of :class:`JudgeKind` — one :class:`LLMJudge` per criterion.

Registered under the name ``per_criterion_rubric`` in the
``tolokaforge.judge_kinds`` entry-point group. A thin specialisation of
:class:`~tolokaforge.core.grading.judge_kinds.chunked.ChunkedRubricJudgeKind`
that hard-pins ``chunk_size = 1``: every criterion is graded by its own
:class:`LLMJudge` call, so each ``submit_report`` payload carries exactly
one verdict and cross-criterion halo/recency drift cannot occur by
construction.

The kind exists as an explicit "isolate every criterion" opt-in for
task authors who care about per-criterion stability more than cost. It
is the recommended kind for graded/subjective rubrics of six or more
criteria where the M50 drift-report identified cross-chunk context loss
as a real source of drift.

``kind_config`` accepts no keys — an explicit ``chunk_size`` here would
be silently ignored, so unknown keys fail loud eagerly. Every fail-loud
guarantee ``chunked_rubric`` carries (per-chunk COMPLETED status,
missing-verdict detection, whole-trial ERRORED with ``chunk_boundaries``
populated for offline retry) applies here too — the underlying merge
path is the same.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge_kinds.chunked import ChunkedRubricJudgeKind

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.judge_result import JudgeResult
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = ["PerCriterionRubricJudgeKind"]


class PerCriterionRubricJudgeKind:
    """Grade a rubric with one :class:`LLMJudge` invocation per criterion."""

    NAME: ClassVar[str] = "per_criterion_rubric"

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
        if kind_config is not None and dict(kind_config):
            raise ValueError(
                f"per_criterion_rubric kind_config accepts no keys; got "
                f"{sorted(dict(kind_config))}. Every criterion is always its own "
                f"chunk — pass no kind_config at all."
            )
        return ChunkedRubricJudgeKind().evaluate(
            rubric=rubric,
            agent_system_prompt=agent_system_prompt,
            transcript=transcript,
            db_reader=db_reader,
            kb_search=kb_search,
            workspace_dir=workspace_dir,
            extra_read_tools=extra_read_tools,
            state_diff=state_diff,
            judge_model_config=judge_model_config,
            judge_model_provider=judge_model_provider,
            disable_knowledge_search=disable_knowledge_search,
            custom_system_prompt=custom_system_prompt,
            include_agent_system_prompt=include_agent_system_prompt,
            kind_config={"chunk_size": 1},
            logger=logger,
        )
