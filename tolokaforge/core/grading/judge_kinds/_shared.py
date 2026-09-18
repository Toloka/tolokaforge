"""Helpers shared by ``JudgeKind`` implementations that dispatch multiple
judge clients per ``evaluate`` call (``chunked_rubric``, ``voted_rubric``,
``jury_rubric``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from tolokaforge.core.grading.judge_result import JudgeStatus, JudgeUsage

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge_result import JudgeResult

__all__ = [
    "CONSTRUCTION_FIELDS",
    "assert_construction_fields_match",
    "member_failure_reason",
    "sum_usage",
]


#: Per-dispatch fields that MUST be constant across every dispatch of a
#: multi-client ``JudgeKind`` (pure functions of the ``evaluate`` inputs). A
#: mismatch across chunks / samples / panel members is a defensive lock
#: catching a future kind refactor that accidentally per-dispatches one of
#: these inputs.
CONSTRUCTION_FIELDS: tuple[str, ...] = (
    "kb_tools_offered",
    "kb_tools_withheld",
    "knowledge_search_disabled",
    "custom_system_prompt",
    "include_agent_system_prompt",
    "read_tools_offered",
    "state_diff",
)


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


def member_failure_reason(result: JudgeResult, criterion_ids: tuple[str, ...]) -> str | None:
    """Return a failure reason string if ``result`` did not COMPLETE cleanly.

    Unit-agnostic: used for a chunk, a sample, or a panel member alike — the
    message names "criterion ids", never the calling kind's unit noun.
    """
    if result.status is not JudgeStatus.COMPLETED:
        return f"status={result.status.value}: {result.reasons}"
    covered = {cr.id for cr in result.criterion_results}
    missing = [cid for cid in criterion_ids if cid not in covered]
    if missing:
        return f"missing verdicts for criterion ids {missing}: {result.reasons}"
    return None


def assert_construction_fields_match(
    results: Sequence[JudgeResult],
    fields: tuple[str, ...],
    *,
    kind_label: str,
    unit_noun: str,
) -> None:
    """Raise :class:`RuntimeError` if any of ``fields`` diverges across ``results``.

    ``results`` come from ``unit_noun``-many dispatches (chunks, samples,
    panel members) that share the same ``evaluate`` inputs, so these
    construction-time fields must be identical across every one of them.
    """
    for field in fields:
        head_value = getattr(results[0], field)
        for index, result in enumerate(results[1:], start=1):
            other_value = getattr(result, field)
            if other_value != head_value:
                raise RuntimeError(
                    f"{kind_label} construction-field mismatch across {unit_noun}s: "
                    f"{field!r} on {unit_noun} 0 is {head_value!r} but {unit_noun} "
                    f"{index} is {other_value!r}. Every {unit_noun} shares the same "
                    f"evaluate inputs; a divergence signals a kind refactor that "
                    f"accidentally per-{unit_noun}s a construction input."
                )
