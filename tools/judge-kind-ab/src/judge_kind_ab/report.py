"""Render a :class:`LiveABResult` to ``report.md`` (PR-body fragment) and
``report.json`` (raw numbers for follow-up tooling).

Kept separate from the CLI so the formatting is testable without driving a
live (or scripted) judge model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tolokaforge.core.grading.judge_kinds.parity import PerCriterionVerdict
from tolokaforge.core.grading.judge_result import JudgeUsage

from .runner import CrossKindResult, LiveABResult, SelfConsistencyResult, UsageRecord


def _column_label(result: CrossKindResult | SelfConsistencyResult) -> str:
    if isinstance(result, CrossKindResult):
        return f"{result.reference} vs {result.candidate}"
    return f"{result.kind} (self)"


def _verdict_cell(verdict: PerCriterionVerdict | None) -> str:
    if verdict is None:
        return "—"
    if verdict.kappa is None:
        return f"n/a ({verdict.status})"
    return f"{verdict.kappa:.3f} ({verdict.status})"


def _kappa_table_rows(
    result: LiveABResult,
) -> tuple[list[CrossKindResult | SelfConsistencyResult], list[str]]:
    """Union every criterion id present in any column's decision, sorted."""
    columns: list[CrossKindResult | SelfConsistencyResult] = [
        *result.cross_kind,
        *result.self_consistency,
    ]
    criterion_ids: set[str] = set()
    for column in columns:
        criterion_ids.update(v.criterion_id for v in column.decision.per_criterion)
    return columns, sorted(criterion_ids)


def _sum_usage(records: tuple[UsageRecord, ...]) -> JudgeUsage:
    """Total usage field-wise, mirroring ``rubric_calibrator.runner._sum_usage``."""
    return JudgeUsage(
        calls=sum(r.usage.calls for r in records),
        prompt_tokens=sum(r.usage.prompt_tokens for r in records),
        completion_tokens=sum(r.usage.completion_tokens for r in records),
        reasoning_tokens=sum(r.usage.reasoning_tokens for r in records),
        cost_usd=sum(r.usage.cost_usd for r in records),
        tool_calls=sum(r.usage.tool_calls for r in records),
        consistency_rejections=sum(r.usage.consistency_rejections for r in records),
    )


def _usage_by_family(records: tuple[UsageRecord, ...]) -> dict[str, JudgeUsage]:
    families = sorted({r.family for r in records})
    return {
        family: _sum_usage(tuple(r for r in records if r.family == family)) for family in families
    }


def render_markdown(result: LiveABResult) -> str:
    """Render the per-criterion kappa table and the cost tables as markdown."""
    lines: list[str] = []

    columns, criterion_ids = _kappa_table_rows(result)
    lines.append("## Per-criterion kappa")
    lines.append("")
    header = ["criterion", *(_column_label(c) for c in columns)]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for criterion_id in criterion_ids:
        row = [criterion_id]
        for column in columns:
            verdict = next(
                (v for v in column.decision.per_criterion if v.criterion_id == criterion_id), None
            )
            row.append(_verdict_cell(verdict))
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    lines.append("## Cost by kind")
    lines.append("")
    lines.append(
        "| kind | calls | prompt tokens | completion tokens | reasoning tokens | cost_usd |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for name in sorted(result.usage_by_kind):
        usage = _sum_usage(result.usage_by_kind[name])
        lines.append(
            f"| {name} | {usage.calls} | {usage.prompt_tokens} | {usage.completion_tokens} | "
            f"{usage.reasoning_tokens} | {usage.cost_usd:.4f} |"
        )

    lines.append("")
    lines.append("## Cost by kind and task family")
    lines.append("")
    lines.append(
        "| kind | family | calls | prompt tokens | completion tokens | reasoning tokens | cost_usd |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for name in sorted(result.usage_by_kind):
        for family, usage in _usage_by_family(result.usage_by_kind[name]).items():
            lines.append(
                f"| {name} | {family} | {usage.calls} | {usage.prompt_tokens} | "
                f"{usage.completion_tokens} | {usage.reasoning_tokens} | {usage.cost_usd:.4f} |"
            )

    return "\n".join(lines) + "\n"


def render_json(result: LiveABResult) -> dict[str, Any]:
    """Raw numbers behind the markdown tables, for follow-up tooling."""

    def _verdict_dict(v: PerCriterionVerdict) -> dict[str, Any]:
        return {
            "criterion_id": v.criterion_id,
            "observations": v.observations,
            "kappa": v.kappa,
            "status": v.status,
            "reason": v.reason,
        }

    def _usage_dict(usage: JudgeUsage) -> dict[str, Any]:
        return {
            "calls": usage.calls,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cost_usd": usage.cost_usd,
            "tool_calls": usage.tool_calls,
            "consistency_rejections": usage.consistency_rejections,
        }

    return {
        "cross_kind": [
            {
                "reference": c.reference,
                "candidate": c.candidate,
                "shippable": c.decision.shippable,
                "blocking_criteria": list(c.decision.blocking_criteria),
                "warning_criteria": list(c.decision.warning_criteria),
                "per_criterion": [_verdict_dict(v) for v in c.decision.per_criterion],
            }
            for c in result.cross_kind
        ],
        "self_consistency": [
            {
                "kind": s.kind,
                "shippable": s.decision.shippable,
                "blocking_criteria": list(s.decision.blocking_criteria),
                "warning_criteria": list(s.decision.warning_criteria),
                "per_criterion": [_verdict_dict(v) for v in s.decision.per_criterion],
            }
            for s in result.self_consistency
        ],
        "usage_by_kind": {
            name: {
                "total": _usage_dict(_sum_usage(records)),
                "by_family": {
                    family: _usage_dict(usage)
                    for family, usage in _usage_by_family(records).items()
                },
            }
            for name, records in result.usage_by_kind.items()
        },
    }


def write_report(result: LiveABResult, out_dir: Path) -> None:
    """Write ``<out_dir>/report.md`` and ``<out_dir>/report.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_markdown(result))
    (out_dir / "report.json").write_text(json.dumps(render_json(result), indent=2))
