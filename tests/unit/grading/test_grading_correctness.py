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
from tolokaforge.runner.models import RequiredAction, TranscriptRulesConfig


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

        assert (
            score == 0.0
        ), f"Score should be 0.0 when configured grading has no evaluated components, got {score}"
        assert (
            binary_pass is False
        ), "binary_pass should be False when grading was configured but nothing evaluated"

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
    """Real-behaviour tests for the author-facing TranscriptRulesConfig grader.

    ``evaluate_transcript_rules`` takes a single
    ``TranscriptRulesConfig.model_dump()`` dict (the schema authors write in
    grading.yaml) and decomposes its fields into per-field sub-checks. These
    tests exercise each field honestly with realistic fixtures and prove the
    historical always-pass no-op bug is gone.
    """

    @staticmethod
    def _config(**fields):
        """Build a TranscriptRulesConfig dump with only the given fields set."""
        return TranscriptRulesConfig(**fields).model_dump()

    # --- empty config (no-op pass) -----------------------------------------

    def test_empty_config_is_noop_pass(self):
        result = evaluate_transcript_rules([], [], self._config())
        assert result.passed is True
        assert result.score == 1.0
        assert result.details == []

    # --- must_contain ------------------------------------------------------

    def test_must_contain_present(self):
        messages = [
            {"role": "user", "content": "Help me with my order"},
            {"role": "assistant", "content": "I'll help you with your order."},
        ]
        result = evaluate_transcript_rules(messages, [], self._config(must_contain=["help you"]))
        assert result.passed is True
        assert result.score == 1.0

    def test_must_contain_absent_fails(self):
        messages = [
            {"role": "user", "content": "Help me with my order"},
            {"role": "assistant", "content": "I cannot assist with that."},
        ]
        result = evaluate_transcript_rules(messages, [], self._config(must_contain=["help you"]))
        assert result.passed is False
        assert result.score == 0.0

    def test_must_contain_partial_credit(self):
        """One of two required strings present → score 0.5, not passed."""
        messages = [{"role": "assistant", "content": "Your order is confirmed."}]
        result = evaluate_transcript_rules(
            messages, [], self._config(must_contain=["confirmed", "refunded"])
        )
        assert result.passed is False
        assert result.score == 0.5

    # --- disallow_regex ----------------------------------------------------

    def test_disallow_regex_no_match_passes(self):
        messages = [{"role": "assistant", "content": "I'll help you with your request."}]
        result = evaluate_transcript_rules(
            messages, [], self._config(disallow_regex=[r"cannot\s+help"])
        )
        assert result.passed is True
        assert result.score == 1.0

    def test_disallow_regex_match_fails(self):
        messages = [{"role": "assistant", "content": "Sorry, I cannot   help with that."}]
        result = evaluate_transcript_rules(
            messages, [], self._config(disallow_regex=[r"cannot\s+help"])
        )
        assert result.passed is False
        assert result.score == 0.0

    # --- max_turns ---------------------------------------------------------

    def test_max_turns_under_limit_passes(self):
        messages = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
        ]
        result = evaluate_transcript_rules(messages, [], self._config(max_turns=5))
        assert result.passed is True
        assert result.score == 1.0

    def test_max_turns_over_limit_fails(self):
        messages = [{"role": "assistant", "content": f"A{i}"} for i in range(10)]
        result = evaluate_transcript_rules(messages, [], self._config(max_turns=5))
        assert result.passed is False
        assert result.score == 0.0

    # --- required_actions --------------------------------------------------

    def test_required_action_present(self):
        tool_history = [
            {
                "tool_name": "get_order",
                "arguments": {"order_id": "123"},
                "executor": "agent",
                "status": "success",
            },
        ]
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    tool_name="get_order",
                    arguments={},
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules([], tool_history, config)
        assert result.passed is True
        assert result.score == 1.0

    def test_required_action_absent_fails(self):
        tool_history = [
            {
                "tool_name": "list_orders",
                "arguments": {},
                "executor": "agent",
                "status": "success",
            },
        ]
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    tool_name="get_order",
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules([], tool_history, config)
        assert result.passed is False
        assert result.score == 0.0

    def test_required_action_compare_args_right_args(self):
        """compare_args subset matches → pass."""
        tool_history = [
            {
                "tool_name": "book_reservation",
                "arguments": {"user_id": "mia_li_3668", "seat": "12A"},
                "executor": "agent",
                "status": "success",
            },
        ]
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="book",
                    requestor="assistant",
                    tool_name="book_reservation",
                    arguments={"user_id": "mia_li_3668"},
                    compare_args=["user_id"],
                )
            ]
        )
        result = evaluate_transcript_rules([], tool_history, config)
        assert result.passed is True
        assert result.score == 1.0

    def test_required_action_compare_args_wrong_args_fails(self):
        """compare_args subset mismatches → fail (the arg value differs)."""
        tool_history = [
            {
                "tool_name": "book_reservation",
                "arguments": {"user_id": "someone_else", "seat": "12A"},
                "executor": "agent",
                "status": "success",
            },
        ]
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="book",
                    requestor="assistant",
                    tool_name="book_reservation",
                    arguments={"user_id": "mia_li_3668"},
                    compare_args=["user_id"],
                )
            ]
        )
        result = evaluate_transcript_rules([], tool_history, config)
        assert result.passed is False
        assert result.score == 0.0

    def test_required_action_requestor_mismatch_fails(self):
        """A call made by the user does not satisfy an assistant-requestor action."""
        tool_history = [
            {
                "tool_name": "get_order",
                "arguments": {},
                "executor": "user",
                "status": "success",
            },
        ]
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    tool_name="get_order",
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules([], tool_history, config)
        assert result.passed is False

    def test_required_action_failed_call_does_not_count(self):
        """A non-success status does not satisfy a required action."""
        tool_history = [
            {
                "tool_name": "get_order",
                "arguments": {},
                "executor": "agent",
                "status": "error",
            },
        ]
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    tool_name="get_order",
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules([], tool_history, config)
        assert result.passed is False

    # --- communicate_info --------------------------------------------------

    def test_communicate_info_required_present(self):
        messages = [
            {"role": "assistant", "content": "Your Wi-Fi password is aurora-481-fennel."},
        ]
        config = self._config(communicate_info=[{"info": "aurora-481-fennel", "required": True}])
        result = evaluate_transcript_rules(messages, [], config)
        assert result.passed is True
        assert result.score == 1.0

    def test_communicate_info_required_absent_fails(self):
        messages = [{"role": "assistant", "content": "Here is some unrelated text."}]
        config = self._config(communicate_info=[{"info": "aurora-481-fennel", "required": True}])
        result = evaluate_transcript_rules(messages, [], config)
        assert result.passed is False
        assert result.score == 0.0

    def test_communicate_info_not_required_is_not_scored(self):
        """Non-required info is advisory and produces no sub-check."""
        messages = [{"role": "assistant", "content": "Nothing relevant here."}]
        config = self._config(communicate_info=[{"info": "aurora-481-fennel", "required": False}])
        result = evaluate_transcript_rules(messages, [], config)
        # No sub-checks at all → no-op pass.
        assert result.details == []
        assert result.passed is True

    # --- regression: the old always-pass no-op bug -------------------------

    def test_violating_config_no_longer_silently_passes(self):
        """REGRESSION: previously the full TranscriptRulesConfig dict was passed
        as a single rule with no ``type`` key, hitting the unknown-type branch
        and ALWAYS returning passed=True / score=1.0. A config the transcript
        clearly violates must now fail.
        """
        messages = [{"role": "assistant", "content": "I did nothing useful."}]
        tool_history = []  # the required action never happened
        config = self._config(
            must_contain=["confirmation number"],
            required_actions=[
                RequiredAction(
                    action_id="book",
                    requestor="assistant",
                    tool_name="book_reservation",
                    compare_args=[],
                )
            ],
            communicate_info=[{"info": "your refund", "required": True}],
        )
        result = evaluate_transcript_rules(messages, tool_history, config)
        assert result.passed is False
        assert result.score < 1.0
        # Every sub-check should be present and failing.
        assert len(result.details) == 3
        assert all(d.passed is False for d in result.details)

    def test_combined_fields_mixed_pass_fail(self):
        """Multiple fields together: score is the fraction of sub-checks passed."""
        messages = [
            {"role": "user", "content": "Cancel my booking"},
            {"role": "assistant", "content": "Done — your booking is cancelled."},
        ]
        tool_history = [
            {
                "tool_name": "cancel_booking",
                "arguments": {"booking_id": "B1"},
                "executor": "agent",
                "status": "success",
            },
        ]
        config = self._config(
            must_contain=["cancelled"],  # pass
            max_turns=5,  # pass (1 assistant turn)
            required_actions=[
                RequiredAction(
                    action_id="cancel",
                    requestor="assistant",
                    tool_name="cancel_booking",
                    compare_args=[],
                )
            ],  # pass
            communicate_info=[{"info": "refund issued", "required": True}],  # fail
        )
        result = evaluate_transcript_rules(messages, tool_history, config)
        assert result.passed is False
        assert result.score == 0.75  # 3 of 4 sub-checks pass


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


class TestNumericCanonicalization:
    """Numerically-equal state values must not be graded as a state change.

    Regression: the DB round-trips ``Decimal`` columns through strings, so the
    same amount surfaces as ``"130.00"`` on one side and ``"130.0"`` (or an
    ``int``) on the other. The naive hash / diff treated that pure formatting
    difference as a mismatch and false-failed correct trials (observed on the
    OTS ``custom_refund_amount`` / payroll ``amount`` fields).
    """

    def test_numeric_types_hash_equal_by_default(self):
        """to_hashable/consistent_hash collapse numeric TYPES (int/float/Decimal)."""
        from decimal import Decimal

        assert consistent_hash(to_hashable(72)) == consistent_hash(to_hashable(72.0))
        assert consistent_hash(to_hashable(72)) == consistent_hash(to_hashable(Decimal("72.00")))

    def test_numeric_strings_do_not_fold_by_default(self):
        """Numeric-looking STRINGS stay distinct without the per-task flag."""
        assert consistent_hash(to_hashable("130.00")) != consistent_hash(to_hashable("130.0"))
        assert consistent_hash(to_hashable("72.00")) != consistent_hash(to_hashable(72))

    def test_numeric_strings_fold_with_flag(self):
        """canonical_number(normalize_strings=True) folds decimal string formats."""
        from tolokaforge.core.hash import canonical_number

        assert canonical_number("130.00", normalize_strings=True) == canonical_number(
            "130.0", normalize_strings=True
        )
        assert canonical_number("72.00", normalize_strings=True) == canonical_number(72)
        assert canonical_number("790.00", normalize_strings=True) != canonical_number(
            "0.0", normalize_strings=True
        )
        assert canonical_number("00123", normalize_strings=True) != canonical_number(
            "123", normalize_strings=True
        )

    def test_bool_not_collapsed_to_int(self):
        """bool stays distinct from its int twin (True == 1 in Python)."""
        assert consistent_hash(to_hashable(True)) != consistent_hash(to_hashable(1))

    def test_compute_state_diff_reports_string_format_difference_by_default(self):
        """Without the flag, a string-format-only diff is still reported (and the
        per-task hash flag, not this report, decides the verdict)."""
        golden = {"d365_api_cases": [{"case_id": "CAS-1", "custom_refund_amount": "130.00"}]}
        trial = {"d365_api_cases": [{"case_id": "CAS-1", "custom_refund_amount": "130.0"}]}
        diff = compute_state_diff(trial, golden)
        assert diff.summary != "States match"

    def test_compute_state_diff_ignores_numeric_type_format(self):
        """Numeric-TYPE representation (72 vs 72.0) is not reported as a diff."""
        golden = {"t": [{"id": "R1", "qty": 72}]}
        trial = {"t": [{"id": "R1", "qty": 72.0}]}
        diff = compute_state_diff(trial, golden)
        assert diff.summary == "States match", f"expected match, got: {diff.summary}"

    def test_compute_state_diff_flags_genuine_amount_difference(self):
        """A real refund difference is still reported as a mismatch."""
        golden = {"d365_api_cases": [{"case_id": "CAS-1", "custom_refund_amount": "790.00"}]}
        trial = {"d365_api_cases": [{"case_id": "CAS-1", "custom_refund_amount": "0.0"}]}
        diff = compute_state_diff(trial, golden)
        assert diff.summary != "States match"
