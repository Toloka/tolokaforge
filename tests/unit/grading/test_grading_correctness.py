"""
Grading Correctness Verification Tests

These tests verify that the grading system works correctly:
- Binary reward (score=1.0 for hash match, score=0.0 for mismatch)
- Error detection (technical errors vs task failures)
- LLM fallback status (placeholder verification)

PROJECT RULES: Tests use real behavior, no mocks.
"""

from datetime import datetime

import pytest

pytestmark = pytest.mark.unit

from tolokaforge.core.grading.state_checks import StateChecker, consistent_hash, to_hashable
from tolokaforge.core.hash import compute_stable_hash
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    Metrics,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.runner.grading import (
    build_grade_reasons,
    combine_grade_components,
    compute_state_diff,
    evaluate_transcript_rules,
)


class TestGoldenMatchScoresOne:
    """Test that golden set hash match produces score=1.0"""

    def test_hash_match_scores_one(self):
        """
        Verify that when actual state hash matches expected hash,
        the grading system returns score=1.0 and binary_pass=True.

        This mirrors the code path in the TlkMcpCore adapter grade() method:
        - compute_stable_hash(expected_stable) -> expected_hash
        - compute_stable_hash(actual_stable) -> actual_hash
        - if actual_hash == expected_hash: score=1.0, binary_pass=True
        """
        # Simulate a golden state (what the golden actions produce)
        golden_state = {
            "zendesk_tickets": [
                {"id": "T001", "status": "resolved", "subject": "Help needed"},
                {"id": "T002", "status": "open", "subject": "Question"},
            ],
            "workday_employees": [
                {"id": "E001", "name": "Alice", "department": "Engineering"},
            ],
        }

        # Compute expected hash using tau-bench compatible algorithm (what StateChecker uses)
        expected_hash = consistent_hash(to_hashable(golden_state))

        # Simulate agent producing the exact same state
        actual_state = {
            "zendesk_tickets": [
                {"id": "T001", "status": "resolved", "subject": "Help needed"},
                {"id": "T002", "status": "open", "subject": "Question"},
            ],
            "workday_employees": [
                {"id": "E001", "name": "Alice", "department": "Engineering"},
            ],
        }

        actual_hash = consistent_hash(to_hashable(actual_state))

        # Verify hashes match
        assert actual_hash == expected_hash, "Hashes should match for identical states"

        # Verify grading logic produces score=1.0
        checker = StateChecker()
        score, reason = checker.check_hash(actual_state, expected_hash)

        assert score == 1.0, f"Score should be 1.0 for hash match, got {score}"
        assert "match" in reason.lower(), f"Reason should mention 'match': {reason}"

    def test_hash_match_with_different_key_order(self):
        """
        Verify that dict key order doesn't affect hash comparison.
        This is critical for deterministic grading.
        """
        # State with keys in one order
        state1 = {"b": 2, "a": 1, "c": {"y": 2, "x": 1}}

        # Same state with keys in different order
        state2 = {"a": 1, "c": {"x": 1, "y": 2}, "b": 2}

        hash1 = compute_stable_hash(state1)
        hash2 = compute_stable_hash(state2)

        assert hash1 == hash2, "Hash should be deterministic regardless of key order"

    def test_grade_components_hash_match(self):
        """
        Test combine_grade_components with hash match.
        """
        components = {
            "hash_match": True,
            "hash_score": 1.0,
            "transcript_pass": True,
            "transcript_score": -1.0,  # Not evaluated
        }

        grading_config = {
            "combine_method": "all",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 1.0,
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == 1.0, f"Score should be 1.0, got {score}"
        assert binary_pass is True, "binary_pass should be True"


class TestGoldenMismatchScoresZero:
    """Test that golden set hash mismatch produces score=0.0"""

    def test_hash_mismatch_scores_zero(self):
        """
        Verify that when actual state hash differs from expected hash,
        the grading system returns score=0.0 and binary_pass=False.

        This mirrors the code path in the TlkMcpCore adapter grade() method:
        - if actual_hash != expected_hash: score=0.0, binary_pass=False
        """
        # Expected state (from golden actions)
        expected_state = {
            "work_orders": [
                {"id": "WO-001", "status": "Scheduled", "technician_id": "TECH-001"},
            ],
        }
        expected_hash = compute_stable_hash(expected_state)

        # Actual state (agent did something different)
        actual_state = {
            "work_orders": [
                {"id": "WO-001", "status": "Unassigned", "technician_id": None},
            ],
        }
        actual_hash = compute_stable_hash(actual_state)

        # Verify hashes don't match
        assert actual_hash != expected_hash, "Hashes should differ for different states"

        # Verify grading logic produces score=0.0
        checker = StateChecker()
        score, reason = checker.check_hash(actual_state, expected_hash)

        assert score == 0.0, f"Score should be 0.0 for hash mismatch, got {score}"
        assert "mismatch" in reason.lower(), f"Reason should mention 'mismatch': {reason}"

    def test_state_diff_computed_on_mismatch(self):
        """
        Verify that state diff is computed when hashes don't match.
        This helps debug why the agent failed.
        """
        expected_state = {
            "appointments": [
                {"id": "APPT-001", "status": "Scheduled", "technician_id": "TECH-001"},
            ],
        }

        actual_state = {
            "appointments": [
                {"id": "APPT-001", "status": "Cancelled", "technician_id": None},
            ],
        }

        # Compute diff
        diff = compute_state_diff(actual_state, expected_state)

        assert diff.summary != "States match", "Diff should indicate mismatch"
        assert (
            "appointments" in diff.tables
            or len(diff.tables) > 0
            or "mismatch" in diff.summary.lower()
        )

    def test_grade_components_hash_mismatch(self):
        """
        Test combine_grade_components with hash mismatch.
        """
        components = {
            "hash_match": False,
            "hash_score": 0.0,
            "transcript_pass": True,
            "transcript_score": -1.0,  # Not evaluated
        }

        grading_config = {
            "combine_method": "all",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 1.0,
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == 0.0, f"Score should be 0.0, got {score}"
        assert binary_pass is False, "binary_pass should be False"

    def test_build_grade_reasons_mismatch(self):
        """
        Test that grade reasons clearly indicate hash mismatch.
        """
        components = {
            "hash_match": False,
            "hash_score": 0.0,
        }

        state_diff = {"summary": "State mismatch: appointments: 1 missing, 0 extra, 1 different"}

        reasons = build_grade_reasons(components, state_diff=state_diff)

        assert "mismatch" in reasons.lower() or "State:" in reasons


class TestErrorTrialDetected:
    """Test that technical errors are properly detected and marked"""

    def test_error_status_auto_fails(self):
        """
        Verify that trials with status=ERROR automatically fail grading.

        This mirrors the code path in orchestrator.py _run_trial():
        - if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
        -     grade = Grade(binary_pass=False, score=0.0, ...)
        """
        # Create a trajectory that represents an error (e.g., API failure)
        now = datetime.now()
        trajectory = Trajectory(
            task_id="TEST-001",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.ERROR,
            termination_reason=TerminationReason.API_ERROR,
            messages=[
                Message(
                    role="system",
                    content="Trial initialization error: LLM API call failed: APIError",
                )
            ],
            metrics=Metrics(),
        )

        # Verify the trajectory is in error state
        assert trajectory.status == TrialStatus.ERROR
        assert trajectory.termination_reason == TerminationReason.API_ERROR

        # Simulate the orchestrator's error handling logic
        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            grade = Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )
        else:
            grade = None

        assert grade is not None
        assert grade.binary_pass is False
        assert grade.score == 0.0
        assert "error" in grade.reasons.lower()

    def test_timeout_status_auto_fails(self):
        """
        Verify that trials with status=TIMEOUT automatically fail grading.
        """
        now = datetime.now()
        trajectory = Trajectory(
            task_id="TEST-002",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.TIMEOUT,
            termination_reason=TerminationReason.TIMEOUT,
            messages=[],
            metrics=Metrics(),
        )

        assert trajectory.status == TrialStatus.TIMEOUT

        # Simulate the orchestrator's error handling logic
        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            grade = Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )
        else:
            grade = None

        assert grade is not None
        assert grade.binary_pass is False
        assert grade.score == 0.0
        assert "timeout" in grade.reasons.lower()

    def test_rate_limit_is_retryable(self):
        """
        Verify that rate limit errors are classified as retryable.

        This mirrors orchestrator._is_retryable_trajectory().
        """
        now = datetime.now()
        trajectory = Trajectory(
            task_id="TEST-003",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.ERROR,
            termination_reason=TerminationReason.RATE_LIMIT,
            messages=[],
            metrics=Metrics(),
        )

        # Check retryable classification
        is_retryable = trajectory.status in (
            TrialStatus.ERROR,
            TrialStatus.TIMEOUT,
        ) or trajectory.termination_reason in (
            TerminationReason.RATE_LIMIT,
            TerminationReason.API_ERROR,
            TerminationReason.TIMEOUT,
            TerminationReason.ERROR,
        )

        assert is_retryable is True, "Rate limit errors should be retryable"

    def test_completed_trial_not_retryable(self):
        """
        Verify that completed trials (even with score=0) are not retryable.
        """
        now = datetime.now()
        trajectory = Trajectory(
            task_id="TEST-004",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.AGENT_DONE,  # AGENT_DONE is the correct enum value
            messages=[],
            metrics=Metrics(),
        )

        # Check retryable classification
        is_retryable = trajectory.status in (
            TrialStatus.ERROR,
            TrialStatus.TIMEOUT,
        ) or trajectory.termination_reason in (
            TerminationReason.RATE_LIMIT,
            TerminationReason.API_ERROR,
            TerminationReason.TIMEOUT,
            TerminationReason.ERROR,
        )

        assert is_retryable is False, "Completed trials should not be retryable"


class TestLLMJudgePlaceholderStatus:
    """
    Test that LLM judge fields exist but are placeholders.

    Note: LLM judge was explicitly deferred during Docker architecture work.
    These tests confirm the placeholder status and document the interface.
    """

    def test_grade_components_has_llm_judge_field(self):
        """
        Verify GradeComponents has llm_judge field (currently null/None).
        """
        components = GradeComponents(
            state_checks=1.0,
            transcript_rules=None,
            llm_judge=None,
            custom_checks=None,
        )

        assert hasattr(components, "llm_judge")
        assert components.llm_judge is None, "LLM judge should be None (placeholder)"

    def test_grade_has_llm_judge_in_components(self):
        """
        Verify Grade model supports llm_judge in components.
        """
        grade = Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(
                state_checks=1.0,
                transcript_rules=None,
                llm_judge=None,
                custom_checks=None,
            ),
            reasons="State: hash match",
        )

        assert grade.components is not None
        assert grade.components.llm_judge is None

    def test_combine_grade_components_ignores_negative_llm_judge(self):
        """
        Verify that combine_grade_components ignores llm_judge when score is -1.0.

        In the current implementation, -1.0 means "not evaluated".
        """
        components = {
            "hash_match": True,
            "hash_score": 1.0,
            "transcript_pass": True,
            "transcript_score": -1.0,  # Not evaluated
        }

        grading_config = {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0, "llm_judge": 0.5},
            "pass_threshold": 0.8,
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        # Should only consider state_checks since llm_judge is not in components
        assert score == 1.0
        assert binary_pass is True

    def test_combine_grade_components_fails_when_configured_but_unevaluated(self):
        """
        Verify combine_grade_components fails when grading is configured
        (weights include state_checks) but no components were actually evaluated.

        This catches the refusal-task bug: golden_actions=[] caused hash grading
        to be skipped, leaving hash_score=-1.0. Previously this silently returned
        (1.0, True) — a false pass. Now it must return (0.0, False).
        """
        components = {
            "hash_match": None,
            "hash_score": -1.0,  # Not evaluated
            "transcript_pass": None,
            "transcript_score": -1.0,  # Not evaluated
        }

        grading_config = {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 1.0,
            "state_checks": {"hash_enabled": True, "golden_actions": []},
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == 0.0, (
            f"Score should be 0.0 when configured grading has no evaluated components, got {score}"
        )
        assert binary_pass is False, (
            "binary_pass should be False when grading was configured but nothing evaluated"
        )

    def test_combine_grade_components_passes_when_nothing_configured(self):
        """
        Verify combine_grade_components passes when no grading is configured at all.

        When weights are empty (no grading components requested), the system
        should pass by default — this is the "no grading" case, not the
        "grading skipped" case.
        """
        components = {
            "hash_match": None,
            "hash_score": -1.0,
            "transcript_pass": None,
            "transcript_score": -1.0,
        }

        grading_config = {
            "combine_method": "weighted",
            "weights": {},  # No grading configured
            "pass_threshold": 1.0,
        }

        score, binary_pass = combine_grade_components(components, grading_config)

        assert score == 1.0, f"Score should be 1.0 when no grading configured, got {score}"
        assert binary_pass is True, "binary_pass should be True when no grading configured"


class TestTranscriptRulesEvaluation:
    """Test transcript rules evaluation (implemented but often not used)"""

    def test_must_contain_rule_pass(self):
        """Test must_contain rule that passes."""
        messages = [
            {"role": "user", "content": "Help me with my order"},
            {
                "role": "assistant",
                "content": "I'll help you with your order. Let me check the status.",
            },
        ]

        rules = [{"type": "must_contain", "text": "help you", "case_sensitive": False}]

        result = evaluate_transcript_rules(messages, [], rules)

        assert result.passed is True
        assert result.score == 1.0

    def test_must_contain_rule_fail(self):
        """Test must_contain rule that fails."""
        messages = [
            {"role": "user", "content": "Help me with my order"},
            {"role": "assistant", "content": "I cannot assist with that."},
        ]

        rules = [{"type": "must_contain", "text": "help you", "case_sensitive": False}]

        result = evaluate_transcript_rules(messages, [], rules)

        assert result.passed is False
        assert result.score == 0.0

    def test_must_not_contain_rule_pass(self):
        """Test must_not_contain rule that passes."""
        messages = [
            {"role": "assistant", "content": "I'll help you with your request."},
        ]

        rules = [{"type": "must_not_contain", "text": "cannot help", "case_sensitive": False}]

        result = evaluate_transcript_rules(messages, [], rules)

        assert result.passed is True
        assert result.score == 1.0

    def test_required_tool_call_rule(self):
        """Test required_tool_call rule."""
        messages = []
        tool_history = [
            {"tool_name": "get_order", "arguments": {"order_id": "123"}, "status": "success"},
        ]

        rules = [{"type": "required_tool_call", "tool_name": "get_order", "min_calls": 1}]

        result = evaluate_transcript_rules(messages, tool_history, rules)

        assert result.passed is True
        assert result.score == 1.0

    def test_max_turns_rule_pass(self):
        """Test max_turns rule that passes."""
        messages = [
            {"role": "user", "content": "Question 1"},
            {"role": "assistant", "content": "Answer 1"},
            {"role": "user", "content": "Question 2"},
            {"role": "assistant", "content": "Answer 2"},
        ]

        rules = [{"type": "max_turns", "max": 5, "count_method": "user_messages"}]

        result = evaluate_transcript_rules(messages, [], rules)

        assert result.passed is True
        assert result.score == 1.0

    def test_max_turns_rule_fail(self):
        """Test max_turns rule that fails."""
        messages = [{"role": "user", "content": f"Question {i}"} for i in range(10)]

        rules = [{"type": "max_turns", "max": 5, "count_method": "user_messages"}]

        result = evaluate_transcript_rules(messages, [], rules)

        assert result.passed is False
        assert result.score == 0.0


class TestStableHashComputation:
    """Test stable hash computation algorithm"""

    def test_stable_hash_deterministic(self):
        """Verify hash is deterministic across multiple calls."""
        state = {
            "users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}],
            "settings": {"theme": "dark", "notifications": True},
        }

        hash1 = compute_stable_hash(state)
        hash2 = compute_stable_hash(state)
        hash3 = compute_stable_hash(state)

        assert hash1 == hash2 == hash3

    def test_stable_hash_sha256_format(self):
        """Verify hash is SHA256 hex digest (64 characters)."""
        state = {"test": "data"}
        hash_result = compute_stable_hash(state)

        assert len(hash_result) == 64
        assert all(c in "0123456789abcdef" for c in hash_result)

    def test_stable_hash_with_unstable_fields_filtered(self):
        """Verify unstable fields are filtered before hashing."""
        state_with_timestamp = {
            "order": {"id": "O001", "status": "confirmed"},
            "created_at": "2024-01-01T00:00:00Z",
        }

        state_without_timestamp = {
            "order": {"id": "O001", "status": "confirmed"},
        }

        # With unstable fields filtered, hashes should match
        hash1 = compute_stable_hash(state_with_timestamp, unstable_fields=["created_at"])
        hash2 = compute_stable_hash(state_without_timestamp)

        assert hash1 == hash2

    def test_consistent_hash_matches_to_hashable(self):
        """Verify consistent_hash and to_hashable work together."""
        state = {"a": 1, "b": [2, 3], "c": {"x": 4}}

        # Using the tau-bench compatible functions
        hashable = to_hashable(state)
        hash1 = consistent_hash(hashable)

        # Should be deterministic
        hash2 = consistent_hash(to_hashable(state))

        assert hash1 == hash2
        assert len(hash1) == 64
