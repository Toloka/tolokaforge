"""Composite grading dispatch — the surface every topology consumes.

Every deployment shape (aggregate image, independent grader container,
future trajectory-storage callback, future snapshot, future shared-mount)
runs one composite grade against a :class:`GradingSubstrate`. This module
carries the per-component helpers extracted from the runner's grading
path so they can be reused verbatim by every topology.

Extract-refactor discipline: the module functions are behaviour-preserving
lifts of runner-side methods on ``RunnerServiceImpl``. The runner keeps
its methods as thin wrappers that delegate here, so behaviour parity
holds by construction — every existing canonical test still exercises
the same code path.

Extraction is being landed in slices; slice B (this file's first
iteration) carries the two transcript-shape helpers (`grade_transcript_
rules`, `grade_trace_checks`) that need only a :class:`TrialTimeline`.
The substrate-dependent helpers (state_checks, custom_checks, llm_judge)
land in the subsequent slice on this branch, once the runner-side gRPC
substrate service and the ``LiveRunnerCallbackGradingSubstrate`` are
wired end-to-end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.grading.key_manifest import NO_TIMELINE_EVENTS_SKIP
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.transcript import (
    evaluate_transcript_rules,
    scored_transcript_rules,
)
from tolokaforge.runner.grading_ledger import transcript_rules_author_keys

if TYPE_CHECKING:
    from tolokaforge.core.grading.key_manifest import KeyAccountingRecord
    from tolokaforge.core.grading.trace_checks import TraceChecksResult
    from tolokaforge.core.grading.trace_timeline import TrialTimeline
    from tolokaforge.core.grading.transcript import (
        TranscriptEvaluationResult,
        TranscriptRulesConfig,
    )
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.runner.models import TraceChecksConfig


def grade_transcript_rules(
    *,
    trial_id: str,
    config: TranscriptRulesConfig,
    timeline: TrialTimeline,
    logger: StructuredLogger,
) -> tuple[TranscriptEvaluationResult | None, dict[str, KeyAccountingRecord]]:
    """Score the pack's transcript rules against a trial timeline.

    Returns ``(result, accounted_keys)``. A ``None`` result is the empty-timeline
    decision coming back empty — every rule is skipped and its key gets an
    ``NO_TIMELINE_EVENTS_SKIP`` record. When only the activity floor is scored,
    its siblings are recorded skipped first so the floor's own record survives
    the blanket skip.

    Substrate-independent: reads only the timeline (the runner already built
    it from the transcript). Called identically by every deployment topology.
    """
    scored_rules = scored_transcript_rules(timeline, config)
    if scored_rules is None:
        logger.info(
            f"GradeTrial: {trial_id} - Skipping transcript rules (no messages or tool calls)"
        )
        return None, dict.fromkeys(transcript_rules_author_keys(), NO_TIMELINE_EVENTS_SKIP)

    skipped_siblings: dict[str, KeyAccountingRecord] = {}
    if timeline.events:
        logger.info(f"GradeTrial: {trial_id} - Evaluating transcript rules")
    else:
        logger.info(
            f"GradeTrial: {trial_id} - Evaluating the activity floor alone "
            "(no messages or tool calls)"
        )
        skipped_siblings = dict.fromkeys(transcript_rules_author_keys(), NO_TIMELINE_EVENTS_SKIP)

    result = evaluate_transcript_rules(timeline, scored_rules)
    return result, {**skipped_siblings, **result.accounted_keys}


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
