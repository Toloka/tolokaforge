"""Auto-selecting-rubric impl of :class:`JudgeKind` — per-rubric routing.

Registered under the name ``auto_rubric`` in the
``tolokaforge.judge_kinds`` entry-point group. Inspects the rubric shape
at ``evaluate`` time and dispatches deterministically (constant-time,
no LLM call) between ``single_shot_rubric`` and ``multi_turn_rubric``:

- Any ``graded`` criterion with ``expected is None`` → dispatch
  ``multi_turn_rubric`` (the composition attacks the fuzzy-wording drift
  class those unanchored criteria expose).
- Otherwise → dispatch ``single_shot_rubric`` (author-written anchors,
  or binary-only rubrics, are handled correctly and cheaply by one call).

The selection reason lands in ``JudgeResult.reasons`` so the audit trail
shows which mode graded a trial.

**No user knobs.** ``auto_rubric`` accepts NO ``kind_config`` — passing
a non-empty ``kind_config`` raises :class:`ValueError` before any judge
dispatch runs.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge_kinds.multi_turn import MultiTurnRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.single_shot import SingleShotRubricJudgeKind

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_kinds.options import JudgeTrialOptions
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.judge_result import JudgeResult
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "AutoRubricJudgeKind",
]


class AutoRubricJudgeKind:
    """Dispatch per rubric shape to ``single_shot_rubric`` or ``multi_turn_rubric``."""

    NAME: ClassVar[str] = "auto_rubric"

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
        options: JudgeTrialOptions,
        kind_config: Mapping[str, Any] | None,
        logger: StructuredLogger,
    ) -> JudgeResult:
        if kind_config:
            raise ValueError(
                "auto_rubric accepts no kind_config; the selection is baked in. "
                f"Received keys: {sorted(kind_config)}."
            )

        unanchored_ids = sorted(
            c.id for c in rubric.criteria if c.kind == "graded" and c.expected is None
        )
        if unanchored_ids:
            selected: SingleShotRubricJudgeKind | MultiTurnRubricJudgeKind = (
                MultiTurnRubricJudgeKind()
            )
            audit_line = (
                f"auto_rubric selected multi_turn_rubric because these graded "
                f"criteria have no `expected:` anchor: {', '.join(unanchored_ids)}\n\n"
            )
        else:
            selected = SingleShotRubricJudgeKind()
            audit_line = (
                "auto_rubric selected single_shot_rubric (all graded criteria are anchored)\n\n"
            )

        inner = selected.evaluate(
            rubric=rubric,
            agent_system_prompt=agent_system_prompt,
            transcript=transcript,
            db_reader=db_reader,
            kb_search=kb_search,
            workspace_dir=workspace_dir,
            extra_read_tools=list(extra_read_tools),
            state_diff=state_diff,
            judge_model_config=judge_model_config,
            judge_model_provider=judge_model_provider,
            options=options,
            kind_config=None,
            logger=logger,
        )

        return dataclasses.replace(inner, reasons=f"{audit_line}{inner.reasons}")
