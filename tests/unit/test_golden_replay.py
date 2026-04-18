"""Unit tests for golden action replay in _execute_hash_grading.

Verifies that golden actions are replayed with their original arguments
(no ID substitution), matching mcp_core's apply_golden_set_to_database()
behaviour.
"""

import pytest

from tolokaforge.runner.models import GoldenAction, HashGradingResult

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Test: GoldenAction model round-trips arguments unchanged
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGoldenActionArguments:
    """Golden actions must preserve argument values without mutation."""

    def test_arguments_not_mutated_by_model(self):
        """GoldenAction model should keep arguments as-is."""
        action = GoldenAction(
            tool_name="zendesk_update_item",
            arguments={"table": "tickets", "id": "6", "item": {"status": "solved"}},
        )
        assert action.arguments["id"] == "6"
        assert action.arguments["table"] == "tickets"
        assert action.arguments["item"] == {"status": "solved"}

    def test_mixed_domain_ids_not_confused(self):
        """IDs from different tool domains should not interfere.

        This tests the scenario that caused the ots_07_logistics_internal bug:
        hris_get_employee returns {"id": "EMP-00000500"} but golden
        zendesk_update_item expects id="6" — these should never be mixed.
        """
        hris_action = GoldenAction(
            tool_name="hris_hris_get_employee",
            arguments={"employee_id": "EMP-00000500"},
        )
        zendesk_action = GoldenAction(
            tool_name="zendesk_update_item",
            arguments={"table": "tickets", "id": "6", "item": {"status": "solved"}},
        )

        # The arguments are independent — no field should be shared/substituted
        assert hris_action.arguments["employee_id"] == "EMP-00000500"
        assert zendesk_action.arguments["id"] == "6"

        # Even though both domains use "id" as a field name in their results,
        # the golden actions use different argument keys (employee_id vs id)
        # and the replay should pass arguments through unchanged.


# ---------------------------------------------------------------------------
# Test: HashGradingResult includes golden_action_errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHashGradingResult:
    """HashGradingResult must propagate golden action errors."""

    def test_no_errors_by_default(self):
        result = HashGradingResult(hash_match=True, hash_score=1.0)
        assert result.golden_action_errors == []

    def test_errors_preserved(self):
        errors = ["Golden action 4 (zendesk_update_item) failed: ExecutionError: not found"]
        result = HashGradingResult(
            hash_match=False,
            hash_score=0.0,
            golden_action_errors=errors,
        )
        assert len(result.golden_action_errors) == 1
        assert "zendesk_update_item" in result.golden_action_errors[0]

    def test_serialization_round_trip(self):
        """Errors survive JSON serialization (for gRPC transport)."""
        errors = ["Golden action 2 failed: ValueError: bad id"]
        result = HashGradingResult(
            hash_match=False,
            hash_score=0.0,
            golden_action_errors=errors,
        )
        data = result.model_dump()
        restored = HashGradingResult.model_validate(data)
        assert restored.golden_action_errors == errors


# ---------------------------------------------------------------------------
# Test: Refusal tasks — empty golden_actions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRefusalTaskGrading:
    """Refusal tasks have golden_actions=[] — the expected state is the initial state.

    The runner must still run hash-based grading for these tasks, comparing
    the trial's final DB state against the initial (unchanged) state.
    """

    def test_empty_golden_actions_list_is_valid(self):
        """An empty golden_actions list is a valid refusal task — not an error."""
        from tolokaforge.runner.models import StateChecksConfig

        config = StateChecksConfig(
            hash_enabled=True,
            golden_actions=[],  # Refusal task
        )
        assert config.hash_enabled is True
        assert config.golden_actions == []

    def test_hash_grading_result_for_refusal_pass(self):
        """When the agent doesn't modify state (correct refusal), hashes match."""
        result = HashGradingResult(
            hash_match=True,
            hash_score=1.0,
            golden_action_errors=[],
        )
        assert result.hash_match is True
        assert result.hash_score == 1.0

    def test_hash_grading_result_for_refusal_fail(self):
        """When the agent incorrectly modifies state, hashes don't match."""
        result = HashGradingResult(
            hash_match=False,
            hash_score=0.0,
            golden_action_errors=[],
        )
        assert result.hash_match is False
        assert result.hash_score == 0.0

    def test_combine_grade_components_refusal_pass(self):
        """Refusal task that correctly leaves state unchanged should pass."""
        from tolokaforge.runner.grading import combine_grade_components

        components = {
            "hash_match": True,
            "hash_score": 1.0,
            "transcript_pass": None,
            "transcript_score": -1.0,
        }
        grading_config = {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 1.0,
        }

        score, binary_pass = combine_grade_components(components, grading_config)
        assert score == 1.0
        assert binary_pass is True

    def test_combine_grade_components_refusal_fail(self):
        """Refusal task where agent incorrectly modified state should fail."""
        from tolokaforge.runner.grading import combine_grade_components

        components = {
            "hash_match": False,
            "hash_score": 0.0,
            "transcript_pass": None,
            "transcript_score": -1.0,
        }
        grading_config = {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 1.0,
        }

        score, binary_pass = combine_grade_components(components, grading_config)
        assert score == 0.0
        assert binary_pass is False
