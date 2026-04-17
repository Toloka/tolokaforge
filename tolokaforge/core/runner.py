"""Trial runner with agent-user loop"""

import time
from datetime import datetime, timezone
from typing import Any

from tolokaforge.core.logging import StructuredLogger, init_trial_logger
from tolokaforge.core.model_client import LLMClient, UserSimulator
from tolokaforge.core.models import (
    Message,
    MessageRole,
    Metrics,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.tools.registry import ToolExecutor

# Import user tools support (optional for dual-control scenarios)
try:
    from tolokaforge.tools.user_tools import UserToolExecutor
except ImportError:
    UserToolExecutor = None


class TrialRunner:
    """Runs a single trial of a task"""

    def __init__(
        self,
        task_id: str,
        trial_index: int,
        agent_client: LLMClient,
        user_simulator: UserSimulator,
        tool_executor: ToolExecutor,
        tool_schemas: list[dict[str, Any]],
        max_turns: int = 50,
        turn_timeout_s: int = 60,
        episode_timeout_s: int = 1200,
        stuck_detector: StuckDetector | None = None,
        user_tool_executor: Any | None = None,  # UserToolExecutor for dual-control
        request_limiter: GlobalRateLimiter | None = None,
        verbose: bool = False,
        strict: bool = False,
    ):
        self.task_id = task_id
        self.trial_index = trial_index
        self.agent_client = agent_client
        self.user_simulator = user_simulator
        self.tool_executor = tool_executor
        self.tool_schemas = tool_schemas
        self.max_turns = max_turns
        self.turn_timeout_s = turn_timeout_s
        self.episode_timeout_s = episode_timeout_s
        self.stuck_detector = stuck_detector
        self.user_tool_executor = user_tool_executor  # For dual-control scenarios
        self.request_limiter = request_limiter
        self.verbose = verbose
        self.strict = strict

        self.messages: list[Message] = []
        self.metrics = Metrics()
        self.start_time: float = 0.0
        self.logger: StructuredLogger | None = None  # Initialized in run()

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        error_str = str(exc).lower()
        return (
            "429" in error_str
            or "ratelimit" in error_str
            or ("rate" in error_str and "limit" in error_str)
        )

    @staticmethod
    def _normalize_tool_arguments(
        tool_name: str, arguments: dict[str, Any] | None, assistant_text: str
    ) -> dict[str, Any]:
        """Apply conservative argument recovery for common malformed tool calls.

        Some providers occasionally emit a ``write_file`` tool call with only
        ``path`` while placing the intended document in the assistant text. This
        keeps evaluation deterministic by recovering only when the assistant text
        is clearly substantial content.
        """

        normalized = dict(arguments or {})
        if tool_name != "write_file":
            return normalized

        if "content" in normalized:
            return normalized

        candidate = (assistant_text or "").strip()
        if not candidate:
            return normalized

        lower = candidate.lower()
        low_signal_prefixes = (
            "let me",
            "i will",
            "i'll",
            "working on",
            "one moment",
            "starting now",
        )
        if lower.startswith(low_signal_prefixes):
            return normalized

        # Require meaningful payload shape before recovering.
        if len(candidate) < 80 and "\n" not in candidate:
            return normalized

        normalized["content"] = candidate
        return normalized

    def run(self, system_prompt: str, initial_user_message: str = "") -> Trajectory:
        """
        Execute trial with agent-user loop

        Args:
            system_prompt: System prompt with task description and tool schemas
            initial_user_message: If provided, used directly as first user message.
                                  Otherwise, user simulator generates the first message.

        Returns:
            Trajectory with full execution history and results
        """
        # Initialize trial logger
        trial_id = f"{self.task_id}:{self.trial_index}"
        self.logger = init_trial_logger(trial_id, self.verbose, self.strict)

        self.logger.info(
            "Starting trial execution",
            task_id=self.task_id,
            trial_index=self.trial_index,
            max_turns=self.max_turns,
        )

        self.start_time = time.time()
        start_ts = datetime.now(tz=timezone.utc)
        status = TrialStatus.COMPLETED  # Optimistic default
        termination_reason: TerminationReason | None = None

        try:
            # Determine first user message:
            # - If initial_user_message is provided, use it directly (tool-use/Tau style)
            # - Otherwise, generate via user simulator (legacy behavior)
            if initial_user_message.strip():
                first_user_text = initial_user_message
                self.logger.debug("Using provided initial_user_message directly")
            else:
                # Generate first message via user simulator (tau-bench style)
                # The instruction is in user simulator's backstory, NOT sent to agent
                greeting_context = [
                    Message(
                        role=MessageRole.ASSISTANT,
                        content="Hi! How can I help you today?",
                        ts=datetime.now(tz=timezone.utc),
                    )
                ]
                first_user_result = None
                init_attempts = 4
                for attempt in range(1, init_attempts + 1):
                    try:
                        first_user_result = self.user_simulator.reply(greeting_context)
                        break
                    except Exception as exc:
                        is_rate_limit = self._is_rate_limit_error(exc)
                        if is_rate_limit and attempt < init_attempts:
                            wait_s = min(2**attempt, 12)
                            self.logger.warning(
                                "Initial user generation rate-limited; retrying",
                                attempt=attempt,
                                max_attempts=init_attempts,
                                wait_s=wait_s,
                                error=str(exc),
                            )
                            time.sleep(wait_s)
                            continue
                        raise

                if first_user_result is None:
                    raise RuntimeError("Failed to generate initial user message")

                first_user_text = first_user_result.text
                self.logger.debug("User simulator generated first message")

            # Add first user message to conversation
            self.messages.append(
                Message(
                    role=MessageRole.USER, content=first_user_text, ts=datetime.now(tz=timezone.utc)
                )
            )

            # Run turn loop
            for turn in range(self.max_turns):
                # Check episode timeout
                if time.time() - self.start_time > self.episode_timeout_s:
                    status = TrialStatus.TIMEOUT
                    termination_reason = TerminationReason.TIMEOUT
                    self.logger.warning(
                        "Episode timeout reached",
                        elapsed_s=time.time() - self.start_time,
                        timeout_s=self.episode_timeout_s,
                    )
                    self.messages.append(
                        Message(
                            role=MessageRole.SYSTEM,
                            content=f"Episode timeout reached ({self.episode_timeout_s}s). Dialogue terminated.",
                            ts=datetime.now(tz=timezone.utc),
                        )
                    )
                    break

                # Agent generates response
                try:
                    self.logger.debug("Requesting agent response", turn=turn)

                    if self.request_limiter is not None:
                        self.request_limiter.acquire()

                    result = self.agent_client.generate(
                        system=system_prompt,
                        messages=self.messages,
                        tools=self.tool_schemas,
                        tool_choice="auto",
                    )

                    # Update metrics
                    self.metrics.api_calls += 1
                    self.metrics.tokens_input += result.token_usage.get("input", 0)
                    self.metrics.tokens_output += result.token_usage.get("output", 0)
                    if result.cost_usd is not None:
                        if self.metrics.cost_usd_est is None:
                            self.metrics.cost_usd_est = result.cost_usd
                        else:
                            self.metrics.cost_usd_est += result.cost_usd

                    self.logger.debug(
                        "Agent response received",
                        turn=turn,
                        tokens_input=result.token_usage.get("input", 0),
                        tokens_output=result.token_usage.get("output", 0),
                    )

                    # Add assistant message
                    tool_calls_list = None
                    if result.tool_calls:
                        tool_calls_list = result.tool_calls
                        self.logger.debug(
                            "Agent requested tool calls",
                            count=len(result.tool_calls),
                            tools=[tc.name for tc in result.tool_calls],
                        )

                    self.messages.append(
                        Message(
                            role=MessageRole.ASSISTANT,
                            content=result.text,
                            tool_calls=tool_calls_list,
                            reasoning=result.reasoning,  # Include thinking/reasoning for visibility
                            ts=datetime.now(tz=timezone.utc),
                        )
                    )

                    # Check for stuck (after agent response, before tool execution)
                    if self.stuck_detector and self.stuck_detector.is_stuck(
                        self.messages, self.tool_executor.get_logs()
                    ):
                        self.metrics.stuck_detected = True
                        termination_reason = TerminationReason.STUCK_DETECTED
                        self.logger.warning("Stuck condition detected")
                        self.messages.append(
                            Message(
                                role=MessageRole.SYSTEM,
                                content="Stuck condition detected. Dialogue terminated.",
                                ts=datetime.now(tz=timezone.utc),
                            )
                        )
                        break

                    # Check if done (agent signals completion)
                    if self._is_done(result.text):
                        termination_reason = TerminationReason.AGENT_DONE
                        self.logger.info("Agent signaled completion")
                        self.messages.append(
                            Message(
                                role=MessageRole.SYSTEM,
                                content="Agent signaled task completion. Dialogue ended.",
                                ts=datetime.now(tz=timezone.utc),
                            )
                        )
                        break

                    # Execute tool calls if present
                    if result.tool_calls:
                        for tc in result.tool_calls:
                            normalized_args = self._normalize_tool_arguments(
                                tc.name, tc.arguments, result.text
                            )
                            if normalized_args != tc.arguments:
                                self.logger.warning(
                                    "Recovered malformed tool arguments from assistant text",
                                    tool=tc.name,
                                    recovered_keys=sorted(
                                        set(normalized_args.keys())
                                        - set((tc.arguments or {}).keys())
                                    ),
                                )
                                tc.arguments = normalized_args

                            tool_start = time.time()
                            tool_result = self.tool_executor.execute(tc.name, tc.arguments)
                            tool_duration = time.time() - tool_start

                            self.metrics.tool_calls += 1

                            if tool_result.success:
                                self.logger.debug(
                                    "Tool executed successfully",
                                    tool=tc.name,
                                    duration_s=tool_duration,
                                )
                            else:
                                self.logger.warning(
                                    "Tool execution failed", tool=tc.name, error=tool_result.error
                                )

                            # Add tool result message with metadata
                            self.messages.append(
                                Message(
                                    role=MessageRole.TOOL,
                                    content=(
                                        tool_result.output
                                        if tool_result.success
                                        else f"Error: {tool_result.error}"
                                    ),
                                    content_blocks=(
                                        tool_result.content_blocks if tool_result.success else None
                                    ),
                                    tool_call_id=tc.id,
                                    ts=datetime.now(tz=timezone.utc),
                                )
                            )
                        continue  # Continue to next turn for agent to see tool results

                    # No tool calls - user responds
                    user_result = self.user_simulator.reply(self.messages)

                    # Check if user signals completion (tau-bench ###STOP###)
                    if "###STOP###" in user_result.text:
                        termination_reason = TerminationReason.USER_STOP
                        self.logger.info("User signaled completion (###STOP###)")
                        self.messages.append(
                            Message(
                                role=MessageRole.SYSTEM,
                                content="User signaled stop (###STOP###). Dialogue ended.",
                                ts=datetime.now(tz=timezone.utc),
                            )
                        )
                        break

                    # Execute user tool calls if present (before adding message)
                    # Note: Anthropic API doesn't support tool_use from USER role, so we embed results in text
                    # BUT we preserve tool_calls for ActionEvaluator to track required actions
                    user_message_text = user_result.text
                    if user_result.tool_calls and self.user_tool_executor:
                        tool_results_text = []
                        for tc in user_result.tool_calls:
                            tool_start = time.time()
                            tool_result = self.user_tool_executor.execute(tc.name, tc.arguments)
                            tool_duration = time.time() - tool_start

                            self.logger.debug(
                                "User tool executed",
                                tool=tc.name,
                                success=tool_result.success,
                                duration_s=tool_duration,
                            )

                            # Format tool result as text
                            result_text = f"{tc.name}() result: {tool_result.output if tool_result.success else f'Error: {tool_result.error}'}"
                            tool_results_text.append(result_text)

                        # Append tool results to user message
                        if tool_results_text:
                            user_message_text = f"{user_result.text}\n\n" + "\n".join(
                                tool_results_text
                            )

                    # Add single user message with text (including embedded tool results)
                    # Keep tool_calls for ActionEvaluator even though we embed results in text for LLM
                    self.messages.append(
                        Message(
                            role=MessageRole.USER,
                            content=user_message_text,
                            tool_calls=user_result.tool_calls if user_result.tool_calls else None,
                            ts=datetime.now(tz=timezone.utc),
                        )
                    )

                except Exception as e:
                    # Log error
                    self.logger.error(
                        "Error during turn execution",
                        turn=turn,
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                    # Detect specific error types for better classification
                    error_str = str(e)
                    if (
                        "429" in error_str
                        or "RateLimitError" in error_str
                        or "rate limit" in error_str.lower()
                    ):
                        termination_reason = TerminationReason.RATE_LIMIT
                        error_msg = f"Rate limit error: {error_str}. Dialogue terminated."
                    elif "API" in error_str or "OpenAI" in error_str or "Anthropic" in error_str:
                        termination_reason = TerminationReason.API_ERROR
                        error_msg = f"API error: {error_str}. Dialogue terminated."
                    else:
                        termination_reason = TerminationReason.ERROR
                        error_msg = f"Error: {error_str}. Dialogue terminated."

                    # Add error message to conversation as SYSTEM message
                    self.messages.append(
                        Message(
                            role=MessageRole.SYSTEM,
                            content=error_msg,
                            ts=datetime.now(tz=timezone.utc),
                        )
                    )

                    status = TrialStatus.ERROR
                    # In strict mode, the logger.error() above will raise
                    # Otherwise, we break the loop
                    break
            else:
                # for-else: loop finished without break (max turns reached)
                termination_reason = TerminationReason.MAX_TURNS
                self.messages.append(
                    Message(
                        role=MessageRole.SYSTEM,
                        content=f"Maximum turns ({self.max_turns}) reached. Dialogue terminated.",
                        ts=datetime.now(tz=timezone.utc),
                    )
                )

        except Exception as e:
            # Catch-all for initialization errors
            status = TrialStatus.ERROR
            termination_reason = TerminationReason.ERROR
            self.logger.error(
                "Trial initialization error", error=str(e), error_type=type(e).__name__
            )
            # Add system message for initialization error
            self.messages.append(
                Message(
                    role=MessageRole.SYSTEM,
                    content=f"Trial initialization error: {str(e)}. Dialogue terminated.",
                    ts=datetime.now(tz=timezone.utc),
                )
            )
            if self.strict:
                raise

        # Finalize metrics
        end_ts = datetime.now(tz=timezone.utc)
        self.metrics.latency_total_s = time.time() - self.start_time
        self.metrics.turns = len([m for m in self.messages if m.role == MessageRole.ASSISTANT])

        # Calculate tool success rate (combine agent and user tool logs)
        tool_logs = self.tool_executor.get_logs()
        user_tool_logs = self.user_tool_executor.get_logs() if self.user_tool_executor else []

        # Combine logs and mark source
        combined_logs = [{**log, "executor": "agent"} for log in tool_logs] + [
            {**log, "executor": "user"} for log in user_tool_logs
        ]

        if combined_logs:
            success_count = sum(1 for log in combined_logs if log.get("success", False))
            self.metrics.tool_success_rate = success_count / len(combined_logs)
            self.metrics.tool_calls = len(combined_logs)

        self.logger.info(
            "Trial execution finished",
            status=status.value,
            turns=self.metrics.turns,
            tool_calls=self.metrics.tool_calls,
            latency_s=self.metrics.latency_total_s,
        )

        # Create trajectory with status and termination reason
        trajectory = Trajectory(
            task_id=self.task_id,
            trial_index=self.trial_index,
            start_ts=start_ts,
            end_ts=end_ts,
            status=status,
            termination_reason=termination_reason,
            messages=self.messages,
            metrics=self.metrics,
            tool_log=combined_logs,
        )

        return trajectory

    def _is_done(self, text: str) -> bool:
        """Check if agent signals completion"""
        done_markers = [
            "###STOP###",
        ]
        text_lower = text.lower()
        return any(marker in text_lower for marker in done_markers)
