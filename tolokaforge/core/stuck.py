"""Stuck detection heuristics"""

from collections import Counter
from typing import Any

from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import Message, MessageRole


class StuckDetector:
    """Detect when agent is stuck in a loop"""

    def __init__(
        self,
        max_repeated_tool_calls: int = 10,
        max_idle_turns: int = 12,
    ):
        self.max_repeated_tool_calls = max_repeated_tool_calls
        self.max_idle_turns = max_idle_turns
        self.logger = get_logger("stuck_detector")

    def is_stuck(self, messages: list[Message], tool_logs: list[dict[str, Any]]) -> bool:
        """
        Check if agent appears to be stuck

        Args:
            messages: Conversation history
            tool_logs: Tool execution logs

        Returns:
            True if agent appears stuck
        """
        # Check repeated tool calls
        if self._has_repeated_tool_calls(tool_logs):
            self.logger.debug("Stuck detected - repeated tool calls", logs_count=len(tool_logs))
            return True

        # Check idle turns (no tool calls for extended period)
        if self._has_idle_turns(messages):
            self.logger.debug("Stuck detected - idle turns", messages_count=len(messages))
            return True

        # Check looping content
        if self._has_looping_content(messages):
            self.logger.debug("Stuck detected - looping content")
            return True

        return False

    def _has_repeated_tool_calls(self, tool_logs: list[dict[str, Any]]) -> bool:
        """Check for repeated identical tool calls"""
        if len(tool_logs) < self.max_repeated_tool_calls:
            return False

        # Look at last N tool calls
        recent_calls = tool_logs[-self.max_repeated_tool_calls :]

        # Create signature for each call (tool + args)
        signatures = []
        for log in recent_calls:
            sig = f"{log.get('tool')}:{str(log.get('arguments', {}))}"
            signatures.append(sig)

        # Check if same signature appears too many times
        counts = Counter(signatures)
        most_common_count = counts.most_common(1)[0][1] if counts else 0

        return most_common_count >= self.max_repeated_tool_calls

    def _has_idle_turns(self, messages: list[Message]) -> bool:
        """Check for too many turns without tool calls"""
        if len(messages) < self.max_idle_turns:
            return False

        # Look at recent messages
        recent_messages = messages[-self.max_idle_turns :]

        # Count assistant messages without tool calls
        idle_count = 0
        for msg in recent_messages:
            if msg.role == MessageRole.ASSISTANT:
                if not msg.tool_calls or len(msg.tool_calls) == 0:
                    idle_count += 1

        return idle_count >= self.max_idle_turns

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
