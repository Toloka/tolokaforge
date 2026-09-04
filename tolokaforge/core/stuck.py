"""Stuck detection heuristics"""

import hashlib
from collections import Counter
from collections.abc import Sequence

from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import Message, MessageRole, RecordedToolCall


class StuckDetector:
    """Detect when agent is stuck in a loop"""

    def __init__(self, max_repeated_tool_calls: int):
        self.max_repeated_tool_calls = max_repeated_tool_calls
        self.logger = get_logger("stuck_detector")

    def is_stuck(self, messages: list[Message], tool_calls: Sequence[RecordedToolCall]) -> bool:
        """
        Check if agent appears to be stuck

        Args:
            messages: Conversation history
            tool_calls: The agent's own recorded tool calls, in trial order

        Returns:
            True if agent appears stuck
        """
        # Check repeated tool calls
        if self._has_repeated_tool_calls(tool_calls):
            self.logger.debug("Stuck detected - repeated tool calls", logs_count=len(tool_calls))
            return True

        # Check looping content
        if self._has_looping_content(messages):
            self.logger.debug("Stuck detected - looping content")
            return True

        return False

    def _has_repeated_tool_calls(self, tool_calls: Sequence[RecordedToolCall]) -> bool:
        """Fire when the same tool call produced the same bytes back over and over.

        Identity is ``(tool_name, arguments, sha256(output))``: same tool + same
        args + same result-bytes across ``max_repeated_tool_calls`` turns is
        no progress. Same tool + same args with *different* result bytes is
        progress — the model got new information every turn — and does not
        fire. Hashing the output bounds the per-call signature to O(1) memory,
        so a long trial over ``pytest``-sized outputs (10-100 KB) cannot blow
        up the Counter.
        """
        if len(tool_calls) < self.max_repeated_tool_calls:
            return False

        recent_calls = tool_calls[-self.max_repeated_tool_calls :]

        signatures = []
        for call in recent_calls:
            output_digest = hashlib.sha256(call.output.encode("utf-8")).hexdigest()
            sig = f"{call.tool_name}:{str(call.arguments)}:{output_digest}"
            signatures.append(sig)

        counts = Counter(signatures)
        most_common_count = counts.most_common(1)[0][1] if counts else 0

        return most_common_count >= self.max_repeated_tool_calls

    def _has_looping_content(self, messages: list[Message]) -> bool:
        """Check for repeating content patterns indicating actual looping"""
        # Get recent assistant messages
        assistant_msgs = [
            msg.content for msg in messages[-10:] if msg.role == MessageRole.ASSISTANT
        ]

        if len(assistant_msgs) < 5:
            return False

        # Extract trigrams from messages
        trigrams = []
        for msg in assistant_msgs:
            words = msg.lower().split()
            if len(words) >= 3:
                for i in range(len(words) - 2):
                    trigram = " ".join(words[i : i + 3])
                    trigrams.append(trigram)

        if not trigrams:
            return False

        # Check for high-frequency trigrams
        # Use higher threshold (10+) to avoid false positives from technical terminology
        # that naturally repeats in domain-specific conversations
        counts = Counter(trigrams)
        most_common_count = counts.most_common(1)[0][1] if counts else 0

        return most_common_count >= 10
