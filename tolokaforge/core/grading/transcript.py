"""Transcript-based grading rules"""

import re

from tolokaforge.core.grading.trace_timeline import (
    TraceEvent,
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


def _event_text(event: TraceEvent) -> str:
    """A turn's text, or a tool result's recorded output."""
    if event.kind is TraceEventKind.TOOL_RESULT:
        return event.result or ""
    return event.text or ""


def _searchable_text(timeline: TrialTimeline) -> str:
    """Everything said or returned during the trial, as one string."""
    return " ".join(
        _event_text(event) for event in timeline.events if event.kind in _SEARCHABLE_KINDS
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

    def check_min_assistant_turns(
        self, timeline: TrialTimeline, min_assistant_turns: int | None
    ) -> tuple[float, str]:
        """Check the agent produced at least the declared number of turns.

        A "turn" is one assistant generation, the same counter ``max_turns``
        bounds from above.
        """
        if min_assistant_turns is None:
            return 1.0, ""

        actual_turns = len(assistant_texts(timeline))

        if actual_turns >= min_assistant_turns:
            return 1.0, ""
        return 0.0, (
            f"Assistant turn count {actual_turns} below min_assistant_turns "
            f"of {min_assistant_turns}"
        )

    def check_tool_expectations(
        self,
        timeline: TrialTimeline,
        required_tools: list[str] | None,
        disallowed_tools: list[str] | None,
    ) -> tuple[float, list[str]]:
        """Check tool usage expectations.

        A call with no record did not run — but only while the timeline carries
        records at all. Without them nothing knows whether the declared calls ran,
        so the check fails naming that rather than reading absent evidence as
        "never used", which would pass every disallowed tool the trial called.
        """
        reasons = []
        score = 1.0

        calls = attempted_calls(timeline)
        if not timeline.records_present and calls and (required_tools or disallowed_tools):
            declared = ", ".join(sorted({call.tool_name for call in calls}))
            return 0.0, [
                "Tool expectations unevaluatable: the trial carries no tool-call record, "
                f"so whether it ran the calls it declared ({declared}) is unknown"
            ]

        tools_used = {call.tool_name for call in calls if call.status is not None}

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
        min_assistant_turns: int | None = None,
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

        floor_score, floor_reason = self.check_min_assistant_turns(timeline, min_assistant_turns)
        if floor_reason:
            all_reasons.append(floor_reason)

        # The activity floor gates the average rather than joining it: a fifth
        # zero bucket scores 0.8, which is the default ``pass_threshold``, so a
        # declared floor would be unable to fail a trial.
        scores = [contain_score, regex_score, turns_score, tools_score]
        final_score = sum(scores) / len(scores) * floor_score

        return final_score, "; ".join(all_reasons) if all_reasons else "All checks passed"
