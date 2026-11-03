"""Helpers shared by ``JudgeKind`` implementations that dispatch multiple
judge clients per ``evaluate`` call (``voted_rubric``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from tolokaforge.core.grading.judge_result import JudgeStatus, JudgeUsage
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.llm.usage import sum_known

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge_result import JudgeResult

__all__ = [
    "CONSTRUCTION_FIELDS",
    "assert_construction_fields_match",
    "billed_sum",
    "carry_earlier_spend",
    "member_failure_reason",
    "sum_usage",
    "sum_usages",
]


#: Per-dispatch fields that must be constant across every dispatch of a
#: multi-client ``JudgeKind`` (chunks / samples / panel members).
CONSTRUCTION_FIELDS: tuple[str, ...] = (
    "kb_tools_offered",
    "kb_tools_withheld",
    "knowledge_search_disabled",
    "custom_system_prompt",
    "include_agent_system_prompt",
    "read_tools_offered",
    "state_diff",
    "evidence",
)


def sum_usage(results: list[JudgeResult]) -> JudgeUsage:
    """Field-wise sum of per-call :class:`JudgeUsage` across multiple judge calls."""
    return sum_usages([r.usage for r in results])


def sum_usages(usages: Sequence[JudgeUsage]) -> JudgeUsage:
    """Field-wise sum of several judge runs' :class:`JudgeUsage`."""
    return JudgeUsage(
        calls=sum(u.calls for u in usages),
        prompt_tokens=sum(u.prompt_tokens for u in usages),
        completion_tokens=sum(u.completion_tokens for u in usages),
        reasoning_tokens=sum(u.reasoning_tokens for u in usages),
        cost_usd=sum(u.cost_usd for u in usages),
        tool_calls=sum(u.tool_calls for u in usages),
        consistency_rejections=sum(u.consistency_rejections for u in usages),
        billed_cost_usd=billed_sum(usages),
    )


def carry_earlier_spend(exc: SubstrateUnreachableError, earlier: Sequence[JudgeUsage]) -> None:
    """Add the spend of judge runs that finished before ``exc`` to the spend it carries.

    A kind that runs its judge more than once (samples, a warm-up) re-raises a refusal from a
    later run; without this, the refusal would carry that run's spend alone.
    """
    exc.judge_usage = sum_usages([*earlier, exc.judge_usage or JudgeUsage()])


def billed_sum(usages: Iterable[JudgeUsage]) -> float | None:
    """The charge stated for the calls of several judge runs: the sum over the runs that made
    a call, ``None`` when one of those stated no charge or when none made a call. A run that made
    no call was charged nothing, so it neither adds to nor voids the sum."""
    return sum_known(usage.billed_cost_usd for usage in usages if usage.calls)


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
