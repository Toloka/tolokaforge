"""Combine grading components into final score"""

import logging
from pathlib import Path
from typing import Any

from tolokaforge.core.evaluators.action_evaluator import ActionEvaluator
from tolokaforge.core.evaluators.communicate_evaluator import CommunicateEvaluator
from tolokaforge.core.grading.check_runner import CheckRunner
from tolokaforge.core.grading.checks_helpers import build_check_context, custom_checks_enabled
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CustomChecksConfig,
    TaskContext,
    ToolCallStatus,
    Transcript,
)
from tolokaforge.core.grading.checks_interface import (
    Message as CheckMessage,
)
from tolokaforge.core.grading.checks_interface import (
    ToolCall as CheckToolCall,
)
from tolokaforge.core.grading.combine_method import combine_by_method
from tolokaforge.core.grading.state_checks import (
    GoldenReplayError,
    StateChecker,
    extract_db_state,
)
from tolokaforge.core.grading.state_composition import (
    compose_state_checks_score,
    inert_hash_weight_reason,
    resolve_hash_weight,
)
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.grading.transcript import TranscriptChecker
from tolokaforge.core.models import (
    CustomCheckDetail,
    Grade,
    GradeComponents,
    GradingConfig,
    InitialStateConfig,
    Trajectory,
)

logger = logging.getLogger(__name__)

_HASH_NOT_CHECKED_NO_SOURCE = (
    "state_checks.hash is enabled but declares neither expected_state_hash nor "
    "golden_actions, so the state hash was not checked"
)
_HASH_NOT_CHECKED_NO_REPLAY_CONTEXT = (
    "state_checks.hash.golden_actions needs the task's directory, initial_state and "
    "mcp_server to replay, and this grading engine has none, so the state hash was "
    "not checked"
)


class GradingEngine:
    """Combine deterministic grading components (state checks, transcript rules,
    custom checks).

    The LLM/rubric judge is NOT part of this engine — it runs runner-side on the
    shared :class:`~tolokaforge.core.loop.ToolCallingLoop` via ``GradeTrial``
    (see ``runner/service.py`` and ``core/grading/judge.py``). This engine covers
    only the deterministic, in-process components used by the non-runner grading
    paths (tests / NativeAdapter helpers).
    """

    def __init__(
        self,
        grading_config: GradingConfig,
        task_domain: str = "general",
        task_dir: Path | None = None,
        task_initial_state: InitialStateConfig | None = None,
        task_mcp_server: str | None = None,
    ):
        self.config = grading_config
        self.task_domain = task_domain
        self.task_dir = task_dir
        self.task_initial_state = task_initial_state
        self.task_mcp_server = task_mcp_server
        self.state_checker = StateChecker()
        self.transcript_checker = TranscriptChecker()
        self.action_evaluator = ActionEvaluator()
        self.communicate_evaluator = CommunicateEvaluator()

    def grade_trajectory(
        self,
        trajectory: Trajectory,
        final_env_state: dict[str, Any],
    ) -> Grade:
        """
        Grade a complete trajectory (deterministic components only)

        Args:
            trajectory: Trial trajectory with messages and metrics
            final_env_state: Final environment state

        Returns:
            Grade with score and components
        """
        components = GradeComponents()
        reasons_parts = []
        state_diff_result = None  # Will store diff if state check fails
        custom_checks_details = None  # Will store detailed custom check results

        timeline = build_trial_timeline(
            trajectory.messages, trajectory.tool_log, trajectory.termination_reason
        )

        # State checks
        if self.config.state_checks:
            state_score, state_reasons, state_diff_result = self._grade_state_checks(
                final_env_state
            )
            if state_reasons:
                reasons_parts.append(f"State: {state_reasons}")

            components.state_checks = state_score

        # Transcript rules
        if self.config.transcript_rules:
            # Use tau2 action evaluator if required_actions specified
            if self.config.transcript_rules.required_actions:
                action_result = self.action_evaluator.evaluate_actions(
                    trajectory=trajectory.messages,
                    required_actions=self.config.transcript_rules.required_actions,
                )
                action_score = action_result.score
                if action_result.reasons:
                    reasons_parts.extend(action_result.reasons)
            else:
                action_score = 1.0

            # Use tau2 communicate evaluator if communicate_info specified
            if self.config.transcript_rules.communicate_info:
                comm_result = self.communicate_evaluator.evaluate_communication(
                    trajectory=trajectory.messages,
                    communicate_info=self.config.transcript_rules.communicate_info,
                )
                comm_score = comm_result.score
                if comm_result.reasons:
                    reasons_parts.extend(comm_result.reasons)
            else:
                comm_score = 1.0

            # Use legacy transcript checker for other rules
            legacy_score, transcript_reasons = self.transcript_checker.grade(
                timeline=timeline,
                must_contain=self.config.transcript_rules.must_contain,
                disallow_regex=self.config.transcript_rules.disallow_regex,
                max_turns=self.config.transcript_rules.max_turns,
                min_assistant_turns=self.config.transcript_rules.min_assistant_turns,
                required_tools=(
                    self.config.transcript_rules.tool_expectations.required_tools
                    if self.config.transcript_rules.tool_expectations
                    else None
                ),
                disallowed_tools=(
                    self.config.transcript_rules.tool_expectations.disallowed_tools
                    if self.config.transcript_rules.tool_expectations
                    else None
                ),
            )
            if transcript_reasons:
                reasons_parts.append(f"Transcript: {transcript_reasons}")

            # Combine transcript scores (product for strictness)
            transcript_score = action_score * comm_score * legacy_score
            components.transcript_rules = transcript_score

        # LLM Judge — NOT computed here. The rubric judge runs runner-side on the
        # shared ToolCallingLoop (runner/service.py GradeTrial → core/grading/judge.py).
        # This engine intentionally leaves ``components.llm_judge`` unset.

        # Custom Python Checks
        if custom_checks_enabled(self.config.custom_checks) and self.task_dir:
            custom_score, custom_reasons, custom_checks_details = self._run_custom_checks(
                trajectory=trajectory,
                final_env_state=final_env_state,
                custom_config=self.config.custom_checks,
            )
            components.custom_checks = custom_score
            if custom_reasons:
                reasons_parts.append(f"Custom: {custom_reasons}")

        final_score, binary_pass = self._combine(components)

        return Grade(
            binary_pass=binary_pass,
            score=final_score,
            components=components,
            reasons=" | ".join(reasons_parts) if reasons_parts else "All checks passed",
            state_diff=state_diff_result,
            custom_checks_details=custom_checks_details,
        )

    def _combine(self, components: GradeComponents) -> tuple[float, bool]:
        """Aggregate the scored components into ``(score, binary_pass)`` by the author's method.

        ``combine.weights`` decides which components enter the map at all: a scored
        component with no declared weight is left out of the mean's numerator, its
        denominator and the aggregation. With nothing in the map there is nothing to
        aggregate, and the trial's verdict is the threshold comparison alone.
        """
        weights = self.config.combine.weights
        component_scores: dict[str, float] = {}
        final_score = 0.0
        total_weight = 0.0
        for name, score in (
            ("state_checks", components.state_checks),
            ("transcript_rules", components.transcript_rules),
            ("llm_judge", components.llm_judge),
            ("custom_checks", components.custom_checks),
        ):
            if score is None or name not in weights:
                continue
            component_scores[name] = score
            final_score += score * weights[name]
            total_weight += weights[name]

        if total_weight > 0:
            final_score = final_score / total_weight

        if not component_scores:
            return final_score, final_score >= self.config.combine.pass_threshold

        return combine_by_method(
            method=self.config.combine.method,
            component_scores=component_scores,
            weighted_mean=final_score,
            pass_threshold=self.config.combine.pass_threshold,
        )

    def _grade_state_checks(
        self, final_env_state: dict[str, Any]
    ) -> tuple[float | None, str, dict[str, Any] | None]:
        """Fold the configured state-check sources into one ``state_checks`` score.

        The two sources read two levels of ``final_env_state``: JSONPath assertions
        read it whole, so an author writes ``$.db.<table>``, and the hash reads the
        unwrapped database inside it (:func:`extract_db_state`).
        """
        checks = self.config.state_checks
        hash_score, hash_reasons, diff_result = self._check_state_hash(final_env_state)
        jsonpath_score, jsonpath_reasons = self.state_checker.check_jsonpaths(
            final_env_state, checks.jsonpaths
        )
        if hash_score is not None and not checks.jsonpaths:
            # An empty assertion list scores a vacuous 1.0, which must not blend
            # against a real hash verdict — a hash-failing tau-style pack would
            # collect jsonpath credit for assertions it never made.
            jsonpath_score = None

        # Re-resolved here rather than trusted from load: ``state_checks.hash`` is an
        # untyped dict, so nothing stops a caller mutating it after validation.
        hash_weight = resolve_hash_weight(
            checks.hash,
            jsonpaths=checks.jsonpaths,
            context="grading.yaml state_checks.hash.weight",
        )
        score = compose_state_checks_score(
            hash_score=hash_score,
            jsonpath_score=jsonpath_score,
            hash_weight=hash_weight,
        )
        inert_reason = inert_hash_weight_reason(
            hash_score=hash_score, jsonpath_score=jsonpath_score, hash_weight=hash_weight
        )
        reasons = jsonpath_reasons + hash_reasons
        if inert_reason:
            reasons.append(inert_reason)
        return score, "; ".join(reasons), diff_result

    def _check_state_hash(
        self, final_env_state: dict[str, Any]
    ) -> tuple[float | None, list[str], dict[str, Any] | None]:
        """Return the state-hash verdict, its reasons, and the state diff.

        ``None`` is *no verdict*: hash grading is off, or it is on and could not
        run — which is reported rather than scored as a failed hash check.
        """
        checks = self.config.state_checks
        hash_config = checks.hash or {}
        if not hash_config.get("enabled", False):
            return None, [], None

        db_state = extract_db_state(final_env_state)
        expected_hash = hash_config.get("expected_state_hash")
        if expected_hash:
            score, reason = self.state_checker.check_hash(
                db_state, expected_hash, numeric_string_fields=checks.numeric_string_fields
            )
            return score, [reason], None

        golden_actions = hash_config.get("golden_actions")
        if not golden_actions:
            return None, [_HASH_NOT_CHECKED_NO_SOURCE], None
        if not (self.task_dir and self.task_initial_state and self.task_mcp_server):
            return None, [_HASH_NOT_CHECKED_NO_REPLAY_CONTEXT], None
        if not self.task_initial_state.json_db:
            raise GoldenReplayError(
                "state_checks.hash.golden_actions must replay against the task's "
                "initial_state.json_db, and this task declares none"
            )

        score, reason, diff_result = self.state_checker.check_hash_against_golden_replay(
            db_state=db_state,
            golden_actions=golden_actions,
            task_dir=self.task_dir,
            initial_state_path=self.task_initial_state.json_db,
            mcp_server_path=self.task_mcp_server,
            task_domain=self.task_domain,
            numeric_string_fields=checks.numeric_string_fields,
        )
        return score, [reason], diff_result

    def _run_custom_checks(
        self,
        trajectory: Trajectory,
        final_env_state: dict[str, Any],
        custom_config: dict[str, Any],
    ) -> tuple[float, str, list[CustomCheckDetail] | None]:
        """
        Run custom Python checks from checks.py.

        Args:
            trajectory: Trial trajectory with messages
            final_env_state: Final environment state
            custom_config: Custom checks configuration from grading.yaml

        Returns:
            Tuple of (score, reasons_string, detailed_results)
        """
        if not self.task_dir:
            logger.warning("Cannot run custom checks: task_dir not set")
            return 0.0, "task_dir not available", None

        # Parse config
        config = CustomChecksConfig(**custom_config)
        checks_file = self.task_dir / config.file

        if not checks_file.exists():
            logger.warning(f"Custom checks file not found: {checks_file}")
            return 0.0, f"checks file not found: {config.file}", None

        # Build CheckContext
        try:
            ctx = self._build_check_context(trajectory, final_env_state)
        except Exception as e:
            logger.error(f"Error building CheckContext: {e}")
            return 0.0, f"context build error: {e}", None

        # Run checks
        logger.info(f"Running custom checks from {checks_file}")
        runner = CheckRunner()
        result = runner.run(
            checks_file=checks_file,
            task_dir=self.task_dir,
            ctx=ctx,
            config=config,
        )

        # Log results and build detailed results list
        detailed_results: list[CustomCheckDetail] = []
        for check_result in result.results:
            # Handle both enum and string status values
            status = check_result.status
            status_str = status.value if hasattr(status, "value") else str(status)
            status_emoji = {
                "passed": "✓",
                "failed": "✗",
                "error": "!",
                "skipped": "○",
            }.get(status_str, "?")
            logger.info(
                f"  {status_emoji} {check_result.check_name}: {status_str} - {check_result.message}"
            )

            # Build detailed result
            detailed_results.append(
                CustomCheckDetail(
                    check_name=check_result.check_name,
                    status=status_str,
                    score=check_result.score,
                    message=check_result.message,
                    details=check_result.details,
                )
            )

        if result.error:
            logger.error(f"Custom checks error: {result.error}")
            return 0.0, f"execution error: {result.error}", detailed_results

        # Build reasons string
        reasons = []
        if result.passed > 0:
            reasons.append(f"{result.passed} passed")
        if result.failed > 0:
            reasons.append(f"{result.failed} failed")
        if result.errors > 0:
            reasons.append(f"{result.errors} errors")
        if result.skipped > 0:
            reasons.append(f"{result.skipped} skipped")

        return (
            result.aggregate_score,
            ", ".join(reasons) if reasons else "no checks",
            detailed_results,
        )

    def _build_check_context(
        self,
        trajectory: Trajectory,
        final_env_state: dict[str, Any],
    ) -> CheckContext:
        """Build the host-side :class:`CheckContext` from a :class:`Trajectory`.

        Owns the transcript-from-``Trajectory`` transform (rich :class:`Message`
        objects → author-facing :class:`CheckMessage`); the state-shape rule
        is delegated to :func:`build_check_context` so the runner path applies
        the same precedence by construction.
        """
        messages: list[CheckMessage] = []
        for msg in trajectory.messages:
            tool_calls = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append(
                        CheckToolCall(
                            name=tc.name,
                            arguments=tc.arguments,
                            result=None,
                            status=ToolCallStatus.SUCCESS,
                        )
                    )
            messages.append(
                CheckMessage(
                    role=msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                    content=msg.content,
                    tool_calls=tool_calls,
                )
            )

        initial_json_db: dict[str, Any] | None = None
        if self.task_initial_state and isinstance(self.task_initial_state.json_db, dict):
            initial_json_db = self.task_initial_state.json_db

        return build_check_context(
            initial_state_json_db=initial_json_db,
            final_env_state=final_env_state,
            transcript=Transcript(messages=messages),
            task=TaskContext(
                task_id=trajectory.task_id,
                task_name=trajectory.task_id,
                domain=self.task_domain,
            ),
        )
