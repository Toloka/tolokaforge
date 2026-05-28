"""Combine grading components into final score"""

import logging
from pathlib import Path
from typing import Any

from tolokaforge.core.evaluators.action_evaluator import ActionEvaluator
from tolokaforge.core.evaluators.communicate_evaluator import CommunicateEvaluator
from tolokaforge.core.evaluators.environment_evaluator import EnvironmentEvaluator
from tolokaforge.core.grading.check_runner import CheckRunner
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CustomChecksConfig,
    EnvironmentState,
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
from tolokaforge.core.grading.judge import LLMJudge
from tolokaforge.core.grading.state_checks import StateChecker
from tolokaforge.core.grading.transcript import TranscriptChecker
from tolokaforge.core.models import (
    CustomCheckDetail,
    Grade,
    GradeComponents,
    GradingConfig,
    InitialStateConfig,
    ModelConfig,
    Trajectory,
)

logger = logging.getLogger(__name__)


class GradingEngine:
    """Combine multiple grading components"""

    def __init__(
        self,
        grading_config: GradingConfig,
        judge_model: ModelConfig | None = None,
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
        self.judge = LLMJudge(judge_model) if judge_model else None
        # New tau2 evaluators
        self.env_evaluator = EnvironmentEvaluator()
        self.action_evaluator = ActionEvaluator()
        self.communicate_evaluator = CommunicateEvaluator()

    def grade_trajectory(
        self,
        trajectory: Trajectory,
        final_env_state: dict[str, Any],
        workspace_dir: Path | None = None,
    ) -> Grade:
        """
        Grade a complete trajectory

        Args:
            trajectory: Trial trajectory with messages and metrics
            final_env_state: Final environment state
            workspace_dir: Agent workspace directory (for agentic judge file reading)

        Returns:
            Grade with score and components
        """
        components = GradeComponents()
        reasons_parts = []
        state_diff_result = None  # Will store diff if state check fails
        custom_checks_details = None  # Will store detailed custom check results

        # State checks
        if self.config.state_checks:
            # Use new tau2 environment evaluator if env_assertions or db_hash_check
            use_tau2_evaluator = (
                self.config.state_checks.env_assertions or self.config.state_checks.db_hash_check
            )

            if use_tau2_evaluator:
                # Use new tau2-faithful environment evaluator
                result = self.env_evaluator.evaluate_state_checks(
                    final_state=final_env_state,
                    state_checks_config=self.config.state_checks,
                    domain=self.task_domain,
                )
                state_score = result.score
                if result.reasons:
                    reasons_parts.extend(result.reasons)
            else:
                # Use legacy state checker for backward compatibility
                hash_config = self.config.state_checks.hash
                expected_hash = None
                hash_weight = 0.5
                golden_actions = None

                if hash_config and hash_config.get("enabled", False):
                    # Check for pre-computed hash from adapter first (preferred for Tau)
                    expected_hash = hash_config.get("expected_state_hash")

                    # Check for tau-bench style golden actions (fallback if no pre-computed hash)
                    if not expected_hash and "golden_actions" in hash_config:
                        golden_actions = hash_config["golden_actions"]

                    hash_weight = hash_config.get(
                        "weight", 1.0
                    )  # Default to 1.0 for tau-bench compatibility

                # If we have pre-computed hash from adapter, use simple hash comparison
                if expected_hash:
                    # For adapter-based tasks, compare db state (which is adapter_env.data)
                    # The final_env_state has structure like {"agent": {}, "user": {}, "db": {...}}
                    db_state = final_env_state.get(
                        "db", final_env_state.get("agent", final_env_state)
                    )

                    state_score, state_reasons = self.state_checker.grade(
                        state=db_state,
                        jsonpath_assertions=self.config.state_checks.jsonpaths,
                        expected_hash=expected_hash,
                        hash_weight=hash_weight,
                    )
                # Use tau-style grading if golden_actions are present and MCP context available
                elif (
                    golden_actions
                    and self.task_dir
                    and self.task_initial_state
                    and self.task_mcp_server
                ):
                    if not self.task_initial_state.json_db:
                        state_score = 0.0
                        state_reasons = (
                            "Cannot use tau-style grading: no initial_state.json_db specified"
                        )
                    else:
                        state_score, state_reasons, state_diff_result = (
                            self.state_checker.grade_tau_style(
                                state=final_env_state,
                                jsonpath_assertions=self.config.state_checks.jsonpaths,
                                golden_actions=golden_actions,
                                task_dir=self.task_dir,
                                initial_state_path=self.task_initial_state.json_db,
                                mcp_server_path=self.task_mcp_server,
                                task_domain=self.task_domain,
                                hash_weight=hash_weight,
                            )
                        )
                else:
                    # Use standard grading with no hash (backward compatible)
                    state_score, state_reasons = self.state_checker.grade(
                        state=final_env_state,
                        jsonpath_assertions=self.config.state_checks.jsonpaths,
                        expected_hash=None,
                        hash_weight=hash_weight,
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
                messages=trajectory.messages,
                tool_log=trajectory.tool_log,
                must_contain=self.config.transcript_rules.must_contain,
                disallow_regex=self.config.transcript_rules.disallow_regex,
                max_turns=self.config.transcript_rules.max_turns,
                required_tools=(
                    self.config.transcript_rules.tool_expectations.get("required_tools")
                    if self.config.transcript_rules.tool_expectations
                    else None
                ),
                disallowed_tools=(
                    self.config.transcript_rules.tool_expectations.get("disallowed_tools")
                    if self.config.transcript_rules.tool_expectations
                    else None
                ),
            )
            if transcript_reasons:
                reasons_parts.append(f"Transcript: {transcript_reasons}")

            # Combine transcript scores (product for strictness)
            transcript_score = action_score * comm_score * legacy_score
            components.transcript_rules = transcript_score

        # LLM Judge
        if self.config.llm_judge and self.judge:
            judge_score, judge_reasons = self.judge.grade(
                messages=trajectory.messages,
                rubric=self.config.llm_judge.rubric,
                output_schema=self.config.llm_judge.output_schema,
                task_description=trajectory.task_id,
                workspace_dir=workspace_dir,
                agentic=getattr(self.config.llm_judge, "agentic", False),
                system_prompt=getattr(self.config.llm_judge, "system_prompt", None),
                tool_packs=getattr(self.config.llm_judge, "tool_packs", None),
            )
            components.llm_judge = judge_score
            if judge_reasons:
                reasons_parts.append(f"Judge: {judge_reasons}")

        # Custom Python Checks
        if self.config.custom_checks and self.task_dir:
            custom_config = self.config.custom_checks
            if custom_config.get("enabled", False):
                custom_score, custom_reasons, custom_checks_details = self._run_custom_checks(
                    trajectory=trajectory,
                    final_env_state=final_env_state,
                    custom_config=custom_config,
                )
                components.custom_checks = custom_score
                if custom_reasons:
                    reasons_parts.append(f"Custom: {custom_reasons}")

        # Combine scores with weights
        final_score = 0.0
        total_weight = 0.0

        weights = self.config.combine.weights
        if components.state_checks is not None and "state_checks" in weights:
            final_score += components.state_checks * weights["state_checks"]
            total_weight += weights["state_checks"]

        if components.transcript_rules is not None and "transcript_rules" in weights:
            final_score += components.transcript_rules * weights["transcript_rules"]
            total_weight += weights["transcript_rules"]

        if components.llm_judge is not None and "llm_judge" in weights:
            final_score += components.llm_judge * weights["llm_judge"]
            total_weight += weights["llm_judge"]

        if components.custom_checks is not None and "custom_checks" in weights:
            final_score += components.custom_checks * weights["custom_checks"]
            total_weight += weights["custom_checks"]

        # Normalize score
        if total_weight > 0:
            final_score = final_score / total_weight

        # Binary pass/fail
        binary_pass = final_score >= self.config.combine.pass_threshold

        return Grade(
            binary_pass=binary_pass,
            score=final_score,
            components=components,
            reasons=" | ".join(reasons_parts) if reasons_parts else "All checks passed",
            state_diff=state_diff_result,
            custom_checks_details=custom_checks_details,
        )

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
        """
        Build CheckContext from trajectory and final state.

        Args:
            trajectory: Trial trajectory
            final_env_state: Final environment state

        Returns:
            CheckContext for custom checks
        """
        # Build transcript with tool calls
        messages: list[CheckMessage] = []
        for msg in trajectory.messages:
            tool_calls = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls.append(
                        CheckToolCall(
                            name=tc.name,
                            arguments=tc.arguments,
                            result=None,  # We don't have results stored in messages
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

        transcript = Transcript(messages=messages)

        # Get initial state from task_initial_state if available
        initial_data = {}
        if self.task_initial_state and self.task_initial_state.json_db:
            if isinstance(self.task_initial_state.json_db, dict):
                initial_data = self.task_initial_state.json_db
            # If it's a path, we'd need to load it - for now, leave empty

        # Handle nested state structure (agent/user/db) or flat state
        # Custom checks need flat state data, so extract from appropriate level
        if "agent" in final_env_state and isinstance(final_env_state.get("agent"), dict):
            # Use agent state which contains the mutated data from tool calls
            final_state_data = final_env_state["agent"]
        elif "db" in final_env_state and isinstance(final_env_state.get("db"), dict):
            # Fallback to db state
            final_state_data = final_env_state["db"]
        else:
            # Use as-is (flat state)
            final_state_data = final_env_state

        # Pass filesystem state through so custom checks can access agent-produced files
        if "filesystem" in final_env_state and "filesystem" not in final_state_data:
            final_state_data = {**final_state_data, "filesystem": final_env_state["filesystem"]}

        return CheckContext(
            initial_state=EnvironmentState(data=initial_data),
            final_state=EnvironmentState(data=final_state_data),
            transcript=transcript,
            task=TaskContext(
                task_id=trajectory.task_id,
                task_name=trajectory.task_id,
                domain=self.task_domain,
            ),
        )
