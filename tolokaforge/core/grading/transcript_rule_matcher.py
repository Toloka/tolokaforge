"""Transcript-rule matcher seam — Protocol + FactoryAlias.

A :class:`TranscriptRuleMatcher` scores the WHOLE
:class:`~tolokaforge.runner.models.TranscriptRulesConfig` against a
:class:`~tolokaforge.core.grading.trace_timeline.TrialTimeline` and returns
one :class:`~tolokaforge.runner.models.TranscriptEvaluationResult`. The
seam is holistic — one matcher per config, NOT one matcher per rule kind —
because a per-kind seam would only be extensible via a
:class:`TranscriptRulesConfig` schema extension, and that schema is what
would truly gate the operator win.

Discovery goes through
:func:`~tolokaforge.core.plugin_registry.load_transcript_rule_matcher` over
the ``tolokaforge.transcript_rule_matchers`` entry-point group; a downstream
package registers an alternative matcher alongside the shipping ``default``
reference impl without a framework PR.

The reference impl lives in
:mod:`tolokaforge.core.grading.default_transcript_rule_matcher` — this
Protocol module carries no behaviour so the composite dispatch can name
:class:`TranscriptRuleMatcher` without ever reaching the reference impl
through it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from tolokaforge.core.grading.trace_timeline import TrialTimeline
    from tolokaforge.runner.models import (
        TranscriptEvaluationResult,
        TranscriptRulesConfig,
    )

__all__ = [
    "TranscriptRuleMatcher",
    "TranscriptRuleMatcherFactory",
]


@runtime_checkable
class TranscriptRuleMatcher(Protocol):
    """Score one :class:`TranscriptRulesConfig` against one :class:`TrialTimeline`."""

    def evaluate(
        self,
        *,
        rules: TranscriptRulesConfig,
        timeline: TrialTimeline,
    ) -> TranscriptEvaluationResult: ...


TranscriptRuleMatcherFactory = Callable[[], TranscriptRuleMatcher]
