"""Reference impl of :class:`TranscriptRuleMatcher` — wraps
:func:`~tolokaforge.core.grading.transcript.evaluate_transcript_rules`.

Registered under the name ``default`` in the
``tolokaforge.transcript_rule_matchers`` entry-point group. The reference
impl scores every kind of rule the shipped
:class:`~tolokaforge.runner.models.TranscriptRulesConfig` schema declares;
a downstream package that wants to score a different vocabulary registers
an alternative matcher alongside — no framework PR.

This module holds the only concrete impl of :class:`TranscriptRuleMatcher`
in the shipping distribution, so the ``.importlinter`` contract can forbid
composite from importing it without also forbidding the Protocol module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tolokaforge.core.grading.transcript import evaluate_transcript_rules
from tolokaforge.core.grading.transcript_rule_matcher import TranscriptRuleMatcher

if TYPE_CHECKING:
    from tolokaforge.core.grading.trace_timeline import TrialTimeline
    from tolokaforge.runner.models import (
        TranscriptEvaluationResult,
        TranscriptRulesConfig,
    )

__all__ = [
    "DefaultTranscriptRuleMatcher",
]


class DefaultTranscriptRuleMatcher:
    """Delegate to :func:`evaluate_transcript_rules` for the whole config."""

    def evaluate(
        self,
        *,
        rules: TranscriptRulesConfig,
        timeline: TrialTimeline,
    ) -> TranscriptEvaluationResult:
        return evaluate_transcript_rules(timeline, rules)


def _default_transcript_rule_matcher_factory() -> TranscriptRuleMatcher:
    """Entry-point factory. Arg-less; returns a fresh matcher instance."""
    return DefaultTranscriptRuleMatcher()
