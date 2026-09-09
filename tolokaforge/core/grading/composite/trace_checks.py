"""Trace-checks composite dispatch.

Naming: this module lives at ``tolokaforge.core.grading.composite.trace_checks``,
distinct from the sibling reference-impl module
``tolokaforge.core.grading.trace_checks`` where :func:`evaluate_trace_checks`
is defined. No shadow at import time — every caller imports symbols by
fully-qualified name, not the short-name module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.grading.trace_checks import evaluate_trace_checks

if TYPE_CHECKING:
    from tolokaforge.core.grading.trace_checks import TraceChecksResult
    from tolokaforge.core.grading.trace_timeline import TrialTimeline
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.runner.models import TraceChecksConfig


def grade_trace_checks(
    *,
    trial_id: str,
    config: TraceChecksConfig,
    timeline: TrialTimeline,
    logger: StructuredLogger,
) -> TraceChecksResult:
    """Score the pack's trace checks over the trial's event timeline.

    A result carrying no constraint verdicts is the trial that left no trace
    of itself — a timeline with neither a conversational turn nor a tool
    call — where every constraint would score against evidence the trial
    does not have. The component is then left out of the combine, and the
    evaluator's own accounting records the skip against each kind the block
    declared.

    Substrate-independent: reads only the timeline. Called identically by
    every deployment topology.
    """
    result = evaluate_trace_checks(timeline, config)
    if not result.constraints:
        logger.info(f"GradeTrial: {trial_id} - Skipping trace checks (no messages or tool calls)")
        return result
    logger.info(f"GradeTrial: {trial_id} - Trace checks: score={result.score:.2f}")
    return result
