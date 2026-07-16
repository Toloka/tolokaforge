"""Trial runner with agent-user loop"""

import time
from datetime import datetime, timezone
from typing import Any

from tolokaforge.core.llm import GenerationResult, LLMClient, UserSimulator
from tolokaforge.core.logging import StructuredLogger, init_trial_logger
from tolokaforge.core.loop import (
    InterventionHandler,
    LoopConfig,
    LoopObserver,
    MetricsSink,
    TerminationDecision,
    ToolCallingLoop,
    UserTurnResult,
)
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
        loop_observer: LoopObserver | None = None,
        intervention_handler: InterventionHandler | None = None,
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
        self.loop_observer = loop_observer
        self.intervention_handler = intervention_handler

        self.messages: list[Message] = []
        self.metrics = Metrics()
        self.start_time: float = 0.0
        self.logger: StructuredLogger | None = None  # Initialized in run()
        self._effective_system_prompt: str | None = None
        self._effective_system_prompt_captured: bool = False
        # Captured from UserSimulator.last_system_prompt once the LLM
        # simulator has fired at least one reply. The orchestrator reads
        # both prompts off the runner after ``run()`` returns and persists
        # them via :meth:`FileArtifactWriter.write_prompts` so analytics
        # can audit which simulator prompt drove ``###STOP###`` fires.
        # Scripted simulators never populate this — stays ``None``.
        self._user_system_prompt_captured: str | None = None

    @property
    def effective_system_prompt(self) -> str | None:
        """Agent's post-policy system prompt as actually sent on the wire.

        Captured from :attr:`GenerationResult.effective_system_prompt` on
        the first turn; ``None`` until ``run()`` has issued at least one
        agent generation. Read by the orchestrator after ``run()``
        returns and persisted to ``prompts.yaml``.
        """
        return self._effective_system_prompt

    @property
    def user_system_prompt(self) -> str | None:
        """User simulator's system prompt for this trial.

        Captured from :attr:`UserSimulator.last_system_prompt` after the
        first simulator reply. ``None`` for scripted simulators (which
        carry no LLM-shaped prompt) or when ``run()`` has not yet driven
        a simulator turn.
        """
        return self._user_system_prompt_captured

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
            self._seed_first_user_message(initial_user_message)

            loop_kwargs: dict[str, Any] = {
                "llm_client": self.agent_client,
                "tool_executor": self.tool_executor,
                "tool_schemas": self.tool_schemas,
                "config": LoopConfig(
                    max_turns=self.max_turns,
                    episode_timeout_s=self.episode_timeout_s,
                ),
                "metrics": _AgentMetricsSink(self.metrics),
                "should_terminate": self._agent_termination,
                "user_turn": self._agent_user_turn,
                "request_limiter": self.request_limiter,
                "normalize_tool_arguments": self._normalize_tool_arguments,
                "logger": self.logger,
            }
            if self.loop_observer is not None:
                loop_kwargs["observer"] = self.loop_observer
            if self.intervention_handler is not None:
                loop_kwargs["intervention_handler"] = self.intervention_handler
            outcome = ToolCallingLoop(**loop_kwargs).run(
                system_prompt, self.messages, self.start_time
            )

            status = outcome.status
            termination_reason = outcome.termination_reason
            if outcome.captured_effective_system_prompt is not None:
                self._effective_system_prompt = outcome.captured_effective_system_prompt
                self._effective_system_prompt_captured = True

        except Exception as e:
            # Catch-all for initialization errors (first-user-message generation)
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

        # Stage 7 (P5) — pull simulator prompt from the (possibly None)
        # attribute exposed by UserSimulator. LLM mode populates it on every
        # reply; scripted mode leaves it None. Overwrite our cached copy on
        # every trial-end so that if a follow-up reply revised the prompt,
        # we land the latest version. Guard against non-string values
        # (e.g. MagicMock in tests) — a real UserSimulator never populates a
        # non-string-non-None value, but silently coercing garbage onto the
        # Trajectory would violate AGENTS.md rule #1.
        sim_prompt = getattr(self.user_simulator, "last_system_prompt", None)
        if isinstance(sim_prompt, str) and sim_prompt:
            self._user_system_prompt_captured = sim_prompt

        # Create trajectory with status and termination reason. Both
        # system prompts are read off the runner via the
        # :attr:`effective_system_prompt` / :attr:`user_system_prompt`
        # properties and persisted by the orchestrator into
        # ``prompts.yaml`` — they no longer ride on Trajectory.
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

    def _seed_first_user_message(self, initial_user_message: str) -> None:
        """Determine and append the first user message before the loop runs.

        If ``initial_user_message`` is provided, use it directly (tool-use / Tau
        style). Otherwise generate it via the user simulator (legacy behaviour),
        retrying on rate limits. The task instruction lives in the simulator's
        backstory and is NOT sent to the agent.
        """
        if initial_user_message.strip():
            first_user_text = initial_user_message
            self.logger.debug("Using provided initial_user_message directly")
        else:
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

        self.messages.append(
            Message(
                role=MessageRole.USER,
                content=first_user_text,
                ts=datetime.now(tz=timezone.utc),
            )
        )

    def _agent_termination(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None:
        """Agent termination policy: stuck detection then ``###STOP###``/_is_done.

        Mirrors the historical order — stuck is checked before the done marker.
        Stuck sets ``metrics.stuck_detected`` as a side effect, matching the
        original loop.
        """
        if self.stuck_detector and self.stuck_detector.is_stuck(
            messages, self.tool_executor.get_logs()
        ):
            self.metrics.stuck_detected = True
            self.logger.warning("Stuck condition detected")
            return TerminationDecision(
                reason=TerminationReason.STUCK_DETECTED,
                system_message="Stuck condition detected. Dialogue terminated.",
            )

        if self._is_done(result.text):
            self.logger.info("Agent signaled completion")
            return TerminationDecision(
                reason=TerminationReason.AGENT_DONE,
                system_message="Agent signaled task completion. Dialogue ended.",
            )

        return None

    def _agent_user_turn(self, messages: list[Message]) -> UserTurnResult:
        """Agent user turn: simulator reply, ``###STOP###`` detection, user tools.

        Embeds user tool-call results in the user message text (Anthropic does
        not support ``tool_use`` from the USER role) while preserving the
        original ``tool_calls`` so ``ActionEvaluator`` can track required actions.
        """
        user_result = self.user_simulator.reply(messages)

        if "###STOP###" in user_result.text:
            self.logger.info("User signaled completion (###STOP###)")
            return UserTurnResult(
                termination=TerminationDecision(
                    reason=TerminationReason.USER_STOP,
                    system_message="User signaled stop (###STOP###). Dialogue ended.",
                )
            )

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

                result_text = f"{tc.name}() result: {tool_result.output if tool_result.success else f'Error: {tool_result.error}'}"
                tool_results_text.append(result_text)

            if tool_results_text:
                user_message_text = f"{user_result.text}\n\n" + "\n".join(tool_results_text)

        return UserTurnResult(
            message=Message(
                role=MessageRole.USER,
                content=user_message_text,
                tool_calls=user_result.tool_calls if user_result.tool_calls else None,
                ts=datetime.now(tz=timezone.utc),
            )
        )


class _AgentMetricsSink(MetricsSink):
    """Accumulates the agent's per-call usage/cost and tool counts into the
    trial :class:`Metrics`, preserving the original field-wise semantics.

    ``Usage.__add__`` is field-wise; ``calls`` concatenate (preserving per-call
    cost_source / latency_s); ``provider_raw`` is "latest wins" per the Usage
    contract.
    """

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics

    def record_generation(self, result: GenerationResult) -> None:
        self._metrics.api_calls += 1
        self._metrics.usage = self._metrics.usage + result.usage
        if result.cost_usd is not None:
            if self._metrics.cost_usd is None:
                self._metrics.cost_usd = result.cost_usd
            else:
                self._metrics.cost_usd += result.cost_usd

    def record_tool_call(self) -> None:
        self._metrics.tool_calls += 1
