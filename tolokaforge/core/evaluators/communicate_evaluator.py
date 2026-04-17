"""Communication evaluator - checks if required information was communicated"""

import re

from tolokaforge.core.models import CommunicateInfo, Message, MessageRole


class CommunicationCheckResult:
    """Result of checking a single communication requirement"""

    def __init__(
        self,
        communicate_info: CommunicateInfo,
        found: bool,
        matched_in_message: str = None,
    ):
        self.communicate_info = communicate_info
        self.found = found
        self.matched_in_message = matched_in_message

    def __repr__(self):
        return f"CommunicationCheckResult(info='{self.communicate_info.info[:30]}...', found={self.found})"


class CommunicateEvaluationResult:
    """Result of evaluating all communication requirements"""

    def __init__(
        self,
        score: float,
        communication_results: list[CommunicationCheckResult],
        reasons: list[str] = None,
    ):
        self.score = score
        self.communication_results = communication_results
        self.reasons = reasons or []

    def __repr__(self):
        found_count = sum(1 for r in self.communication_results if r.found)
        total = len(self.communication_results)
        return f"CommunicateEvaluationResult(score={self.score:.2f}, found={found_count}/{total})"


class CommunicateEvaluator:
    """Evaluates if required information was communicated to user"""

    @staticmethod
    def check_info_communicated(
        info_text: str, messages: list[Message], case_sensitive: bool = False
    ) -> tuple[bool, str]:
        """
        Check if information was communicated in assistant messages

        Args:
            info_text: Information text to search for
            messages: List of messages to search
            case_sensitive: Whether to do case-sensitive matching

        Returns:
            (found, matched_message) tuple
        """
        # Only check assistant messages (messages to the user)
        assistant_messages = [m for m in messages if m.role == MessageRole.ASSISTANT]

        # Prepare search text
        search_text = info_text if case_sensitive else info_text.lower()

        # Try exact substring match first
        for message in assistant_messages:
            message_text = message.content if case_sensitive else message.content.lower()

            if search_text in message_text:
                return True, message.content[:200]

        # Try fuzzy keyword matching (all words must appear)
        # Split info into keywords (ignore common words)
        stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for"}
        keywords = [
            word.lower()
            for word in re.findall(r"\b\w+\b", info_text)
            if word.lower() not in stop_words and len(word) > 2
        ]

        if keywords:
            for message in assistant_messages:
                message_text = message.content.lower()
                # Check if all keywords appear in message
                if all(keyword in message_text for keyword in keywords):
                    return True, message.content[:200]

        return False, None

    def evaluate_communication(
        self, trajectory: list[Message], communicate_info: list[CommunicateInfo]
    ) -> CommunicateEvaluationResult:
        """
        Evaluate if all required information was communicated

        Args:
            trajectory: Complete message trajectory
            communicate_info: List of information that should be communicated

        Returns:
            CommunicateEvaluationResult with score and details
        """
        if not communicate_info:
            # No communication requirements, perfect score
            return CommunicateEvaluationResult(score=1.0, communication_results=[], reasons=[])

        # Check each communication requirement
        communication_results = []
        reasons = []

        for comm_info in communicate_info:
            found, matched_message = self.check_info_communicated(comm_info.info, trajectory)

            result = CommunicationCheckResult(
                communicate_info=comm_info,
                found=found,
                matched_in_message=matched_message,
            )
            communication_results.append(result)

            if not found and comm_info.required:
                reasons.append(f"Required information not communicated: '{comm_info.info[:50]}...'")

        # Calculate score
        # Only count required communication items
        required_items = [c for c in communicate_info if c.required]

        if not required_items:
            # No required items, perfect score
            score = 1.0
        else:
            # Score is fraction of required items that were communicated
            required_found = sum(
                1
                for i, r in enumerate(communication_results)
                if r.found and communicate_info[i].required
            )
            score = required_found / len(required_items)

        return CommunicateEvaluationResult(
            score=score, communication_results=communication_results, reasons=reasons
        )
