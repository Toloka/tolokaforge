"""Transcript-based grading rules"""

import re

from tolokaforge.core.grading.trace_timeline import (
    TraceEventKind,
    TrialTimeline,
    assistant_texts,
    attempted_calls,
)

# What a phrase rule searches: the trial's own text. ``role: system`` harness
# annotations are not timeline events, so a required phrase can never be
# satisfied by a termination notice the harness wrote.
_SEARCHABLE_KINDS = (
    TraceEventKind.USER_MESSAGE,
    TraceEventKind.ASSISTANT_MESSAGE,
    TraceEventKind.TOOL_RESULT,
)


def _searchable_text(timeline: TrialTimeline) -> str:
    """Everything said or returned during the trial, as one string."""
    return " ".join(
        (event.text if event.kind is not TraceEventKind.TOOL_RESULT else event.result) or ""
        for event in timeline.events
        if event.kind in _SEARCHABLE_KINDS
    )


class TranscriptChecker:
    """Check conversation transcript against rules"""

    def check_must_contain(
        self, timeline: TrialTimeline, phrases: list[str]
    ) -> tuple[float, list[str]]:
        """Check if transcript contains required phrases"""
        if not phrases:
            return 1.0, []

        full_transcript = _searchable_text(timeline)

        found = 0
        reasons = []

        for phrase in phrases:
            if phrase in full_transcript:
                found += 1
            else:
                reasons.append(f"Missing required phrase: '{phrase}'")

        score = found / len(phrases) if phrases else 1.0
        return score, reasons

    def check_disallowed_regex(
        self, timeline: TrialTimeline, patterns: list[str]
    ) -> tuple[float, list[str]]:
        """Check if transcript contains disallowed patterns"""
        if not patterns:
            return 1.0, []

        full_transcript = _searchable_text(timeline)

        violations = []
        for pattern in patterns:
            matches = re.findall(pattern, full_transcript, re.IGNORECASE)
            if matches:
                violations.append(f"Disallowed pattern '{pattern}' found: {matches[:3]}")

        score = 0.0 if violations else 1.0
        return score, violations

    def check_max_turns(self, timeline: TrialTimeline, max_turns: int | None) -> tuple[float, str]:
        """Check if conversation stayed within turn limit"""
        if max_turns is None:
            return 1.0, ""

        actual_turns = len(assistant_texts(timeline))

        if actual_turns <= max_turns:
            return 1.0, ""
        else:
            return 0.0, f"Exceeded max turns: {actual_turns} > {max_turns}"

    def check_tool_expectations(
        self,
        timeline: TrialTimeline,
        required_tools: list[str] | None,
        disallowed_tools: list[str] | None,
    ) -> tuple[float, list[str]]:
        """Check tool usage expectations"""
        reasons = []
        score = 1.0

        tools_used = {
            call.tool_name for call in attempted_calls(timeline) if call.status is not None
        }

        if required_tools:
            missing = set(required_tools) - tools_used
            if missing:
                score *= 0.5
                # Sort so the reason text is reproducible across runs — Python set
                # repr ordering is not stable, which made these reasons drift
                # spuriously across machines / processes.
                reasons.append(f"Missing required tools: {', '.join(sorted(missing))}")

        if disallowed_tools:
            violations = tools_used & set(disallowed_tools)
            if violations:
                score = 0.0
                reasons.append(f"Used disallowed tools: {', '.join(sorted(violations))}")

        return score, reasons

    def grade(
        self,
        timeline: TrialTimeline,
        must_contain: list[str] | None = None,
        disallow_regex: list[str] | None = None,
        max_turns: int | None = None,
        required_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> tuple[float, str]:
        """
        Grade transcript with all rules

        Returns:
            (score 0-1, reasons)
        """
        all_reasons = []

        # Check each rule
        contain_score, contain_reasons = self.check_must_contain(timeline, must_contain or [])
        all_reasons.extend(contain_reasons)

        regex_score, regex_reasons = self.check_disallowed_regex(timeline, disallow_regex or [])
        all_reasons.extend(regex_reasons)

        turns_score, turns_reason = self.check_max_turns(timeline, max_turns)
        if turns_reason:
            all_reasons.append(turns_reason)

        tools_score, tools_reasons = self.check_tool_expectations(
            timeline, required_tools, disallowed_tools
        )
        all_reasons.extend(tools_reasons)

        # Average scores
        scores = [contain_score, regex_score, turns_score, tools_score]
        final_score = sum(scores) / len(scores)

        return final_score, "; ".join(all_reasons) if all_reasons else "All checks passed"
