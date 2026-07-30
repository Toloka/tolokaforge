"""Trial runner with agent-user loop"""

import time
from datetime import datetime, timezone
from typing import Any

from tolokaforge.core.llm import GenerationResult, LLMClient, UserSimulator
from tolokaforge.core.logging import StructuredLogger, init_trial_logger
from tolokaforge.core.logging_context import trial_id_scope
from tolokaforge.core.loop import (
    LoopConfig,
    MetricsSink,
    TerminationDecision,
    ToolCallingLoop,
    UserTurnResult,
)
from tolokaforge.core.models import (
    Message,
    MessageRole,
    Metrics,
    RateLimitProbeBucketMetrics,
    RateLimitProbeRoleMetrics,
    RecordedToolCall,
    TerminationReason,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    LLMCallObservation,
    RateLimitProbeStats,
    RunDisplayEvents,
)
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.tools.registry import ToolExecutor, resolve_tool_status

# Import user tools support (optional for dual-control scenarios)
try:
    from tolokaforge.tools.user_tools import UserToolExecutor
except ImportError:
    UserToolExecutor = None


def _as_utc(ts: float | None) -> datetime | None:
    """``time.time()`` epoch seconds as an aware UTC datetime, ``None`` passthrough."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)
class TrialToolCallRecorder:
    """The trial's single ordered tool-call record.

    One list and one counter for the whole trial, so ``sequence`` is execution
    order across every executor. Satisfies
    :class:`~tolokaforge.core.models.ToolCallRecorder`.
    """

    def __init__(self) -> None:
        self._recorded: list[RecordedToolCall] = []

    def record(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: ToolExecutorIdentity,
        status: ToolExecutionStatus,
        output: str,
        latency_seconds: float,
    ) -> None:
        self._recorded.append(
            RecordedToolCall(
                call_id=call_id,
                sequence=len(self._recorded),
                tool_name=tool_name,
                arguments=arguments,
                executor=executor,
                status=status,
                output=output,
                latency_seconds=latency_seconds,
                timestamp=datetime.now(tz=timezone.utc),
            )
        )

    @property
    def recorded(self) -> tuple[RecordedToolCall, ...]:
        return tuple(self._recorded)

    def recorded_for(self, executor: ToolExecutorIdentity) -> tuple[RecordedToolCall, ...]:
        """The calls one executor made, in trial order.

        Stuck detection is a policy over the agent's own repetition, so it must
        read the agent's stream alone — a user-side call sitting in its last-N
        window would dilute it.
        """
        return tuple(call for call in self._recorded if call.executor is executor)


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
        events: RunDisplayEvents = _NULL_EVENTS,
        probe_stats: RateLimitProbeStats | None = None,
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
        self._events = events
        # Non-``None`` only under rate-limit probe mode. Shared by the agent and
        # user observations so both roles' 429s land in one per-trial total, and
        # copied onto ``Metrics`` when the trial finalises.
        self._probe_stats = probe_stats

        self.messages: list[Message] = []
        self.tool_call_recorder = TrialToolCallRecorder()
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
        # Built in ``run()`` before the first user-simulator reply; the None
        # sentinel matches the ``observation`` default on
        # :meth:`UserSimulator.reply` for tests that drive the runner's helper
        # methods directly.
        self._user_observation: LLMCallObservation | None = None
        # Set when the simulator emits a substantive final reply glued to the
        # ``###STOP###`` token in the same message. On the next user turn the
        # runner terminates before calling the simulator so the agent gets
        # exactly one more turn to act on the delivered reply, then the loop
        # ends with ``USER_STOP``.
        self._user_stop_pending: bool = False

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

    @property
    def _rate_limit_probe_active(self) -> bool:
        """True when rate-limit probe mode is on for this trial.

        The stats accumulator exists exactly when the mode is enabled — the
        conductor builds one only in that case — so it doubles as the flag.
        """
        return self._probe_stats is not None

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
        # Bind the trial identity for the whole execution so every log record
        # emitted here and in the tool-calling loop it drives is tagged for the
        # panel's per-trial log view.
        with trial_id_scope(trial_id):
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

            # Per-trial × role observations threaded into the agent's LLM client
            # (via the loop) and into the user-simulator's ``reply`` call sites so
            # the RunDisplayEvents trio (started / finished / retry_scheduled)
            # fires with the correct ``trial_id`` + ``role``. The ``LLMClient`` is
            # shared across concurrent trials — the identity must ride the call,
            # not the client.
            self._user_observation = LLMCallObservation(
                events=self._events,
                trial_id=trial_id,
                role="user",
                probe_stats=self._probe_stats,
            )

            try:
                self._seed_first_user_message(initial_user_message)

                outcome = ToolCallingLoop(
                    llm_client=self.agent_client,
                    tool_executor=self.tool_executor,
                    tool_schemas=self.tool_schemas,
                    config=LoopConfig(
                        max_turns=self.max_turns,
                        episode_timeout_s=self.episode_timeout_s,
                    ),
                    metrics=_AgentMetricsSink(
                        self.metrics,
                        events=self._events,
                        trial_id=trial_id,
                    ),
                    should_terminate=self._agent_termination,
                    user_turn=self._agent_user_turn,
                    recorder=self.tool_call_recorder,
                    request_limiter=self.request_limiter,
                    normalize_tool_arguments=self._normalize_tool_arguments,
                    logger=self.logger,
                    call_observation=LLMCallObservation(
                        events=self._events,
                        trial_id=trial_id,
                        role="agent",
                        probe_stats=self._probe_stats,
                    ),
                ).run(system_prompt, self.messages, self.start_time)

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
            self._apply_probe_stats()

            recorded_calls = self.tool_call_recorder.recorded
            if recorded_calls:
                success_count = sum(
                    1 for call in recorded_calls if call.status is ToolExecutionStatus.SUCCESS
                )
                self.metrics.tool_success_rate = success_count / len(recorded_calls)
                self.metrics.tool_calls = len(recorded_calls)

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
                tool_log=list(recorded_calls),
            )

            return trajectory

    def _apply_probe_stats(self) -> None:
        """Copy the trial's rate-limit probe accounting onto :class:`Metrics`.

        No-op outside probe mode, which leaves every counter at its default so a
        normal run's ``metrics.yaml`` carries zeros rather than a signal that
        does not exist.

        Both censuses are copied: the 429 side into ``rate_limit_*`` and the
        success side into ``probe_*``. The per-``(role, model)`` rows carry both
        and are emitted in sorted key order; ``probe_buckets`` is sorted
        window-first so the series reads as a timeline.
        """
        stats = self._probe_stats
        if stats is None:
            return
        self.metrics.rate_limit_retries = stats.retries
        self.metrics.rate_limit_wait_s = stats.wait_s
        self.metrics.rate_limit_first_ts = _as_utc(stats.first_ts)
        self.metrics.rate_limit_last_ts = _as_utc(stats.last_ts)
        self.metrics.rate_limit_by_role_model = [
            RateLimitProbeRoleMetrics(
                role=role,
                model=model,
                retries=counters.retries,
                wait_s=counters.wait_s,
                first_ts=_as_utc(counters.first_ts),
                last_ts=_as_utc(counters.last_ts),
                successful_calls=counters.successes,
                success_duration_s=counters.success_duration_s,
                prompt_tokens=counters.prompt_tokens,
                completion_tokens=counters.completion_tokens,
            )
            for (role, model), counters in sorted(stats.by_role_model.items())
        ]
        self.metrics.probe_successful_calls = stats.successes
        self.metrics.probe_success_duration_s = stats.success_duration_s
        self.metrics.probe_prompt_tokens = stats.prompt_tokens
        self.metrics.probe_completion_tokens = stats.completion_tokens
        self.metrics.probe_bucket_width_s = stats.bucket_width_s
        self.metrics.probe_dropped_buckets = stats.dropped_buckets
        self.metrics.probe_buckets = [
            RateLimitProbeBucketMetrics(
                # ``bucket_start`` is already an exact integer epoch second, so
                # this render is lossless and identical across run legs.
                bucket_start_ts=datetime.fromtimestamp(start, tz=timezone.utc),
                role=role,
                model=model,
                successful_calls=counters.successes,
                success_duration_s=counters.success_duration_s,
                prompt_tokens=counters.prompt_tokens,
                completion_tokens=counters.completion_tokens,
                retries=counters.retries,
                wait_s=counters.wait_s,
            )
            for (start, role, model), counters in sorted(
                ((start, role, model), counters)
                for (role, model, start), counters in stats.by_bucket.items()
            )
        ]
        if stats.dropped_buckets:
            self.logger.warning(
                "Rate-limit probe dropped throughput buckets at the cap",
                probe_dropped_buckets=stats.dropped_buckets,
                probe_max_buckets=stats.max_buckets,
                probe_bucket_width_s=stats.bucket_width_s,
            )
        if stats.retries:
            self.logger.warning(
                "Rate-limit probe absorbed 429s",
                rate_limit_retries=stats.retries,
                rate_limit_wait_s=round(stats.wait_s, 3),
                by_role_model={
                    f"{role}/{model}": counters.retries
                    for (role, model), counters in sorted(stats.by_role_model.items())
                },
            )

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
        retrying on rate limits — and *only* on rate limits: any other error is
        re-raised on the first attempt. The task instruction lives in the
        simulator's backstory and is NOT sent to the agent.

        This runs before the loop, so no episode-timeout check can interrupt it
        mid-flight, but its wall time is *consumed from* the episode budget
        rather than added to it: ``run()`` sets ``self.start_time`` before
        calling this and hands that same ``start_time`` to the loop.
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
            # Probe mode collapses this to one attempt. This loop only ever
            # retries 429s (see the ``is_rate_limit and`` guard below), and under
            # probe mode the simulator's own client already polls 429s at a fixed
            # interval for up to its per-call budget — strictly more tolerant
            # than 4 attempts of 2/4/8 s backoff — so the outer attempts are
            # redundant. Dropping them also keeps this step's worst case at one
            # simulator budget instead of ``init_attempts`` of them, which is
            # what makes the budget invariant alone sufficient to bound the trial
            # under its ``max(300, episode_s * 2)`` queue lease. Non-429 errors
            # are unaffected: they were never retried here, and the client's own
            # five-attempt exponential path still covers them under probe mode.
            init_attempts = 1 if self._rate_limit_probe_active else 4
            for attempt in range(1, init_attempts + 1):
                try:
                    first_user_result = self.user_simulator.reply(
                        greeting_context, observation=self._user_observation
                    )
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
            messages, self.tool_call_recorder.recorded_for(ToolExecutorIdentity.AGENT)
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

        Stop-token handling has two shapes:

        * Bare ``###STOP###`` (or the token with only whitespace before it) —
          terminate immediately with ``USER_STOP``.
        * Substantive text glued to ``###STOP###`` in one message — deliver the
          pre-token text as a normal USER message, set a pending flag, and
          terminate on the following user turn. Guarantees the agent sees the
          final reply (e.g. a backstory-mandated verbal decline) before the
          dialogue ends.
        """
        if self._user_stop_pending:
            self.logger.info("User signaled completion (###STOP### after final reply)")
            self._user_stop_pending = False
            return UserTurnResult(
                termination=TerminationDecision(
                    reason=TerminationReason.USER_STOP,
                    system_message="User signaled stop (###STOP### after final reply). Dialogue ended.",
                )
            )

        user_result = self.user_simulator.reply(messages, observation=self._user_observation)

        if "###STOP###" in user_result.text:
            pre_stop_text, _, _ = user_result.text.partition("###STOP###")
            pre_stop_text = pre_stop_text.rstrip()
            if not pre_stop_text:
                self.logger.info("User signaled completion (###STOP###)")
                return UserTurnResult(
                    termination=TerminationDecision(
                        reason=TerminationReason.USER_STOP,
                        system_message="User signaled stop (###STOP###). Dialogue ended.",
                    )
                )
            self.logger.info(
                "User sent final reply with ###STOP### — delivering reply, stop pending"
            )
            self._user_stop_pending = True
            user_result.text = pre_stop_text

        user_message_text = user_result.text
        if user_result.tool_calls and self.user_tool_executor:
            tool_results_text = []
            for tc in user_result.tool_calls:
                tool_start = time.time()
                tool_result = self.user_tool_executor.execute(tc.name, tc.arguments, call_id=tc.id)
                tool_duration = time.time() - tool_start

                self.tool_call_recorder.record(
                    call_id=tc.id,
                    tool_name=tc.name,
                    arguments=tc.arguments or {},
                    executor=ToolExecutorIdentity.USER,
                    status=resolve_tool_status(tool_result),
                    output=(
                        tool_result.output if tool_result.success else (tool_result.error or "")
                    ),
                    latency_seconds=tool_duration,
                )

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
    contract. Every ``record_generation`` also fires
    :meth:`RunDisplayEvents.trial_progress` on the injected events sink so
    the live display accumulates per-turn deltas alongside the run-level
    cumulative counters it derives from ``run_started`` / ``trial_*``.
    """

    def __init__(
        self,
        metrics: Metrics,
        *,
        events: RunDisplayEvents = _NULL_EVENTS,
        trial_id: str = "",
    ) -> None:
        self._metrics = metrics
        self._events = events
        self._trial_id = trial_id

    def record_generation(self, result: GenerationResult) -> None:
        self._metrics.api_calls += 1
        self._metrics.usage = self._metrics.usage + result.usage
        if result.cost_usd is not None:
            if self._metrics.cost_usd is None:
                self._metrics.cost_usd = result.cost_usd
            else:
                self._metrics.cost_usd += result.cost_usd
        self._events.trial_progress(
            trial_id=self._trial_id,
            prompt_tokens_delta=result.usage.prompt_tokens,
            completion_tokens_delta=result.usage.completion_tokens,
            cost_delta_usd=result.cost_usd if result.cost_usd is not None else 0.0,
        )

    def record_tool_call(self) -> None:
        self._metrics.tool_calls += 1
