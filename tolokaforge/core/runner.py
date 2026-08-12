"""Trial runner with agent-user loop"""

import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from tolokaforge.core.actors.actor import Actor
from tolokaforge.core.actors.reply_guard import UserReplyRefused
from tolokaforge.core.actors.turn_policy import TurnPolicy, TurnState
from tolokaforge.core.llm import SIMULATOR_GREETING, GenerationResult, LLMClient, UserSimulator
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
    FirstUserMessageSource,
    Message,
    MessageRole,
    Metrics,
    RateLimitProbeBucketMetrics,
    RateLimitProbeRoleMetrics,
    RecordedToolCall,
    ReplyDefect,
    TerminationReason,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
    TrialStatus,
    UserReplyGuardEvent,
    UserReplyOutcome,
)
from tolokaforge.core.models.task_config import InteractionMode, TaskConfig
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    LLMCallObservation,
    RateLimitProbeStats,
    RunDisplayEvents,
)
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.tools.registry import ToolExecutor, resolve_tool_output, resolve_tool_status

# Import user tools support (optional for dual-control scenarios)
try:
    from tolokaforge.tools.user_tools import UserToolExecutor
except ImportError:
    UserToolExecutor = None

_AGENT_DONE_MARKERS: tuple[str, ...] = ("###STOP###",)


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
        user_simulator: UserSimulator | None,
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
        interaction_mode: InteractionMode = "conversational",
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
        self.interaction_mode = interaction_mode
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
        # How turn 0 was delivered, stamped by ``_seed_first_user_message`` and
        # carried onto the trajectory. Stays ``None`` when the bootstrap never
        # completed — a trial that failed before turn 0 has no such source.
        self._first_user_message_source: FirstUserMessageSource | None = None
        # One entry per dispatched user turn the reply guard did not accept on
        # its first generation, carried onto the trajectory.
        self._user_reply_guard_events: list[UserReplyGuardEvent] = []
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
                # Deferred import: the plugin registry pulls the conductor
                # protocol, which pulls this runner module — an eager top-level
                # import would loop.
                from tolokaforge.core.plugin_registry import (
                    TurnPolicyContext,
                    load_turn_policy,
                )

                policy = load_turn_policy(self.interaction_mode)(
                    TurnPolicyContext(user_simulator=self.user_simulator)
                )
                task_config = TaskConfig(
                    task_id=self.task_id,
                    description="",
                    interaction_mode=self.interaction_mode,
                )
                self._seed_first_user_message(task_config, policy, initial_user_message)

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
                    user_turn=lambda messages: self._policy_user_turn(policy, messages),
                    recorder=self.tool_call_recorder,
                    request_limiter=self.request_limiter,
                    normalize_tool_arguments=self._normalize_tool_arguments,
                    classify_error=self.agent_client.classify_loop_error,
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
                first_user_message_source=self._first_user_message_source,
                messages=self.messages,
                user_reply_guard_events=list(self._user_reply_guard_events),
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
        """Whether the agent's text carries a completion marker.

        Both operands are folded by the same call at the point of comparison,
        so the match cannot be made case-blind on one side only.
        """
        folded = text.casefold()
        return any(marker.casefold() in folded for marker in _AGENT_DONE_MARKERS)

    def _seed_first_user_message(
        self,
        task_config: TaskConfig,
        policy: TurnPolicy,
        initial_user_message: str,
    ) -> None:
        """Determine and append the first user message before the loop runs.

        Delegates to ``policy.bootstrap(...)``: a policy may short-circuit to a
        caller-provided literal (tool-use / Tau style, agent-monologue seed),
        route to a user simulator to synthesise turn 0 (today's default
        conversational path), or raise :class:`ValueError` when a required seed
        is missing (agent-only with no ``initial_user_message``).

        The simulator invocation retries on rate limits — and *only* on rate
        limits: any other error is re-raised on the first attempt. This runs
        before the loop, so no episode-timeout check can interrupt it mid-flight,
        but its wall time is *consumed from* the episode budget rather than
        added to it: ``run()`` sets ``self.start_time`` before calling this and
        hands that same ``start_time`` to the loop.
        """
        seed = initial_user_message if initial_user_message.strip() else None
        decision = policy.bootstrap(task_config, seed)

        if decision.first_user_message is not None:
            first_user_text = decision.first_user_message
            self._first_user_message_source = FirstUserMessageSource.PINNED
        elif decision.bootstrap_via_simulator:
            first_user_text = self._bootstrap_via_simulator()
            self._first_user_message_source = FirstUserMessageSource.SIMULATOR
        else:
            raise RuntimeError(
                "BootstrapDecision must supply first_user_message or bootstrap_via_simulator=True"
            )

        self.logger.info(
            "First user message delivered",
            source=self._first_user_message_source.value,
        )
        self.messages.append(
            Message(
                role=MessageRole.USER,
                content=first_user_text,
                ts=datetime.now(tz=timezone.utc),
            )
        )

    def _record_user_reply_guard(
        self,
        *,
        message_index: int,
        outcome: UserReplyOutcome,
        rejected: Sequence[ReplyDefect],
    ) -> None:
        """Record what one dispatched user turn cost the reply guard.

        A turn the guard accepted on its first generation rejected nothing and
        records nothing, so a trial that never broke frame carries an empty list
        rather than one no-op row per turn.
        """
        if not rejected:
            return
        self._user_reply_guard_events.append(
            UserReplyGuardEvent(
                message_index=message_index,
                outcome=outcome,
                rejected=list(rejected),
            )
        )

    def _bootstrap_via_simulator(self) -> str:
        """Synthesise turn 0 by dispatching the user simulator against a canned
        agent greeting. Retries on rate limits only.

        Probe mode collapses this to one attempt. The retry loop only ever
        catches 429s (see the ``is_rate_limit`` guard below), and under probe
        mode the simulator's own client already polls 429s at a fixed interval
        for up to its per-call budget — strictly more tolerant than 4 attempts
        of 2/4/8 s backoff — so the outer attempts are redundant. Dropping them
        also keeps this step's worst case at the simulator budgets one guarded
        reply can spend (``USER_REPLY_MAX_ATTEMPTS`` of them, the term
        ``turn_budget_s`` already carries) instead of ``init_attempts`` times as
        many, which is what makes the budget invariant alone sufficient to bound
        the trial under its ``max(300, episode_s * 2)`` queue lease. Non-429
        errors are unaffected: they were never retried here, and the client's own
        five-attempt exponential path still covers them under probe mode.
        """
        if self.user_simulator is None:
            raise RuntimeError(
                "bootstrap_via_simulator requires a user simulator; the conductor "
                "must construct one for interaction_mode='conversational'."
            )
        greeting_context = [
            Message(
                role=MessageRole.ASSISTANT,
                content=SIMULATOR_GREETING,
                ts=datetime.now(tz=timezone.utc),
            )
        ]
        init_attempts = 1 if self._rate_limit_probe_active else 4
        for attempt in range(1, init_attempts + 1):
            try:
                first_user_result = self.user_simulator.reply(
                    greeting_context, observation=self._user_observation
                )
                self._record_user_reply_guard(
                    message_index=len(self.messages),
                    outcome=UserReplyOutcome.DELIVERED,
                    rejected=first_user_result.guard_rejections,
                )
                # An empty opening would seed the transcript with a blank USER
                # turn: the simulator's flipped context then drops it, loses
                # every trace of having asked, and restarts the conversation —
                # the failure mode the seeded-opening fix exists to prevent.
                if not first_user_result.text.strip():
                    raise RuntimeError(
                        "User simulator bootstrap produced an empty first message; "
                        "a blank opening cannot seed the conversation."
                    )
                self.logger.debug("User simulator generated first message")
                return first_user_result.text
            except UserReplyRefused as exc:
                # Before the re-raise: the trial dies here, and the evidence for
                # why has to outlive the exception. A refusal is never a rate
                # limit, so it must not reach the retry branch below either.
                self._record_user_reply_guard(
                    message_index=len(self.messages),
                    outcome=UserReplyOutcome.REFUSED,
                    rejected=exc.rejected,
                )
                raise
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

        raise RuntimeError("Failed to generate initial user message")

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

    def _policy_user_turn(self, policy: TurnPolicy, messages: list[Message]) -> UserTurnResult:
        """Route the loop's optional user turn through ``policy.next_actor``.

        The loop invokes this shim only when the just-completed agent turn
        produced no tool calls (``loop.py``'s ``_advance_user_turn`` gate), so
        the :class:`TurnState` handed to the policy sets
        ``last_agent_had_tool_calls=False``. ``turn_index`` is the count of
        assistant messages so far — the next actor the policy hands back is the
        speaker of turn ``turn_index + 1``.

        Three possible policy outcomes:

        * :class:`ActorTurn` — dispatch the actor (the historical path).
          :class:`~tolokaforge.core.actors.turn_policy.ConversationalTurnPolicy`
          takes this branch byte-for-byte identically to today.
        * :class:`TerminationDecision` — surface it as
          ``UserTurnResult(termination=...)``. The loop honors it and stops.
          :class:`~tolokaforge.core.actors.turn_policy.AgentOnlyTurnPolicy`
          takes this branch: agent has no more actions and no user party
          exists to advance the conversation, so the trial is done.
        * ``None`` — reserved for future policies that want to skip a
          turn without terminating; returns an empty
          :class:`UserTurnResult` and the loop advances to the next agent
          turn. Not exercised by either built-in.
        """
        state = TurnState(
            messages=messages,
            last_agent_had_tool_calls=False,
            turn_index=sum(1 for m in messages if m.role == MessageRole.ASSISTANT),
        )
        decision = policy.next_actor(state)
        if decision is None:
            return UserTurnResult()
        if isinstance(decision, TerminationDecision):
            return UserTurnResult(termination=decision)
        return self._dispatch_user_actor(decision.actor, messages)

    def _dispatch_user_actor(self, actor: Actor, messages: list[Message]) -> UserTurnResult:
        """Run one user actor turn: reply, ``###STOP###`` detection, user tools.

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

        # Read before the dispatch: this is the position the turn's USER message
        # will occupy, and on a stop token or a refusal the loop puts its own
        # SYSTEM message there instead.
        message_index = len(messages)
        try:
            user_result = actor.reply(messages, observation=self._user_observation)
        except UserReplyRefused as exc:
            self._record_user_reply_guard(
                message_index=message_index,
                outcome=UserReplyOutcome.REFUSED,
                rejected=exc.rejected,
            )
            raise
        self._record_user_reply_guard(
            message_index=message_index,
            outcome=UserReplyOutcome.DELIVERED,
            rejected=user_result.guard_rejections,
        )

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
                    output=resolve_tool_output(tool_result),
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
