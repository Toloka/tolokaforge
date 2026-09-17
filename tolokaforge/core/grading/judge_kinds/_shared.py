"""Helpers shared by ``JudgeKind`` implementations that dispatch multiple
judge clients per ``evaluate`` call (``chunked_rubric``, ``voted_rubric``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.grading.judge_result import JudgeUsage

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge_result import JudgeResult

__all__ = ["sum_usage"]


def sum_usage(results: list[JudgeResult]) -> JudgeUsage:
    """Field-wise sum of per-call :class:`JudgeUsage` across multiple judge calls."""
    return JudgeUsage(
        calls=sum(r.usage.calls for r in results),
        prompt_tokens=sum(r.usage.prompt_tokens for r in results),
        completion_tokens=sum(r.usage.completion_tokens for r in results),
        reasoning_tokens=sum(r.usage.reasoning_tokens for r in results),
        cost_usd=sum(r.usage.cost_usd for r in results),
        tool_calls=sum(r.usage.tool_calls for r in results),
        consistency_rejections=sum(r.usage.consistency_rejections for r in results),
    )
