"""Tests for tau2-faithful evaluators"""

import pytest

from tolokaforge.core.evaluators.action_evaluator import ActionEvaluator
from tolokaforge.core.evaluators.communicate_evaluator import CommunicateEvaluator
from tolokaforge.core.evaluators.environment_evaluator import EnvironmentEvaluator
from tolokaforge.core.models import (
    CommunicateInfo,
    Message,
    MessageRole,
    RequiredAction,
    StateChecksConfig,
    ToolCall,
)

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestActionEvaluator:
    """Test action evaluator"""

    def test_simple_action_match(self):
        """Test matching a simple required action"""
        evaluator = ActionEvaluator()

        # Create a required action
        required = RequiredAction(
            action_id="test_1",
            requestor="assistant",
            name="get_customer_by_phone",
            arguments={"phone": "555-1234"},
            compare_args=["phone"],
        )

        # Create messages with matching tool call
        tool_call = ToolCall(
            id="call_1",
            name="get_customer_by_phone",
            arguments={"phone": "555-1234", "extra": "ignored"},
        )
        message = Message(
            role=MessageRole.ASSISTANT, content="Let me look that up", tool_calls=[tool_call]
        )

        # Evaluate
        result = evaluator.evaluate_actions([message], [required])

        assert result.score == 1.0
        assert len(result.action_results) == 1
        assert result.action_results[0].found

    def test_action_missing(self):
        """Test when required action is missing"""
        evaluator = ActionEvaluator()

        required = RequiredAction(
            action_id="test_1",
            requestor="assistant",
            name="get_customer_by_phone",
            arguments={"phone": "555-1234"},
        )

        # Message without the required tool call
        message = Message(role=MessageRole.ASSISTANT, content="I can't find that")

        result = evaluator.evaluate_actions([message], [required])

        assert result.score == 0.0
        assert len(result.action_results) == 1
        assert not result.action_results[0].found
        assert len(result.reasons) > 0

    def test_partial_match_compare_args(self):
        """Test partial argument matching with compare_args"""
        evaluator = ActionEvaluator()

        # Only compare phone, ignore other arguments
        required = RequiredAction(
            action_id="test_1",
            requestor="user",
            name="toggle_data",
            arguments={"enabled": True},
            compare_args=[],  # Don't compare any arguments, just tool name
        )

        tool_call = ToolCall(id="call_1", name="toggle_data", arguments={})
        message = Message(role=MessageRole.USER, content="Toggling data", tool_calls=[tool_call])

        result = evaluator.evaluate_actions([message], [required])

        assert result.score == 1.0
        assert result.action_results[0].found

    def test_wrong_requestor(self):
        """Test that requestor must match"""
        evaluator = ActionEvaluator()

        # Require assistant to make the call
        required = RequiredAction(
            action_id="test_1", requestor="assistant", name="toggle_data", arguments={}
        )

        # But user makes the call
        tool_call = ToolCall(id="call_1", name="toggle_data", arguments={})
        message = Message(role=MessageRole.USER, content="Toggling", tool_calls=[tool_call])

        result = evaluator.evaluate_actions([message], [required])

        assert result.score == 0.0

    def test_multiple_actions(self):
        """Test with multiple required actions"""
        evaluator = ActionEvaluator()

        required_actions = [
            RequiredAction(action_id="1", requestor="user", name="toggle_data", arguments={}),
            RequiredAction(action_id="2", requestor="assistant", name="refuel_data", arguments={}),
        ]

        # Only one action present
        tool_call = ToolCall(id="call_1", name="toggle_data", arguments={})
        message = Message(role=MessageRole.USER, content="Toggling", tool_calls=[tool_call])

        result = evaluator.evaluate_actions([message], required_actions)

        assert result.score == 0.5  # 1 out of 2 found
        assert result.action_results[0].found
        assert not result.action_results[1].found


@pytest.mark.unit
class TestCommunicateEvaluator:
    """Test communication evaluator"""

    def test_exact_match(self):
        """Test exact string matching"""
        evaluator = CommunicateEvaluator()

        info = CommunicateInfo(info="Your data plan has been refueled", required=True)

        messages = [
            Message(role=MessageRole.ASSISTANT, content="Your data plan has been refueled."),
        ]

        result = evaluator.evaluate_communication(messages, [info])

        assert result.score == 1.0
        assert result.communication_results[0].found

    def test_keyword_matching(self):
        """Test fuzzy keyword matching"""
        evaluator = CommunicateEvaluator()

        info = CommunicateInfo(info="data plan refueled successfully", required=True)

        messages = [
            Message(
                role=MessageRole.ASSISTANT,
                content="Great! I've successfully refueled your data plan.",
            ),
        ]

        result = evaluator.evaluate_communication(messages, [info])

        assert result.score == 1.0
        assert result.communication_results[0].found

    def test_case_insensitive(self):
        """Test case insensitive matching"""
        evaluator = CommunicateEvaluator()

        info = CommunicateInfo(info="DATA PLAN REFUELED", required=True)

        messages = [
            Message(role=MessageRole.ASSISTANT, content="Your data plan has been refueled."),
        ]

        result = evaluator.evaluate_communication(messages, [info])

        assert result.score == 1.0

    def test_missing_communication(self):
        """Test when information not communicated"""
        evaluator = CommunicateEvaluator()

        info = CommunicateInfo(info="Your bill is $50", required=True)

        messages = [
            Message(role=MessageRole.ASSISTANT, content="I've looked up your account."),
        ]

        result = evaluator.evaluate_communication(messages, [info])

        assert result.score == 0.0
        assert not result.communication_results[0].found

    def test_optional_communication(self):
        """Test optional communication item"""
        evaluator = CommunicateEvaluator()

        required_info = CommunicateInfo(info="data refueled", required=True)
        optional_info = CommunicateInfo(info="extra details", required=False)

        messages = [
            Message(role=MessageRole.ASSISTANT, content="Your data has been refueled."),
        ]

        result = evaluator.evaluate_communication(messages, [required_info, optional_info])

        # Score should be 1.0 because only required items count
        assert result.score == 1.0
        assert result.communication_results[0].found
        assert not result.communication_results[1].found

    def test_only_checks_assistant_messages(self):
        """Test that only assistant messages are checked"""
        evaluator = CommunicateEvaluator()

        info = CommunicateInfo(info="data refueled", required=True)

        messages = [
            Message(role=MessageRole.USER, content="Your data has been refueled."),  # User says it
            Message(role=MessageRole.ASSISTANT, content="Thank you."),  # Assistant doesn't
        ]

        result = evaluator.evaluate_communication(messages, [info])

        assert result.score == 0.0  # Assistant didn't communicate it


@pytest.mark.unit
class TestEnvironmentEvaluator:
    """Test environment evaluator - DB hash checks only (no external assertions)"""

    def test_db_hash_match(self):
        """Test DB hash match when enabled"""
        evaluator = EnvironmentEvaluator()

        config = StateChecksConfig(
            db_hash_check=True,
            hash={"expected_state_hash": None},  # Will be computed
        )

        final_state = {"agent": {"db": {"users": [1, 2, 3]}}, "user": {}}

        # Without expected hash, should pass (no check configured)
        result = evaluator.evaluate_state_checks(final_state, config)

        assert result.score == 1.0  # No expected hash to check against

    def test_no_checks_configured(self):
        """Test when no checks are configured"""
        evaluator = EnvironmentEvaluator()

        config = StateChecksConfig(env_assertions=[], db_hash_check=False)

        final_state = {"agent": {}, "user": {}}

        result = evaluator.evaluate_state_checks(final_state, config)

        assert result.score == 1.0


@pytest.mark.unit
class TestIntegration:
    """Integration tests for combined evaluation - action and communication only"""

    def test_action_and_communication_pass(self):
        """Test evaluation with action and communication passing"""
        action_eval = ActionEvaluator()
        comm_eval = CommunicateEvaluator()

        # Required action
        required_action = RequiredAction(
            action_id="1", requestor="assistant", name="refuel_data", arguments={}
        )

        # Communication requirement
        comm_info = CommunicateInfo(info="data refueled", required=True)

        # Create trajectory
        tool_call = ToolCall(id="call_1", name="refuel_data", arguments={"amount_gb": 2.0})
        messages = [
            Message(
                role=MessageRole.ASSISTANT,
                content="I've refueled your data plan.",
                tool_calls=[tool_call],
            ),
        ]

        # Evaluate
        action_result = action_eval.evaluate_actions(messages, [required_action])
        comm_result = comm_eval.evaluate_communication(messages, [comm_info])

        assert action_result.score == 1.0
        assert comm_result.score == 1.0

        # Combined score (weighted average or product)
        combined_score = action_result.score * comm_result.score
        assert combined_score == 1.0

    def test_action_passes_communication_fails(self):
        """Test evaluation with action passing but communication failing"""
        action_eval = ActionEvaluator()
        comm_eval = CommunicateEvaluator()

        required_action = RequiredAction(
            action_id="1", requestor="assistant", name="refuel_data", arguments={}
        )
        comm_info = CommunicateInfo(info="data refueled", required=True)

        # Trajectory: action taken but not communicated properly
        tool_call = ToolCall(id="call_1", name="refuel_data", arguments={})
        messages = [
            Message(
                role=MessageRole.ASSISTANT,
                content="I've updated your account.",  # Doesn't say "refueled"
                tool_calls=[tool_call],
            ),
        ]

        # Evaluate
        action_result = action_eval.evaluate_actions(messages, [required_action])
        comm_result = comm_eval.evaluate_communication(messages, [comm_info])

        assert action_result.score == 1.0  # Action was taken
        assert comm_result.score == 0.0  # Not communicated

        # Combined score shows failure
        combined_score = action_result.score * comm_result.score
        assert combined_score == 0.0
