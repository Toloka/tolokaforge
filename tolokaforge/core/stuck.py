"""Stuck detection heuristics"""

import hashlib
from collections import Counter
from collections.abc import Sequence

from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import RecordedToolCall


class StuckDetector:
    """Fire when the agent's own recent tool calls stopped producing new information.

    One heuristic: the last ``max_repeated_tool_calls`` recorded tool calls all
    carry the same ``(tool_name, arguments, sha256(output))`` identity. That is
    the shape of a stall the harness can see without reading agent prose —
    whether the *task* additionally requires the agent to act is a per-task
    assertion, answered by ``transcript_rules`` in the task's ``grading.yaml``.
    """

    def __init__(self, max_repeated_tool_calls: int):
        self.max_repeated_tool_calls = max_repeated_tool_calls
        self.logger = get_logger("stuck_detector")

    def is_stuck(self, tool_calls: Sequence[RecordedToolCall]) -> bool:
        """True when the recent tool-call window is a byte-for-byte repeat.

        Args:
            tool_calls: The agent's own recorded tool calls, in trial order.
        """
        if self._has_repeated_tool_calls(tool_calls):
            self.logger.debug("Stuck detected - repeated tool calls", logs_count=len(tool_calls))
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
