"""Transcript-based grading rules"""

import re
from typing import Any

from tolokaforge.core.models import Message, MessageRole


class TranscriptChecker:
    """Check conversation transcript against rules"""

    def __init__(self):
        pass

    def check_must_contain(
        self, messages: list[Message], phrases: list[str]
    ) -> tuple[float, list[str]]:
        """Check if transcript contains required phrases"""
        if not phrases:
            return 1.0, []

        # Combine all message content
        full_transcript = " ".join(msg.content for msg in messages)

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
        self, messages: list[Message], patterns: list[str]
    ) -> tuple[float, list[str]]:
        """Check if transcript contains disallowed patterns"""
        if not patterns:
            return 1.0, []

        full_transcript = " ".join(msg.content for msg in messages)

        violations = []
        for pattern in patterns:
            matches = re.findall(pattern, full_transcript, re.IGNORECASE)
            if matches:
                violations.append(f"Disallowed pattern '{pattern}' found: {matches[:3]}")

        score = 0.0 if violations else 1.0
        return score, violations

    def check_max_turns(self, messages: list[Message], max_turns: int | None) -> tuple[float, str]:
        """Check if conversation stayed within turn limit"""
        if max_turns is None:
            return 1.0, ""

        actual_turns = len([m for m in messages if m.role == MessageRole.ASSISTANT])

        if actual_turns <= max_turns:
            return 1.0, ""
        else:
            return 0.0, f"Exceeded max turns: {actual_turns} > {max_turns}"

    def check_tool_expectations(
        self,
        tool_log: list[dict[str, Any]],
        required_tools: list[str] | None,
        disallowed_tools: list[str] | None,
    ) -> tuple[float, list[str]]:
        """Check tool usage expectations"""
        reasons = []
        score = 1.0

        tools_used = {log.get("tool") for log in tool_log}

        if required_tools:
            missing = set(required_tools) - tools_used
            if missing:
                score *= 0.5
                reasons.append(f"Missing required tools: {missing}")

        if disallowed_tools:
            violations = tools_used & set(disallowed_tools)
            if violations:
                score = 0.0
                reasons.append(f"Used disallowed tools: {violations}")

        return score, reasons

    def grade(
        self,
        messages: list[Message],
        tool_log: list[dict[str, Any]],
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
        contain_score, contain_reasons = self.check_must_contain(messages, must_contain or [])
        all_reasons.extend(contain_reasons)

        regex_score, regex_reasons = self.check_disallowed_regex(messages, disallow_regex or [])
        all_reasons.extend(regex_reasons)

        turns_score, turns_reason = self.check_max_turns(messages, max_turns)
        if turns_reason:
            all_reasons.append(turns_reason)

        tools_score, tools_reasons = self.check_tool_expectations(
            tool_log, required_tools, disallowed_tools
        )
        all_reasons.extend(tools_reasons)

        # Average scores
        scores = [contain_score, regex_score, turns_score, tools_score]
        final_score = sum(scores) / len(scores)

        return final_score, "; ".join(all_reasons) if all_reasons else "All checks passed"
