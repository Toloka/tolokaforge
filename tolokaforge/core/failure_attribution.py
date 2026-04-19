"""Deterministic failure attribution from trajectory artifacts."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from tolokaforge.core.models import TerminationReason, Trajectory, TrialStatus

DETERMINISTIC_CLASSES = {
    "tool_arguments",
    "tool_execution",
    "grader_contract",
    "infrastructure",
    "timeout_or_resource",
}

_CONNECTION_ERROR_RE = re.compile(
    r"ERR_CONNECTION_REFUSED|ECONNREFUSED|Connection refused|net::ERR_",
    re.IGNORECASE,
)


def is_failed_trajectory(trajectory: Trajectory) -> bool:
    """Return True if trajectory should be considered a failed attempt."""
    if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT, TrialStatus.FAILED):
        return True
    if trajectory.grade is None:
        return True
    return not trajectory.grade.binary_pass


def attribute_failure(trajectory: Trajectory) -> dict[str, Any]:
    """Classify failure cause with evidence pointers."""
    evidence: list[dict[str, Any]] = []
    failure_class = "model_reasoning"
    deterministic = False

    if trajectory.termination_reason in (
        TerminationReason.TIMEOUT,
        TerminationReason.RATE_LIMIT,
        TerminationReason.API_ERROR,
        TerminationReason.ERROR,
    ):
        failure_class = "timeout_or_resource"
        deterministic = True
        evidence.append(
            {
                "kind": "termination_reason",
                "value": (
                    trajectory.termination_reason.value if trajectory.termination_reason else None
                ),
                "status": trajectory.status.value,
            }
        )
    else:
        for idx, log in enumerate(trajectory.tool_log):
            if log.get("success") is True:
                continue
            err_text = str(log.get("error") or "")
            tool_name = str(log.get("tool") or "unknown")
            evidence.append(
                {
                    "kind": "tool_log",
                    "tool": tool_name,
                    "index": idx,
                    "error": err_text,
                }
            )
            if "invalid arguments" in err_text.lower() or "validation" in err_text.lower():
                failure_class = "tool_arguments"
                deterministic = True
                break
            failure_class = "tool_execution"
            deterministic = True
            break

        if not deterministic and trajectory.grade is not None:
            if trajectory.grade.state_diff:
                failure_class = "grader_contract"
                deterministic = True
                evidence.append(
                    {
                        "kind": "state_diff",
                        "keys": sorted(trajectory.grade.state_diff.keys()),
                    }
                )
            elif isinstance(trajectory.grade.reasons, dict) and trajectory.grade.reasons:
                failure_class = "grader_contract"
                deterministic = True
                evidence.append(
                    {"kind": "grade_reasons", "keys": sorted(trajectory.grade.reasons.keys())}
                )

        # --- Extended heuristics for richer evidence ---

        # Detect connection errors in conversation messages (infrastructure issues)
        if not deterministic:
            connection_errors = 0
            for msg in trajectory.messages:
                if msg.content and _CONNECTION_ERROR_RE.search(msg.content):
                    connection_errors += 1
            if connection_errors > 0:
                failure_class = "infrastructure"
                deterministic = True
                evidence.append(
                    {
                        "kind": "connection_errors",
                        "count": connection_errors,
                    }
                )

        # Extract FAIL patterns from grading reasons string
        if not evidence and trajectory.grade and isinstance(trajectory.grade.reasons, str):
            reasons = trajectory.grade.reasons
            fail_patterns = [r.strip() for r in reasons.split("|") if "FAIL" in r.upper()]
            if fail_patterns:
                evidence.append(
                    {
                        "kind": "grade_fail_patterns",
                        "patterns": fail_patterns[:5],
                    }
                )

        # Detect missing required tool calls (grading expects files but tool was never called)
        if trajectory.grade and isinstance(trajectory.grade.reasons, str):
            tools_used = {log.get("tool") for log in trajectory.tool_log}
            if "No files match" in trajectory.grade.reasons and "write_file" not in tools_used:
                evidence.append(
                    {
                        "kind": "missing_tool",
                        "tool": "write_file",
                        "detail": "Grading expects output files but write_file was never called",
                    }
                )

    confidence = 1.0 if deterministic else 0.5
    return {
        "task_id": trajectory.task_id,
        "trial_index": trajectory.trial_index,
        "status": trajectory.status.value,
        "termination_reason": (
            trajectory.termination_reason.value if trajectory.termination_reason else None
        ),
        "failure_class": failure_class,
        "deterministic": deterministic,
        "confidence": confidence,
        "evidence": evidence,
    }


def summarize_failure_attributions(attributions: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate attribution stats for reporting."""
    by_class = Counter(a.get("failure_class", "unknown") for a in attributions)
    by_tool = Counter()
    deterministic_count = 0

    for attribution in attributions:
        if attribution.get("deterministic"):
            deterministic_count += 1
        for ev in attribution.get("evidence", []):
            tool = ev.get("tool")
            if tool:
                by_tool[str(tool)] += 1

    total = len(attributions)
    return {
        "total_failed_attempts": total,
        "deterministic_attribution_coverage": (deterministic_count / total) if total else None,
        "by_failure_class": dict(by_class),
        "by_tool": dict(by_tool),
    }
