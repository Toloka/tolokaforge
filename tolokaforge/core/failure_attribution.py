"""Deterministic failure attribution from trajectory artifacts."""

from __future__ import annotations

import re
from collections import Counter
from enum import Enum
from typing import Any

from tolokaforge.core.models import (
    TerminationReason,
    ToolExecutionStatus,
    Trajectory,
    TrialStatus,
)

DETERMINISTIC_CLASSES = {
    "tool_arguments",
    "tool_execution",
    "grader_contract",
    "grading_failure",
    "infrastructure",
    "timeout_or_resource",
    "provision_failure",
}

_CONNECTION_ERROR_RE = re.compile(
    r"ERR_CONNECTION_REFUSED|ECONNREFUSED|Connection refused|net::ERR_",
    re.IGNORECASE,
)


class TrialOutcomeClass(str, Enum):
    """Whether a trial measured the agent, and if not, whose fault that was.

    ``MEASURED`` — the agent was given its budget and the outcome describes
    how it performed, including every way it can perform badly (a spent turn
    or wall-clock budget, a repeated-action loop, a provider error we cannot
    attribute).

    ``HARNESS_ERROR`` — something broke and the evidence points at us. Counted
    in the rates like any other failure, because excluding it would remove our
    own defects from the denominator; a non-zero count is a run-health signal.

    ``INFRASTRUCTURE_ABORT`` — the trial never happened. No grade is produced
    and it is excluded from every rate.

    ``UNGRADEABLE`` — the trial happened and the agent was measured, but grading
    itself could not produce a verdict, and the fault is ours. Counted in
    ``measured_trials`` for the same reason ``HARNESS_ERROR`` is, excluded from
    ``scored_trials`` because no grade exists, and a non-pass for every rate.
    """

    MEASURED = "measured"
    HARNESS_ERROR = "harness_error"
    INFRASTRUCTURE_ABORT = "infrastructure_abort"
    UNGRADEABLE = "ungradeable"


EXCLUDED_TYPED_REASONS = frozenset(
    {
        TerminationReason.API_TIMEOUT,
        TerminationReason.PROVISION_ERROR,
        TerminationReason.RATE_LIMIT,
    }
)
"""The termination reasons that exclude a trial from the measured denominator.

Membership is earned by *typed* evidence: every one of these reasons is
produced from an exception type or an HTTP status, never from matching prose
against an exception message. A reason produced by text matching cannot gate
exclusion — a context-window overflow and a malformed tool schema both read as
"an API error" — and excluding a trial the agent actually failed inflates every
benchmark number with nothing in the output to show it. Counting a trial the
provider killed deflates them instead, visibly and boundedly, so that is the
direction the tie breaks. ``tests/canonical/test_termination_reason_reachability.py``
fails if any reason here becomes producible from prose.
"""


def classify_trial_outcome(trajectory: Trajectory) -> TrialOutcomeClass:
    """Return whether *trajectory* measured the agent.

    ``grading_error`` is read before the pair, and unconditionally: it is typed
    evidence that *we* failed, while exclusion is earned by typed evidence that
    the provider or the substrate killed the trial. Our own defects stay in the
    denominator, so a refusal outranks any reason that would have removed one.
    It outranks every *other* reason too, including the ones that would have
    named the fault ours by another name: a trial we could not grade is reported
    ungradeable even when its own termination was also a defect of ours, so it
    leaves ``harness_errors`` alone and lands in ``ungradeable`` instead.

    Total over ``(status, termination_reason)`` and gated on the exclusion
    allowlist, so a reason this function has never heard of is ``MEASURED``.
    That default is the point: an unrecognised reason that fell out of the
    denominator would be invisible in the output, while one that stayed in is
    visible in ``outcomes_by_reason`` and recoverable without a rerun.
    """
    if trajectory.grading_error is not None:
        return TrialOutcomeClass.UNGRADEABLE
    reason = trajectory.termination_reason
    if reason in EXCLUDED_TYPED_REASONS:
        return TrialOutcomeClass.INFRASTRUCTURE_ABORT
    if reason is TerminationReason.ERROR:
        return TrialOutcomeClass.HARNESS_ERROR
    if reason is None and trajectory.status in (
        TrialStatus.ERROR,
        TrialStatus.TIMEOUT,
        TrialStatus.FAILED,
    ):
        return TrialOutcomeClass.HARNESS_ERROR
    return TrialOutcomeClass.MEASURED


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

    if trajectory.grading_error is not None:
        # Ahead of every other branch, not only the tool-log scan: an ungradeable
        # trial has no failed call and no grade to inspect, so it would come out
        # ``model_reasoning``, and a refusal is the cause even when the trial's own
        # termination would have attributed the failure elsewhere.
        failure_class = "grading_failure"
        deterministic = True
        evidence.append({"kind": "grading_error", "error": trajectory.grading_error})
    elif trajectory.termination_reason == TerminationReason.PROVISION_ERROR:
        failure_class = "provision_failure"
        deterministic = True
        evidence.append(
            {
                "kind": "termination_reason",
                "value": trajectory.termination_reason.value,
                "status": trajectory.status.value,
            }
        )
    elif trajectory.termination_reason in (
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
        for idx, call in enumerate(trajectory.tool_log):
            if call.status is ToolExecutionStatus.SUCCESS:
                continue
            # A failed call records its failure text as the call's output.
            err_text = call.output
            evidence.append(
                {
                    "kind": "tool_log",
                    "tool": call.tool_name,
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
            tools_used = {call.tool_name for call in trajectory.tool_log}
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
        "outcome_class": classify_trial_outcome(trajectory).value,
        "failure_class": failure_class,
        "deterministic": deterministic,
        "confidence": confidence,
        "evidence": evidence,
    }


def summarize_failure_attributions(attributions: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate attribution stats for reporting.

    ``by_outcome_class`` splits the attempts by whose fault they were, so a
    reader of the summary alone can tell an agent failure from a trial that
    never ran.
    """
    by_class = Counter(a.get("failure_class", "unknown") for a in attributions)
    by_outcome_class = Counter(a.get("outcome_class", "unknown") for a in attributions)
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
        "by_outcome_class": dict(by_outcome_class),
        "by_tool": dict(by_tool),
    }
