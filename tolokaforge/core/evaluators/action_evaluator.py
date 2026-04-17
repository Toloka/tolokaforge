"""Action evaluator - checks if required actions appear in trajectory"""

from tolokaforge.core.models import Message, MessageRole, RequiredAction, ToolCall


class ActionCheckResult:
    """Result of checking a single required action"""

    def __init__(
        self,
        action: RequiredAction,
        found: bool,
        matching_call: ToolCall = None,
    ):
        self.action = action
        self.found = found
        self.matching_call = matching_call

    def __repr__(self):
        return f"ActionCheckResult(action={self.action.action_id}, found={self.found})"


class ActionEvaluationResult:
    """Result of evaluating all required actions"""

    def __init__(
        self,
        score: float,
        action_results: list[ActionCheckResult],
        reasons: list[str] = None,
    ):
        self.score = score
        self.action_results = action_results
        self.reasons = reasons or []

    def __repr__(self):
        found_count = sum(1 for r in self.action_results if r.found)
        total = len(self.action_results)
        return f"ActionEvaluationResult(score={self.score:.2f}, found={found_count}/{total})"


class ActionEvaluator:
    """Evaluates if required actions appear in trajectory"""

    @staticmethod
    def compare_tool_call(required: RequiredAction, tool_call: ToolCall, requestor: str) -> bool:
        """
        Compare a required action with an actual tool call

        Args:
            required: Required action specification
            tool_call: Actual tool call from trajectory
            requestor: Who made the call (assistant or user)

        Returns:
            True if tool call matches required action
        """
        # Check requestor matches
        if required.requestor != requestor:
            return False

        # Check name matches
        if required.name != tool_call.name:
            return False

        # Determine which arguments to compare
        if required.compare_args is None:
            # Compare all arguments from required action
            compare_keys = required.arguments.keys()
        else:
            # Compare only specified arguments
            compare_keys = required.compare_args

        # If no arguments to compare, it's a match
        if len(compare_keys) == 0:
            return True

        # Compare specified arguments
        for key in compare_keys:
            required_value = required.arguments.get(key)
            actual_value = tool_call.arguments.get(key)

            if required_value != actual_value:
                return False

        return True

    @staticmethod
    def extract_tool_calls_from_trajectory(
        trajectory: list[Message],
    ) -> list[tuple[ToolCall, str]]:
        """
        Extract all tool calls from trajectory with their requestor

        Args:
            trajectory: List of messages

        Returns:
            List of (ToolCall, requestor) tuples
        """
        tool_calls = []

        for message in trajectory:
            if message.tool_calls:
                # Determine requestor based on message role
                if message.role == MessageRole.ASSISTANT:
                    requestor = "assistant"
                elif message.role == MessageRole.USER:
                    requestor = "user"
                else:
                    # Tool results don't have tool_calls, but just in case
                    continue

                for tool_call in message.tool_calls:
                    tool_calls.append((tool_call, requestor))

        return tool_calls

    def evaluate_actions(
        self, trajectory: list[Message], required_actions: list[RequiredAction]
    ) -> ActionEvaluationResult:
        """
        Evaluate if all required actions appear in trajectory

        Args:
            trajectory: Complete message trajectory
            required_actions: List of required actions

        Returns:
            ActionEvaluationResult with score and details
        """
        if not required_actions:
            # No actions required, perfect score
            return ActionEvaluationResult(score=1.0, action_results=[], reasons=[])

        # Extract all tool calls from trajectory
        trajectory_calls = self.extract_tool_calls_from_trajectory(trajectory)

        # Check each required action
        action_results = []
        reasons = []

        for required in required_actions:
            found = False
            matching_call = None

            # Search for matching tool call
            for tool_call, requestor in trajectory_calls:
                if self.compare_tool_call(required, tool_call, requestor):
                    found = True
                    matching_call = tool_call
                    break

            result = ActionCheckResult(action=required, found=found, matching_call=matching_call)
            action_results.append(result)

            if not found:
                reasons.append(
                    f"Missing required action: {required.requestor}.{required.name}"
                    f"({required.arguments}) [id={required.action_id}]"
                )

        # Calculate score as fraction of required actions found
        found_count = sum(1 for r in action_results if r.found)
        score = found_count / len(required_actions) if required_actions else 1.0

        return ActionEvaluationResult(score=score, action_results=action_results, reasons=reasons)
