"""
Grading Correctness Verification Tests

These tests verify that the grading system works correctly:
- Binary reward (score=1.0 for hash match, score=0.0 for mismatch)
- Error detection (technical errors vs task failures)
- LLM fallback status (placeholder verification)

PROJECT RULES: Tests use real behavior, no mocks.
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.unit

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import build_timeline
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.state_checks import StateChecker, consistent_hash, to_hashable
from tolokaforge.core.grading.trace_timeline import TrialTimeline, build_trial_timeline
from tolokaforge.core.grading.transcript import evaluate_transcript_rules
from tolokaforge.core.hash import compute_stable_hash
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    GradingConfig,
    Message,
    MessageRole,
    Metrics,
    RecordedToolCall,
    TerminationReason,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
    TrialStatus,
)
from tolokaforge.runner.grading import (
    build_grade_reasons,
    combine_grade_components,
    compute_state_diff,
)
from tolokaforge.runner.models import (
    RequiredAction,
    ToolExpectations,
    TranscriptRulesConfig,
)

# One written-out ``grading.yaml`` section per component, keyed by the section
# name the registry declares. Indexed rather than iterated, so a component added
# to the registry without a section here fails on the missing key instead of
# going untested.
_MINIMAL_CONFIG_SECTIONS: dict[str, dict] = {
    "state_checks": {"hash_enabled": True, "golden_actions": []},
    "transcript_rules": {"required_actions": []},
    "trace_checks": {"constraints": []},
    "llm_judge": {"rubric": {"criteria": []}},
    "custom_checks": {"file": "checks.py"},
}


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

        folded = combine_grade_components(components, grading_config)
        score, binary_pass = folded.score, folded.binary_pass

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

        folded = combine_grade_components(components, grading_config)
        score, binary_pass = folded.score, folded.binary_pass

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

    def test_build_grade_reasons_names_a_scored_trace_checks_component(self):
        """A scored component absent from the prose is a verdict an author cannot read."""
        reasons = build_grade_reasons({"trace_checks_score": 0.75})

        assert "Trace checks: score=0.75" in reasons

    def test_build_grade_reasons_omits_an_unevaluated_trace_checks_component(self):
        reasons = build_grade_reasons({"trace_checks_score": -1.0})

        assert "Trace checks" not in reasons

    def test_build_grade_reasons_names_each_failing_trace_constraint(self):
        """The runner emits the per-constraint lines core's engine emits.

        A score alone says how much was lost, never which constraint lost it, so a
        grade produced by the runner would answer less than the same grade produced
        by core — a substrate difference in what the author reads.
        """
        reasons = build_grade_reasons(
            {"trace_checks_score": 0.5},
            trace_checks_result={
                "constraints": [
                    {"id": "lookup_before_denial", "passed": False, "message": "before: no match"},
                    {"id": "no_prefill", "passed": True, "message": ""},
                ]
            },
        )

        assert "Trace check lookup_before_denial: before: no match" in reasons
        assert "no_prefill" not in reasons, "a passing constraint has nothing to report"


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

        folded = combine_grade_components(components, grading_config)
        score, binary_pass = folded.score, folded.binary_pass

        # Should only consider state_checks since llm_judge is not in components
        assert score == 1.0
        assert binary_pass is True

    @pytest.mark.parametrize("spec", GRADE_COMPONENTS, ids=lambda spec: spec.name)
    def test_combine_grade_components_fails_when_configured_but_unevaluated(self, spec):
        """
        Verify combine_grade_components fails when a component is configured
        (weighted, with its config section written) but was never evaluated.

        This catches the refusal-task bug: golden_actions=[] caused hash grading
        to be skipped, leaving hash_score=-1.0, and the trial silently returned
        (1.0, True) — a false pass. Every component must return (0.0, False)
        there; a component missing from the check is a whole task family that
        passes on nothing.
        """
        components = {
            "hash_match": None,
            "hash_score": -1.0,  # Not evaluated
            "transcript_pass": None,
            "transcript_score": -1.0,  # Not evaluated
        }

        grading_config = {
            "combine_method": "weighted",
            "weights": {spec.name: 1.0},
            "pass_threshold": 1.0,
            spec.config_section: _MINIMAL_CONFIG_SECTIONS[spec.config_section],
        }

        folded = combine_grade_components(components, grading_config)
        score, binary_pass = folded.score, folded.binary_pass

        assert score == 0.0, f"{spec.name} configured but unevaluated scored {score}, not 0.0"
        assert binary_pass is False, f"{spec.name} configured with nothing evaluated must not pass"

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

        folded = combine_grade_components(components, grading_config)
        score, binary_pass = folded.score, folded.binary_pass

        assert score == 1.0, f"Score should be 1.0 when no grading configured, got {score}"
        assert binary_pass is True, "binary_pass should be True when no grading configured"


class TestTranscriptRulesEvaluation:
    """Real-behaviour tests for the author-facing TranscriptRulesConfig grader.

    ``evaluate_transcript_rules`` takes the trial's :class:`TrialTimeline` plus the
    validated ``TranscriptRulesConfig`` (the schema authors write in grading.yaml)
    and decomposes the config's fields into per-field sub-checks. These tests
    exercise each field honestly with realistic fixtures and prove the historical
    always-pass no-op bug is gone.
    """

    @staticmethod
    def _config(**fields):
        """Build a TranscriptRulesConfig with only the given fields set."""
        return TranscriptRulesConfig(**fields)

    @staticmethod
    def _timeline(
        turns: Sequence[tuple[str, str]] = (),
        calls: Sequence[RecordedToolCall] = (),
    ) -> TrialTimeline:
        return build_timeline(turns, calls)

    @staticmethod
    def _call(
        tool_name: str,
        *,
        sequence: int = 0,
        status: str = "success",
        arguments: dict | None = None,
        executor: str = "agent",
    ) -> RecordedToolCall:
        return recorded_call(
            tool_name,
            sequence=sequence,
            status=ToolExecutionStatus(status),
            arguments=arguments,
            executor=ToolExecutorIdentity(executor),
        )

    # --- the typed seam ----------------------------------------------------

    def test_a_dumped_config_is_refused(self):
        """The seam both substrates meet at is the model, and only the model.

        A dump reads field-for-field like the model and validates nothing, which is
        how two spellings of the tool field and seven semantic divergences lived
        side by side unnoticed. A caller handing one over is refused rather than
        scored.
        """
        with pytest.raises(TypeError, match="validated TranscriptRulesConfig, not dict"):
            evaluate_transcript_rules(self._timeline(), self._config().model_dump())

    # --- empty config (no-op pass) -----------------------------------------

    def test_empty_config_is_noop_pass(self):
        result = evaluate_transcript_rules(self._timeline(), self._config())
        assert result.passed is True
        assert result.score == 1.0
        assert result.details == []

    # --- must_contain ------------------------------------------------------

    def test_must_contain_present(self):
        timeline = self._timeline(
            [
                ("user", "Help me with my order"),
                ("assistant", "I'll help you with your order."),
            ]
        )
        result = evaluate_transcript_rules(timeline, self._config(must_contain=["help you"]))
        assert result.passed is True
        assert result.score == 1.0

    def test_must_contain_absent_fails(self):
        timeline = self._timeline(
            [
                ("user", "Help me with my order"),
                ("assistant", "I cannot assist with that."),
            ]
        )
        result = evaluate_transcript_rules(timeline, self._config(must_contain=["help you"]))
        assert result.passed is False
        assert result.score == 0.0

    def test_must_contain_only_searches_assistant_turns(self):
        """A phrase the *user* said does not satisfy the rule — the check is on
        what the agent communicated."""
        timeline = self._timeline([("user", "please say confirmed"), ("assistant", "Okay.")])
        result = evaluate_transcript_rules(timeline, self._config(must_contain=["confirmed"]))
        assert result.passed is False
        assert result.score == 0.0

    def test_a_harness_annotation_cannot_satisfy_a_required_phrase(self):
        """``role: system`` turns are harness text, not trial text.

        A termination notice the harness wrote is not a timeline event at all, so
        an author cannot have a phrase rule satisfied by the harness announcing
        the trial ran out of turns.
        """
        timeline = build_trial_timeline(
            [
                Message(role=MessageRole.ASSISTANT, content="Done."),
                Message(role=MessageRole.SYSTEM, content="Trial terminated: max turns reached"),
            ],
            [],
            None,
        )

        result = evaluate_transcript_rules(
            timeline, self._config(must_contain=["max turns reached"])
        )

        assert result.passed is False
        assert result.score == 0.0

    def test_must_contain_partial_credit(self):
        """One of two required strings present → score 0.5, not passed."""
        timeline = self._timeline([("assistant", "Your order is confirmed.")])
        result = evaluate_transcript_rules(
            timeline, self._config(must_contain=["confirmed", "refunded"])
        )
        assert result.passed is False
        assert result.score == 0.5

    # --- disallow_regex ----------------------------------------------------

    def test_disallow_regex_no_match_passes(self):
        timeline = self._timeline([("assistant", "I'll help you with your request.")])
        result = evaluate_transcript_rules(
            timeline, self._config(disallow_regex=[r"cannot\s+help"])
        )
        assert result.passed is True
        assert result.score == 1.0

    def test_disallow_regex_match_fails(self):
        timeline = self._timeline([("assistant", "Sorry, I cannot   help with that.")])
        result = evaluate_transcript_rules(
            timeline, self._config(disallow_regex=[r"cannot\s+help"])
        )
        assert result.passed is False
        assert result.score == 0.0

    # --- max_turns ---------------------------------------------------------

    def test_max_turns_under_limit_passes(self):
        timeline = self._timeline(
            [
                ("user", "Q1"),
                ("assistant", "A1"),
                ("user", "Q2"),
                ("assistant", "A2"),
            ]
        )
        result = evaluate_transcript_rules(timeline, self._config(max_turns=5))
        assert result.passed is True
        assert result.score == 1.0

    def test_max_turns_over_limit_fails(self):
        timeline = self._timeline([("assistant", f"A{i}") for i in range(10)])
        result = evaluate_transcript_rules(timeline, self._config(max_turns=5))
        assert result.passed is False
        assert result.score == 0.0

    # --- min_assistant_turns -----------------------------------------------

    def test_met_floor_emits_no_sub_check(self):
        """A met floor must add no row, or it would dilute the fraction."""
        timeline = self._timeline([("user", "Cancel O1."), ("assistant", "Cancelled O1.")])

        result = evaluate_transcript_rules(timeline, self._config(min_assistant_turns=1))

        assert result.details == []
        assert result.passed is True
        assert result.score == 1.0

    def test_unmet_floor_fails_naming_the_bound_and_both_counts(self):
        timeline = self._timeline([("user", "Cancel O1.")])

        result = evaluate_transcript_rules(timeline, self._config(min_assistant_turns=1))

        assert result.passed is False
        assert result.score == 0.0
        assert [(d.rule_type, d.message) for d in result.details] == [
            ("min_assistant_turns", "Assistant turn count 0 below min_assistant_turns of 1")
        ]

    def test_an_unmet_floor_vetoes_a_passing_fraction(self):
        """The floor is a gate on the component, not one more sub-check.

        Both other rules pass on this trial, so scoring the floor as a fourth
        sub-check would yield 0.667 and any `pass_threshold` at or below that
        would swallow the violation.
        """
        timeline = self._timeline([("user", "Cancel O1."), ("assistant", "Cancelled O1.")])
        config = self._config(
            min_assistant_turns=3,  # fail (1 assistant turn)
            must_contain=["Cancelled"],  # pass
            max_turns=5,  # pass
        )

        result = evaluate_transcript_rules(timeline, config)

        assert result.score == 0.0
        assert sum(1 for d in result.details if d.passed) == 2

    def test_a_zero_activity_trial_passes_when_no_floor_is_declared(self):
        """Opt-in: `max_turns` alone still passes a trial that produced nothing."""
        timeline = self._timeline([("user", "Cancel O1.")])

        result = evaluate_transcript_rules(timeline, self._config(max_turns=18))

        assert result.passed is True
        assert result.score == 1.0

    # --- tool_expectations -------------------------------------------------

    def test_empty_tool_expectations_contributes_no_sub_checks(self):
        """An empty block must not add sub-checks, or it would dilute the fraction."""
        config = self._config(tool_expectations=ToolExpectations())
        result = evaluate_transcript_rules(self._timeline(calls=[self._call("anything")]), config)
        assert result.details == []
        assert result.passed is True
        assert result.score == 1.0

    def test_required_tool_called_successfully_passes(self):
        config = self._config(tool_expectations=ToolExpectations(required_tools=["write_file"]))
        result = evaluate_transcript_rules(self._timeline(calls=[self._call("write_file")]), config)
        assert result.passed is True
        assert result.score == 1.0
        assert [d.rule_type for d in result.details] == ["required_tool"]

    def test_required_tool_never_called_fails_with_named_sub_check(self):
        config = self._config(tool_expectations=ToolExpectations(required_tools=["write_file"]))
        result = evaluate_transcript_rules(self._timeline(calls=[self._call("read_file")]), config)
        assert result.passed is False
        assert result.score == 0.0
        assert len(result.details) == 1
        detail = result.details[0]
        assert detail.rule_type == "required_tool"
        assert "write_file" in detail.message
        assert detail.passed is False

    def test_required_tool_errored_call_does_not_count(self):
        """Mirrors required_actions: a failed call did not do the work."""
        config = self._config(tool_expectations=ToolExpectations(required_tools=["write_file"]))
        result = evaluate_transcript_rules(
            self._timeline(calls=[self._call("write_file", status="error")]), config
        )
        assert result.passed is False
        assert result.score == 0.0

    def test_disallowed_tool_never_called_passes(self):
        config = self._config(
            tool_expectations=ToolExpectations(disallowed_tools=["delete_customer"])
        )
        result = evaluate_transcript_rules(self._timeline(calls=[self._call("read_file")]), config)
        assert result.passed is True
        assert result.score == 1.0
        assert [d.rule_type for d in result.details] == ["disallowed_tool"]

    @pytest.mark.parametrize("status", ["success", "error", "timeout"])
    def test_disallowed_tool_called_at_any_status_fails(self, status):
        """Attempting a forbidden call IS the violation — status is irrelevant."""
        config = self._config(
            tool_expectations=ToolExpectations(disallowed_tools=["delete_customer"])
        )
        result = evaluate_transcript_rules(
            self._timeline(calls=[self._call("delete_customer", status=status)]), config
        )
        assert result.passed is False
        assert result.score == 0.0
        detail = result.details[0]
        assert detail.rule_type == "disallowed_tool"
        assert "delete_customer" in detail.message
        assert status in detail.message

    def test_disallowed_tool_declared_but_never_run_passes(self):
        """A call the agent declared on a terminating turn never reached the
        substrate, so there is no forbidden execution to report. Naming intent as
        the violation is a matcher question, tracked on #678.

        The trial records another call, which is what makes "declared, not
        recorded" mean "did not run" — see the records-absent case below.
        """
        config = self._config(
            tool_expectations=ToolExpectations(disallowed_tools=["delete_customer"])
        )
        timeline = build_timeline(
            [("assistant", "I will remove the customer next.")],
            [self._call("read_file")],
            unexecuted=[ToolCall(id="never_ran", name="delete_customer", arguments={})],
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is True
        assert result.score == 1.0
        assert result.details[0].message == "Disallowed tool 'delete_customer' was never called"

    def test_a_records_less_timeline_fails_every_tool_expectation_by_name(self):
        """Re-grading a recorded bundle is this shape: `tool_log` is not written to
        `trajectory.yaml`, so the message view declares calls and nothing says
        whether they ran. Reading that as "never used" passed every
        `disallowed_tools` check unconditionally (docs/GRADING.md G6b)."""
        config = self._config(
            tool_expectations=ToolExpectations(
                required_tools=["read_file"], disallowed_tools=["delete_customer"]
            )
        )
        timeline = build_timeline(
            [("assistant", "Removing the customer.")],
            unexecuted=[
                ToolCall(id="c1", name="read_file", arguments={}),
                ToolCall(id="c2", name="delete_customer", arguments={}),
            ],
        )

        result = evaluate_transcript_rules(timeline, config)

        assert timeline.records_present is False
        assert result.passed is False
        assert result.score == 0.0
        assert [(d.rule_type, d.passed) for d in result.details] == [
            ("required_tool", False),
            ("disallowed_tool", False),
        ]
        for detail in result.details:
            assert "carries no tool-call record" in detail.message

    def test_a_records_less_timeline_still_clears_a_tool_never_asked_for(self):
        """A record can only name a call the message view declared, so a tool the
        trial never asked for never ran — knowable without the record view."""
        config = self._config(tool_expectations=ToolExpectations(disallowed_tools=["drop_table"]))
        timeline = build_timeline(
            [("assistant", "Reading the file.")],
            unexecuted=[ToolCall(id="c1", name="read_file", arguments={})],
        )

        result = evaluate_transcript_rules(timeline, config)

        assert result.passed is True
        assert result.details[0].message == "Disallowed tool 'drop_table' was never called"

    def test_a_records_less_timeline_names_the_gap_on_a_required_action(self):
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="a1",
                    requestor="assistant",
                    name="delete_customer",
                    arguments={"customer_id": "c1"},
                )
            ]
        )
        timeline = build_timeline(
            [("assistant", "Removing the customer.")],
            unexecuted=[ToolCall(id="c1", name="delete_customer", arguments={"customer_id": "c1"})],
        )

        result = evaluate_transcript_rules(timeline, config)

        assert result.passed is False
        assert "carries no tool-call record" in result.details[0].message

    def test_tool_expectations_decomposes_one_sub_check_per_tool(self):
        """Each declared tool is scored independently, like must_contain entries."""
        config = self._config(
            tool_expectations=ToolExpectations(
                required_tools=["write_file", "read_file"],
                disallowed_tools=["delete_customer", "drop_table"],
            )
        )
        timeline = self._timeline(
            calls=[
                self._call("write_file", sequence=0),
                self._call("delete_customer", sequence=1),
            ]
        )
        result = evaluate_transcript_rules(timeline, config)
        # write_file present (pass), read_file absent (fail), delete_customer
        # called (fail), drop_table untouched (pass).
        assert len(result.details) == 4
        assert result.score == 0.5
        assert result.passed is False

    def test_sub_check_rows_follow_the_declared_order_on_every_run(self):
        """Output is reproducible across runs, and the order is the author's own.

        The rows a caller reads and a snapshot pins carry no set iteration
        anywhere: one row per declared entry, in declaration order. A set-derived
        ordering drifts between processes, which is how these reasons used to
        differ machine to machine for the same trial.
        """
        config = self._config(
            tool_expectations=ToolExpectations(required_tools=["zebra_tool", "alpha_tool"])
        )
        timeline = self._timeline(calls=[self._call("mango_tool")])

        first = evaluate_transcript_rules(timeline, config)
        second = evaluate_transcript_rules(timeline, config)

        assert [d.message for d in first.details] == [
            "Required tool 'zebra_tool' was never called successfully",
            "Required tool 'alpha_tool' was never called successfully",
        ]
        assert [d.model_dump() for d in first.details] == [d.model_dump() for d in second.details]

    # --- required_actions --------------------------------------------------

    def test_required_action_present(self):
        timeline = self._timeline(calls=[self._call("get_order", arguments={"order_id": "123"})])
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    name="get_order",
                    arguments={},
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is True
        assert result.score == 1.0

    def test_required_action_absent_fails(self):
        timeline = self._timeline(calls=[self._call("list_orders")])
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    name="get_order",
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is False
        assert result.score == 0.0

    def test_required_action_compare_args_right_args(self):
        """compare_args subset matches → pass."""
        timeline = self._timeline(
            calls=[
                self._call("book_reservation", arguments={"user_id": "mia_li_3668", "seat": "12A"})
            ]
        )
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="book",
                    requestor="assistant",
                    name="book_reservation",
                    arguments={"user_id": "mia_li_3668"},
                    compare_args=["user_id"],
                )
            ]
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is True
        assert result.score == 1.0

    def test_required_action_compare_args_wrong_args_fails(self):
        """compare_args subset mismatches → fail (the arg value differs)."""
        timeline = self._timeline(
            calls=[
                self._call("book_reservation", arguments={"user_id": "someone_else", "seat": "12A"})
            ]
        )
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="book",
                    requestor="assistant",
                    name="book_reservation",
                    arguments={"user_id": "mia_li_3668"},
                    compare_args=["user_id"],
                )
            ]
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is False
        assert result.score == 0.0

    def test_required_action_requestor_mismatch_fails(self):
        """A call made by the user does not satisfy an assistant-requestor action."""
        timeline = self._timeline(calls=[self._call("get_order", executor="user")])
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    name="get_order",
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is False

    def test_required_action_failed_call_does_not_count(self):
        """A non-success status does not satisfy a required action."""
        timeline = self._timeline(calls=[self._call("get_order", status="error")])
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    name="get_order",
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is False

    def test_required_action_unexecuted_call_does_not_count(self):
        """A call the agent declared on a terminating turn never ran, so it carries
        no status — and absent evidence must fail rather than pass."""
        timeline = build_timeline(
            [("assistant", "Cancelling now.")],
            unexecuted=[ToolCall(id="never_ran", name="get_order", arguments={})],
        )
        config = self._config(
            required_actions=[
                RequiredAction(
                    action_id="get",
                    requestor="assistant",
                    name="get_order",
                    compare_args=[],
                )
            ]
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is False
        assert result.score == 0.0

    # --- communicate_info --------------------------------------------------

    def test_communicate_info_required_present(self):
        timeline = self._timeline([("assistant", "Your Wi-Fi password is aurora-481-fennel.")])
        config = self._config(communicate_info=[{"info": "aurora-481-fennel", "required": True}])
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is True
        assert result.score == 1.0

    def test_communicate_info_required_absent_fails(self):
        timeline = self._timeline([("assistant", "Here is some unrelated text.")])
        config = self._config(communicate_info=[{"info": "aurora-481-fennel", "required": True}])
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is False
        assert result.score == 0.0

    def test_communicate_info_not_required_is_not_scored(self):
        """Non-required info is advisory and produces no sub-check."""
        timeline = self._timeline([("assistant", "Nothing relevant here.")])
        config = self._config(communicate_info=[{"info": "aurora-481-fennel", "required": False}])
        result = evaluate_transcript_rules(timeline, config)
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
        # The required action never happened, so the timeline carries no records.
        timeline = self._timeline([("assistant", "I did nothing useful.")])
        config = self._config(
            must_contain=["confirmation number"],
            required_actions=[
                RequiredAction(
                    action_id="book",
                    requestor="assistant",
                    name="book_reservation",
                    compare_args=[],
                )
            ],
            communicate_info=[{"info": "your refund", "required": True}],
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is False
        assert result.score < 1.0
        # Every sub-check should be present and failing.
        assert len(result.details) == 3
        assert all(d.passed is False for d in result.details)

    def test_combined_fields_mixed_pass_fail(self):
        """Multiple fields together: score is the fraction of sub-checks passed."""
        timeline = self._timeline(
            [
                ("user", "Cancel my booking"),
                ("assistant", "Done — your booking is cancelled."),
            ],
            [self._call("cancel_booking", arguments={"booking_id": "B1"})],
        )
        config = self._config(
            must_contain=["cancelled"],  # pass
            max_turns=5,  # pass (1 assistant turn)
            required_actions=[
                RequiredAction(
                    action_id="cancel",
                    requestor="assistant",
                    name="cancel_booking",
                    compare_args=[],
                )
            ],  # pass
            communicate_info=[{"info": "refund issued", "required": True}],  # fail
        )
        result = evaluate_transcript_rules(timeline, config)
        assert result.passed is False
        assert result.score == 0.75  # 3 of 4 sub-checks pass


class TestCoreEngineTranscriptComponent:
    """What the core engine reports for ``transcript_rules``, through the real grade.

    The evaluator's own arithmetic is locked above; what these lock is the engine's
    caller — the component the author reads in ``grade.yaml`` is the fraction the
    shared evaluator scored, with nothing folded on top of it.
    """

    @staticmethod
    def _component(transcript_rules: dict, messages: list[Message]) -> float | None:
        config = GradingConfig(
            combine={"method": "weighted", "weights": {"transcript_rules": 1.0}},
            transcript_rules=transcript_rules,
        )
        grade = GradingEngine(config).grade_trajectory(
            Trajectory(
                task_id="transcript_component",
                trial_index=0,
                start_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                end_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
                messages=messages,
            ),
            {},
        )
        return grade.components.transcript_rules

    def test_a_declared_but_empty_rule_set_scores_one(self):
        """A block declaring no rule can be violated by nothing, so it scores ``1.0``.

        The block is still declared, so the component is scored rather than left
        unset — presence follows the ``transcript_rules:`` key, not its contents.
        """
        assert self._component({}, [Message(role=MessageRole.USER, content="go")]) == 1.0

    def test_a_single_violated_rule_scores_zero(self):
        """One declared rule, violated, is the whole denominator — no free buckets."""
        component = self._component(
            {"tool_expectations": {"required_tools": ["write_file"]}},
            [
                Message(role=MessageRole.USER, content="write the report"),
                Message(role=MessageRole.ASSISTANT, content="I wrote nothing."),
            ],
        )
        assert component == 0.0


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
        """Numeric-looking STRINGS stay distinct unless a field opts them in."""
        assert consistent_hash(to_hashable("130.00")) != consistent_hash(to_hashable("130.0"))
        assert consistent_hash(to_hashable("72.00")) != consistent_hash(to_hashable(72))

    def test_numeric_strings_fold_when_normalized(self):
        """canonical_number(normalize_strings=True) folds decimal string formats.

        This value-level flag is what the per-field ``numeric_string_fields``
        set turns on for a listed field's values.
        """
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

    def test_to_hashable_folds_strings_only_for_listed_field(self):
        """The core path (to_hashable) honors numeric_string_fields with the same
        per-field selectivity as the runner path (compute_stable_hash): a listed
        money field folds while an unlisted sibling version string is preserved.
        """
        a = {"cases": [{"refund": "130.00", "version": "1.10"}]}
        b = {"cases": [{"refund": "130.0", "version": "1.1"}]}

        # No field set: numeric-looking strings never fold.
        assert consistent_hash(to_hashable(a)) != consistent_hash(to_hashable(b))
        # Only "refund" listed: the "version" 1.10 vs 1.1 difference is preserved,
        # so the two states stay distinct.
        assert consistent_hash(to_hashable(a, frozenset(["refund"]))) != consistent_hash(
            to_hashable(b, frozenset(["refund"]))
        )
        # Both listed: everything numeric folds and the states match.
        assert consistent_hash(to_hashable(a, frozenset(["refund", "version"]))) == consistent_hash(
            to_hashable(b, frozenset(["refund", "version"]))
        )
        # Money-only formatting difference under the listed field folds.
        c = {"cases": [{"refund": "130.00", "version": "1.1"}]}
        d = {"cases": [{"refund": "130.0", "version": "1.1"}]}
        assert consistent_hash(to_hashable(c, frozenset(["refund"]))) == consistent_hash(
            to_hashable(d, frozenset(["refund"]))
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
