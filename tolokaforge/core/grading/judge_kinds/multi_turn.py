"""Multi-turn-rubric impl of :class:`JudgeKind` — the baked-in composition.

Registered under the name ``multi_turn_rubric`` in the
``tolokaforge.judge_kinds`` entry-point group. Composes
``voted_rubric(n_samples=3, aggregator="geometric_median")`` wrapping
``auto_anchored_rubric`` wrapping ``single_shot_rubric``. The composition
attacks both variance classes the M50 evidence identified: ``voted``
collapses provider-side sampling noise at temp=0, and ``auto_anchored``
rewrites unanchored graded criteria to have a strict anchor before
dispatch so the wrapped judge grades against a concrete anchor rather
than fuzzy natural-language descriptions.

Wrapping order is chosen so the ``auto_anchored`` warm-up call fires
exactly once per unique (rubric, judge_model): its process-lifetime
anchor cache is inside ``voted``'s K-loop, so K samples all see the
identical synthetic rubric.

**No user knobs.** ``multi_turn_rubric`` accepts NO ``kind_config`` —
passing a non-empty ``kind_config`` raises :class:`ValueError` before
any judge dispatch runs. Users who want to tune the composition select
``voted_rubric`` or ``auto_anchored_rubric`` directly.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge_kinds.voted import VotedRubricJudgeKind

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
    "MultiTurnRubricJudgeKind",
]

_COMPOSITION_AUDIT_LINE = (
    "multi_turn_rubric composition: voted(n=3, geometric_median) "
    "→ auto_anchored → single_shot\n\n"
)


class MultiTurnRubricJudgeKind:
    """Grade a rubric via the baked-in ``voted → auto_anchored → single_shot`` stack."""

    NAME: ClassVar[str] = "multi_turn_rubric"

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
                "multi_turn_rubric accepts no kind_config; the composition is baked in. "
                f"Received keys: {sorted(kind_config)}."
            )

        inner_kind_config: dict[str, Any] = {
            "n_samples": 3,
            "aggregator": "geometric_median",
            "wrapped_kind": "auto_anchored_rubric",
        }

        inner = VotedRubricJudgeKind().evaluate(
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
            kind_config=inner_kind_config,
            logger=logger,
        )

        return dataclasses.replace(inner, reasons=f"{_COMPOSITION_AUDIT_LINE}{inner.reasons}")
