"""Transcript-rules composite dispatch.

Every deployment shape's transcript-rules scoring goes through
:func:`grade_transcript_rules`. The composite forwards the timeline to the
resolved
:class:`~tolokaforge.core.grading.transcript_rule_matcher.TranscriptRuleMatcher`
seam and applies the events-less-trial gate (:func:`scored_transcript_rules`)
above the matcher so every topology books the same accounting for a trial
that left no evidence to score against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.grading.key_manifest import NO_TIMELINE_EVENTS_SKIP
from tolokaforge.core.grading.transcript import scored_transcript_rules
from tolokaforge.runner.grading_ledger import transcript_rules_author_keys

if TYPE_CHECKING:
    from tolokaforge.core.grading.trace_timeline import TrialTimeline
    from tolokaforge.core.grading.transcript import (
        TranscriptEvaluationResult,
        TranscriptRulesConfig,
    )
    from tolokaforge.core.grading.transcript_rule_matcher import TranscriptRuleMatcher
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import KeyAccountingRecord


def grade_transcript_rules(
    *,
    trial_id: str,
    config: TranscriptRulesConfig,
    timeline: TrialTimeline,
    matcher: TranscriptRuleMatcher,
    logger: StructuredLogger,
) -> tuple[TranscriptEvaluationResult | None, dict[str, KeyAccountingRecord]]:
    """Score the pack's transcript rules against a trial timeline.

    Returns ``(result, accounted_keys)``. A ``None`` result is the empty-timeline
    decision coming back empty — every rule is skipped and its key gets an
    ``NO_TIMELINE_EVENTS_SKIP`` record. When only the activity floor is scored,
    its siblings are recorded skipped first so the floor's own record survives
    the blanket skip.

    :func:`scored_transcript_rules` stays inside this composite — it is the
    events-less-trial gate every deployment topology needs applied at the same
    layer, not per-matcher policy. The resolved
    :class:`~tolokaforge.core.grading.transcript_rule_matcher.TranscriptRuleMatcher`
    is invoked with the gate-narrowed config: a plug-in impl (``default`` in
    the shipping config; a downstream ruleset alongside) that owns the
    actual per-rule scoring.

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

    result = matcher.evaluate(rules=scored_rules, timeline=timeline)
    return result, {**skipped_siblings, **result.accounted_keys}
