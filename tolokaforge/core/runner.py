"""Trial runner with agent-user loop"""

import shlex
import time
from collections.abc import Collection, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from tolokaforge_coding_harnesses.stdout_telemetry import (
    HarnessStdoutTelemetry,
    parse_harness_stdout,
)
from tolokaforge_coding_harnesses.usage_log import (
    MIDDLEWARE_PROXY_USAGE_SOURCE,
    HarnessRequestOutcomes,
    sum_harness_usage_records,
    summarise_harness_requests,
)

from tolokaforge.core.actors.actor import Actor
from tolokaforge.core.actors.reply_guard import UserReplyRefused
from tolokaforge.core.actors.tool_turn_rule import UserToolTurnRule
from tolokaforge.core.actors.tool_turns import agent_view
from tolokaforge.core.actors.turn_policy import TurnPolicy, TurnState
from tolokaforge.core.actors.user_simulator import UserSimulator
from tolokaforge.core.actors.user_stop import UserStop, UserStopRule
from tolokaforge.core.failure_attribution import EXCLUDED_TYPED_REASONS
from tolokaforge.core.grading.trace_timeline import (
    TimelineInconsistencyError,
    build_trial_timeline,
)
from tolokaforge.core.llm import (
    SIMULATOR_GREETING,
    GenerationResult,
    LLMClient,
    Usage,
)
from tolokaforge.core.llm.client import ParserError
from tolokaforge.core.logging import StructuredLogger, init_trial_logger
from tolokaforge.core.logging_context import trial_id_scope
from tolokaforge.core.loop import (
    AgentLoopContext,
    LoopConfig,
    LoopOutcome,
    MetricsSink,
    TerminationDecision,
    UserTurnResult,
    episode_timeout_decision,
)
from tolokaforge.core.models import (
    CostByRoleMetrics,
    CostByRoleModelMetrics,
    FirstUserMessageSource,
    Message,
    MessageRole,
    Metrics,
    ParserErrorRecord,
    RateLimitProbeBucketMetrics,
    RateLimitProbeRoleMetrics,
    RecordedToolCall,
    ReplyDefect,
    TerminationReason,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
    TrialStatus,
    UserReplyGuardEvent,
    UserReplyOutcome,
)
from tolokaforge.core.models.task_config import InteractionMode, TaskConfig
from tolokaforge.core.pricing import MODEL_PRICING, estimate_cost, resolve_pricing
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    LLMCallObservation,
    LLMCallRole,
    RateLimitProbeStats,
    RunDisplayEvents,
    conversation_session_id,
)
from tolokaforge.core.stuck import StuckDetector
from tolokaforge.core.summarize_policy import LLMSummarizer, SummarizePolicy
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.runner.protocol import TrialNotRegisteredError
from tolokaforge.tools.registry import ToolExecuting, resolve_tool_output, resolve_tool_status

if TYPE_CHECKING:
    from tolokaforge.observability.observer import LoopObserver

_HARNESS_USAGE_READ_CALL_ID_PREFIX = "harness-usage:"
"""Call-id prefix for the engine's own read of a harness trial's usage records.

Distinct from the ``harness:`` id the CLI's own exec carries, so the runner-side
execution record the read unavoidably leaves is attributable to the engine
rather than readable as a second thing the agent did."""

_USAGE_READ_DETAIL_CHARS = 200
"""How much of a failed usage read's output the log carries.

Enough to name the cause (``cat``'s "No such file or directory" is the expected
one) without spilling an unbounded container stream into the trial log."""

BUILT_IN_AGENT_LOOP = "engine-loop"
"""The ``tolokaforge.agent_loops`` registration of the loop this repo ships.

:attr:`TrialRunner.agent_loop` defaults to it. The post-conditions read no
loop name: the built-in loop earns its denominator-excluding reasons the way
any other implementation does, by carrying the evidence
:func:`~tolokaforge.core.loop.classify_loop_error` hands it.
"""

_RECONCILIATION_DETAIL_CHARS = 400
"""How much of a timeline reconciliation failure the log carries.

Enough for the first unlinkable call id and the counts around it, without
copying an unbounded tool name or argument blob into the trial log.
"""


def _call_names(calls: list[ToolCall]) -> str:
    """The names of *calls*, in order, for a log line or a system message."""
    return ", ".join(call.name for call in calls)


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


_VENDOR_COST_TOLERANCE = 1.25
"""How far our price may sit from a CLI's own before the trial says so.

Wide on purpose. The two figures are allowed to differ — rounding, a retry the
CLI folded away, a list rate against a negotiated one — and the failure worth
catching is a *multiple*: the live drifts that motivated this were 1.4x, 2.5x
and 4.6x.
"""

_COST_ROLLUP_RESIDUAL_TOLERANCE_USD = 1e-9
"""Below this, ``cost_usd − Σ(usage.calls[*].cost_usd)`` is float noise, not
unattributed spend, so no phantom ``agent`` residual row is emitted.

An LLM-loop trial's ``cost_usd`` is the running sum of the very call costs the
rollup re-sums, so the residual is exact zero up to summation order; a real
harness residual is the whole trial cost, orders of magnitude above this."""


def _enabled_completion_tools(
    tool_schemas: list[dict[str, Any]], sourced_tool_names: Collection[str] = ()
) -> frozenset[str]:
    """The completion-tool names among *tool_schemas*, per the builtin registry.

    Read off the tool surface the model is actually offered rather than off the
    run config, so the termination seam and the schema list can never disagree
    about which tools can end an episode.

    *sourced_tool_names* are the offered tools the trial reconstructed from a
    :class:`~tolokaforge.runner.models.ToolSource` — a pack's or an adapter's
    own implementation, which the builtin registry knows nothing about. The
    name alone does not make a tool the end-of-episode signal: a pack shipping
    its own ``submit`` would otherwise end the trial at the call site, before
    the tool it actually named ever ran.
    """
    # Deferred: importing the builtin package pulls every tool driver, and this
    # module is on the orchestrator's import path well before any tool is built.
    from tolokaforge.tools.builtin import registry as builtin_registry

    sourced = set(sourced_tool_names)
    names = {schema.get("function", {}).get("name", "") for schema in tool_schemas}
    return frozenset(
        name for name in names if name not in sourced and builtin_registry.is_completion(name)
    )


class TrialRunner:
    """Runs a single trial of a task"""

    def __init__(
        self,
        task_id: str,
        trial_index: int,
        agent_client: LLMClient,
        user_simulator: UserSimulator | None,
        tool_executor: ToolExecuting,
        tool_schemas: list[dict[str, Any]],
        max_turns: int = 50,
        turn_timeout_s: int = 60,
        episode_timeout_s: int = 1200,
        stuck_detector: StuckDetector | None = None,
        user_tool_executor: ToolExecuting | None = None,
        request_limiter: GlobalRateLimiter | None = None,
        verbose: bool = False,
        strict: bool = False,
        events: RunDisplayEvents = _NULL_EVENTS,
        probe_stats: RateLimitProbeStats | None = None,
        interaction_mode: InteractionMode = "conversational",
        agent_loop: str = BUILT_IN_AGENT_LOOP,
        tool_output_max_chars_by_tool: dict[str, int] | None = None,
        loop_observer: "LoopObserver | None" = None,
        sourced_tool_names: Collection[str] = (),
        user_stop: UserStopRule = UserStopRule(),
        user_tool_turns: UserToolTurnRule = UserToolTurnRule(),
        first_agent_message: str | None = None,
        trace_id: str | None = None,
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
        # The enabled agent tools whose call ends the episode. Empty unless an
        # operator put a completion tool in ``tools.agent.enabled`` *and* the
        # trial reconstructed it as the builtin, which is what keeps this seam
        # inert for every pack that terminates through its user simulator and
        # for one that ships a tool of the same name.
        self._completion_tools = _enabled_completion_tools(tool_schemas, sourced_tool_names)
        self.user_tool_executor = user_tool_executor
        self.request_limiter = request_limiter
        self.verbose = verbose
        self.strict = strict
        self.interaction_mode = interaction_mode
        # Name in the ``tolokaforge.agent_loops`` entry-point group. :meth:`run`
        # resolves it to the loop that drives the agent's turns before the turn
        # cycle starts; the orchestrator refuses an unregistered name at run
        # start, ahead of any trial.
        self.agent_loop = agent_loop
        self._events = events
        self.tool_output_max_chars_by_tool = tool_output_max_chars_by_tool
        # Non-``None`` only under rate-limit probe mode. Shared by the agent and
        # user observations so both roles' 429s land in one per-trial total, and
        # copied onto ``Metrics`` when the trial finalises.
        self._probe_stats = probe_stats
        # Live tracing (ADR-0047): the trial's observer bound to the agent role, or None.
        self._loop_observer = loop_observer
        self._user_stop = user_stop
        self._user_tool_turns = user_tool_turns
        # ``actors.user.first_agent_message``: written as the transcript's first
        # message, ahead of turn 0, when set. The message written is kept, so the
        # turn counts can leave out the one assistant message no model generated.
        self._first_agent_message = first_agent_message
        self._opening_line: Message | None = None
        # The trial attempt's trace id (``TrialIdentity.trace_id``); ``None`` leaves
        # both roles' calls without a conversation identity.
        if trace_id is not None and not trace_id.strip():
            raise ValueError(
                f"TrialRunner trace_id must be a non-empty id or None, got {trace_id!r}"
            )
        self._trace_id = trace_id

        self.messages: list[Message] = []
        self.tool_call_recorder = TrialToolCallRecorder()
        # One assigner for the whole trial, drawn from by both actors — the
        # agent's loop takes it as an argument — so a raw provider id one actor
        # used is disambiguated for the other rather than recorded twice.
        self._call_ids = EpisodeUniqueCallIds()
        self.metrics = Metrics()
        # Both staged by :meth:`run_harness` and applied at trial end by
        # :meth:`_apply_harness_telemetry`; ``None`` on every other way a
        # trial can be driven. The first is what the CLI printed, the second
        # the usage records a middleware proxy wrote for a CLI that printed
        # nothing, read back out of the trial container while it was still up.
        self._harness_stdout_telemetry: HarnessStdoutTelemetry | None = None
        self._harness_usage_records: str | None = None
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
        # Set when the simulator emits a substantive final reply glued to a stop
        # token in the same message and the rule says ``deliver``. On the next
        # user turn the runner terminates before calling the simulator so the
        # agent gets exactly one more turn to act on the delivered reply, then
        # the loop ends with ``USER_STOP``.
        self._pending_user_stop: UserStop | None = None

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
            # What the loop returned, or ``None`` where it never returned at
            # all. The post-conditions below check what a loop produced, so a
            # trial that died before or inside the loop has nothing for them to
            # read and must not be reported as a loop that broke its contract.
            outcome: LoopOutcome | None = None

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
                session_id=self._session_id("user"),
            )

            # Deferred import: the plugin registry pulls the conductor
            # protocol, which pulls this runner module — an eager top-level
            # import would loop.
            from tolokaforge.core.plugin_registry import (
                TurnPolicyContext,
                load_agent_loop,
                load_turn_policy,
            )

            # Resolved outside the ``except Exception`` below: an unregistered
            # name is a config fault, and reporting it as this trial's status
            # would price it as a scored agent failure.
            loop_factory = load_agent_loop(self.agent_loop)

            try:
                policy = load_turn_policy(self.interaction_mode)(
                    TurnPolicyContext(user_simulator=self.user_simulator)
                )
                task_config = TaskConfig(
                    task_id=self.task_id,
                    description="",
                    interaction_mode=self.interaction_mode,
                )
                self._seed_first_user_message(task_config, policy, initial_user_message)

                agent_metrics_sink = _TrialMetricsSink(
                    self.metrics,
                    events=self._events,
                    trial_id=trial_id,
                )
                capabilities = self.agent_client.capabilities
                summarize_policy: SummarizePolicy | None = None
                if (
                    capabilities.max_context_tokens is not None
                    and capabilities.context_watermark is not None
                ):
                    summarize_policy = LLMSummarizer(self.agent_client, agent_metrics_sink)
                loop = loop_factory(
                    AgentLoopContext(
                        llm_client=self.agent_client,
                        tool_executor=self.tool_executor,
                        tool_schemas=self.tool_schemas,
                        validation_schemas_by_tool=self.agent_client.sanitize_tools_for_execution(
                            self.tool_schemas
                        ),
                        tool_output_max_chars_by_tool=self.tool_output_max_chars_by_tool,
                        config=LoopConfig(
                            max_turns=self.max_turns,
                            episode_timeout_s=self.episode_timeout_s,
                            empty_retry_count=capabilities.empty_retry_count,
                            reasoning_stall_retry_count=capabilities.reasoning_stall_retry_count,
                            reasoning_stall_turn_limit=capabilities.reasoning_stall_turn_limit,
                            output_length_retry_count=capabilities.output_length_retry_count,
                            parser_error_retry_count=capabilities.parser_error_retry_count,
                            tool_output_max_chars=capabilities.tool_output_max_chars,
                            max_context_tokens=capabilities.max_context_tokens,
                            context_watermark=capabilities.context_watermark,
                            summarize_policy=summarize_policy,
                        ),
                        metrics=agent_metrics_sink,
                        should_terminate=self._agent_termination,
                        user_turn=lambda messages: self._policy_user_turn(policy, messages),
                        recorder=self.tool_call_recorder,
                        call_ids=self._call_ids,
                        request_limiter=self.request_limiter,
                        normalize_tool_arguments=self._normalize_tool_arguments,
                        classify_error=self.agent_client.classify_loop_error,
                        logger=self.logger,
                        call_observation=LLMCallObservation(
                            events=self._events,
                            trial_id=trial_id,
                            role="agent",
                            probe_stats=self._probe_stats,
                            session_id=self._session_id("agent"),
                        ),
                        observer=self._loop_observer,
                        agent_view=agent_view if self._user_tool_turns.isolated else None,
                    )
                )
                outcome = loop.run(system_prompt, self.messages, self.start_time)

                status = outcome.status
                termination_reason = outcome.termination_reason
                if outcome.captured_effective_system_prompt is not None:
                    self._effective_system_prompt = outcome.captured_effective_system_prompt
                    self._effective_system_prompt_captured = True

            except Exception as e:
                # Catch-all for initialization errors (first-user-message generation).
                # The simulator's opening tool calls run here, before the loop and
                # its classifier, so the one reason a tool call can end the trial
                # under is named here too.
                #
                # Typed provider faults (API timeout, rate limit, …) that reach
                # here from the opening ``client.completion`` are the same
                # class the loop's ``classify_loop_error`` already routes to
                # ``EXCLUDED_TYPED_REASONS``. Consulting the classifier here
                # avoids pricing "one 429 on the opening generation" as a
                # scored agent failure when "the identical 429 one turn later"
                # is excluded. ``TrialNotRegisteredError`` beats the classifier
                # (it is the "trial-lost" signal, unrelated to provider health).
                status = TrialStatus.ERROR
                if isinstance(e, TrialNotRegisteredError):
                    termination_reason = TerminationReason.TRIAL_LOST
                else:
                    # ``classify_loop_error`` maps typed provider faults
                    # (API_TIMEOUT, RATE_LIMIT) to reasons in
                    # ``EXCLUDED_TYPED_REASONS`` and everything else back to
                    # ``ERROR``. Reads them here so a 429 on the opening
                    # generation and one turn later are classified alike.
                    termination_reason = self.agent_client.classify_loop_error(e).reason
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

            if outcome is not None:
                termination_reason = self._audit_loop_postconditions(outcome, termination_reason)

            return self._finalise(
                status=status, termination_reason=termination_reason, start_ts=start_ts
            )

    def run_harness(
        self,
        *,
        tool_name: str,
        command: str,
        instruction: str,
        timeout_s: float,
        harness: str = "",
        usage_log_container_path: str | None = None,
    ) -> Trajectory:
        """Run the trial as a single invocation of a coding-harness CLI.

        A harness CLI owns its own planning loop inside the container, so the
        engine's turn loop would be a second agent stacked on the first. The
        trial is one tool call instead: no LLM generation, no user turn, no
        :class:`~tolokaforge.core.loop.AgentLoop`. The trajectory records
        *instruction* as the user message and the CLI's output as the agent's
        single reply.

        Args:
            tool_name: Tool the command runs through — the task's sole agent
                tool, resolved by the caller.
            command: Shell command that starts the CLI against *instruction*.
                Built by the adapter, which owns every CLI's argv.
            instruction: The task text handed to the CLI, recorded as the
                trial's user message.
            timeout_s: The harness deadline, for the engine's own accounting.
                Must equal the target tool's registered ``timeout_s`` — the
                budget the runner resolves and enforces is the one the tool
                declares, so no per-call value rides the wire; passing the
                same number here keeps the engine-side overrun warning honest.
            harness: Name of the CLI the command starts, used only to pick a
                parser for the totals it prints. Empty, unrecognised, or a CLI
                that prints no totals all leave the trial's turn / token / cost
                accounting exactly as a single tool call produces it.
            usage_log_container_path: Path *inside the trial container* of the
                NDJSON usage records a request middleware wrote for this trial,
                for a CLI that prints no token counts of its own. Read back
                through *tool_name* the moment the CLI's exec returns, while
                the container is still up. ``None`` — and a file the proxy
                never wrote — leave the token accounting to what the CLI
                printed.
        """
        trial_id = f"{self.task_id}:{self.trial_index}"
        with trial_id_scope(trial_id):
            self.logger = init_trial_logger(trial_id, self.verbose, self.strict)
            self.logger.info(
                "Starting harness trial",
                task_id=self.task_id,
                trial_index=self.trial_index,
                tool_name=tool_name,
                timeout_s=timeout_s,
            )
            self.start_time = time.time()
            start_ts = datetime.now(tz=timezone.utc)
            self.messages.append(
                Message(
                    role=MessageRole.USER,
                    content=instruction,
                    ts=datetime.now(tz=timezone.utc),
                )
            )

            arguments = {"command": command}
            call_id = f"harness:{trial_id}"
            call_started = time.time()
            result = self.tool_executor.execute(
                tool_name,
                arguments,
                call_id=call_id,
            )
            output = resolve_tool_output(result)
            tool_status = resolve_tool_status(result)
            # Read the CLI's stdout, not the recorded output: a failed call
            # records its error text instead, and a CLI that billed for real
            # work before exiting non-zero still spent that money. Parsing the
            # raw stream keeps the trial's cost honest in that case, and an
            # empty stream simply reports nothing.
            self._harness_stdout_telemetry = parse_harness_stdout(harness, result.output or "")
            self.tool_call_recorder.record(
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                executor=ToolExecutorIdentity.AGENT,
                status=tool_status,
                output=output,
                latency_seconds=time.time() - call_started,
            )
            # After the agent's call is recorded, so the recorded latency is
            # the CLI's alone, and before anything can tear the stack down.
            if usage_log_container_path is not None:
                self._harness_usage_records = self._read_container_usage_records(
                    tool_name, usage_log_container_path
                )
            self.messages.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=output,
                    ts=datetime.now(tz=timezone.utc),
                )
            )

            refused = self._harness_requests_all_refused()
            if refused is not None:
                # The CLI ran, wrote a transcript and exited — but the provider
                # served none of its requests, so nothing it "did" was its own
                # work. Left as a completed trial this scores against an
                # untouched repository, and on these packs that is worth
                # 0.42-0.58 of partial credit: a dead agent reported as a weak
                # one. ERROR routes it to a synthesized grade instead.
                status = TrialStatus.ERROR
                termination_reason = TerminationReason.API_ERROR
                self.logger.error(
                    "Harness trial made no successful provider request; not scoring it",
                    requests=refused.requests,
                    statuses=list(refused.statuses),
                )
            elif tool_status is ToolExecutionStatus.SUCCESS:
                status = TrialStatus.COMPLETED
                termination_reason = TerminationReason.AGENT_DONE
            elif tool_status is ToolExecutionStatus.TIMEOUT:
                status = TrialStatus.TIMEOUT
                termination_reason = TerminationReason.TIMEOUT
                self.logger.warning("Harness CLI exceeded its deadline", timeout_s=timeout_s)
            else:
                status = TrialStatus.ERROR
                termination_reason = TerminationReason.ERROR
                self.logger.error("Harness CLI invocation failed", tool_status=tool_status.value)

            return self._finalise(
                status=status, termination_reason=termination_reason, start_ts=start_ts
            )

    def _harness_requests_all_refused(self) -> HarnessRequestOutcomes | None:
        """The trial's request outcomes when the provider served none of them.

        ``None`` whenever the question cannot be answered or the answer is no:
        the harness booted no proxy, the records were unreadable, the CLI
        called no provider, or at least one request was served. Only a
        positive count of requests, every one of them refused, is evidence —
        an absent measurement must not condemn a trial any more than it may
        excuse one.
        """
        records = self._harness_usage_records
        if records is None:
            return None
        outcomes = summarise_harness_requests(records)
        if outcomes is None or not outcomes.none_succeeded:
            return None
        return outcomes

    def _audit_loop_postconditions(
        self, outcome: LoopOutcome, termination_reason: TerminationReason | None
    ) -> TerminationReason | None:
        """Check what the loop returned against the obligations of its seam.

        Runs on every trial a loop returned from, whichever loop drove it. The
        obligations :class:`~tolokaforge.core.loop.AgentLoop` states are
        enforced by nothing at write time, and each one broken produces a
        plausible trajectory carrying a wrong number rather than an error —
        so they are checked here, on every run, not only under test.

        Returns the trial's termination reason, downgraded where a
        denominator-excluding one arrived with no typed evidence behind it.
        Nothing else changes the trial: the agent's work is finished by the
        time this runs, and
        :func:`~tolokaforge.core.failure_attribution.classify_trial_outcome`
        already classifies a trial grading cannot answer rather than dropping
        it, so discarding one here would destroy evidence the run keeps.

        Every finding is reported through
        :meth:`_report_postcondition_finding`, which logs at ERROR without
        ending the trial.
        """
        downgraded = self._reason_downgraded_without_typed_evidence(outcome, termination_reason)
        self._audit_call_id_reconciliation(downgraded)
        self._audit_metrics_sink_liveness()
        return downgraded

    def _report_postcondition_finding(self, message: str, **context: Any) -> None:
        """Log one post-condition finding at ERROR, and leave the trial standing.

        ``StructuredLogger.error`` raises under ``strict``, and these checks run
        after the agent's work is finished and before the trajectory is
        assembled: a raise here would take the whole trial with it, including
        the evidence the finding describes and the remaining checks. The record
        reaches the trial's log either way, so the finding is reported and the
        trial finalises — the "logged, not refused" every one of these checks
        is written to.
        """
        try:
            self.logger.error(message, **context)
        except RuntimeError:
            return

    def _reason_downgraded_without_typed_evidence(
        self, outcome: LoopOutcome, termination_reason: TerminationReason | None
    ) -> TerminationReason | None:
        """Keep a denominator-excluding reason only where typed evidence earned it.

        Every reason in
        :data:`~tolokaforge.core.failure_attribution.EXCLUDED_TYPED_REASONS`
        removes the trial from the measured denominator *and* produces no
        grade, so a loop free to emit one from nothing can delete its own
        failures from the run's results with nothing in the output to show it.
        The evidence rides the outcome as
        :attr:`~tolokaforge.core.loop.LoopOutcome.excluding_reason_evidence`; an
        outcome leaving it ``None`` is an outcome that claims the exclusion
        rather than earning it, and
        :data:`~tolokaforge.core.models.TerminationReason.ERROR` is the counted
        reason it becomes — ``HARNESS_ERROR`` rather than ``MEASURED``, because
        a loop that ends a trial on an unevidenced provider fault is a defect
        of ours and belongs in the denominator as one.

        Every loop is held to it, the built-in one included. A loop that routes
        its exceptions through ``context.classify_error`` is handed the evidence
        on the :class:`~tolokaforge.core.loop.TerminationDecision` it already
        copies the reason from, so the rule costs a conforming implementation
        one field rather than a typing judgement of its own.
        """
        if termination_reason not in EXCLUDED_TYPED_REASONS:
            return termination_reason
        if outcome.excluding_reason_evidence is not None:
            return termination_reason
        self._report_postcondition_finding(
            "The agent loop ended this trial on a reason that excludes it from the "
            "measured denominator, but carried no typed evidence for it — counting "
            "the trial instead",
            agent_loop=self.agent_loop,
            claimed_termination_reason=termination_reason.value,
            counted_as=TerminationReason.ERROR.value,
            remedy=(
                "carry TerminationDecision.excluding_reason_evidence from "
                "context.classify_error onto LoopOutcome.excluding_reason_evidence, "
                "or name the empty-completion observation behind the reason"
            ),
        )
        return TerminationReason.ERROR

    def _audit_call_id_reconciliation(self, termination_reason: TerminationReason | None) -> None:
        """The message view and the tool-call record must describe the same calls.

        :func:`~tolokaforge.core.grading.trace_timeline.build_trial_timeline`
        is where the two views are joined, and the only place the rule for
        joining them lives, so this runs that function rather than restating
        it. What the check buys over waiting for grading is *when* it speaks:
        here it names the loop that produced the disagreement, while the trial
        is still the subject.

        Logged, not refused. Grading raises on these same inputs and
        :func:`~tolokaforge.core.failure_attribution.classify_trial_outcome`
        reports the trial ``UNGRADEABLE`` — counted in ``measured_trials``,
        never a pass, and visible in the output — so the trial is already
        handled honestly. Refusing it here would instead discard a trial whose
        state-based checks may well still grade it.
        """
        recorded = self.tool_call_recorder.recorded
        declared = sum(len(message.tool_calls or []) for message in self.messages)
        if recorded and not self._has_conversation_turns():
            self._report_postcondition_finding(
                "Tool-call id reconciliation could not run: the loop recorded tool "
                "calls but appended no assistant or user turn to reconcile them "
                "against, so nothing declares the calls the record describes",
                agent_loop=self.agent_loop,
                recorded_tool_calls=len(recorded),
            )
            return
        try:
            build_trial_timeline(self.messages, recorded, termination_reason)
        except TimelineInconsistencyError as exc:
            self._report_postcondition_finding(
                "The loop's declared tool calls and its recorded ones do not "
                "reconcile, so this trial cannot be graded from its trajectory",
                agent_loop=self.agent_loop,
                declared_tool_calls=declared,
                recorded_tool_calls=len(recorded),
                detail=str(exc)[:_RECONCILIATION_DETAIL_CHARS],
                remedy=(
                    "key every call by context.call_ids.assign(<provider id>) and use "
                    "that one key on the assistant message, the recorder and the "
                    "tool executor"
                ),
            )

    def _has_conversation_turns(self) -> bool:
        """Whether the trial carries any turn the timeline reads as a declaration.

        ``role: system`` messages are harness annotations, so a trajectory of
        nothing but those declares no tool call.
        """
        return any(
            message.role in (MessageRole.ASSISTANT, MessageRole.USER) for message in self.messages
        )

    def _audit_metrics_sink_liveness(self) -> None:
        """Assistant turns with no recorded model call means the sink never fired.

        ``cost_usd`` accumulates in :meth:`_AgentMetricsSink.record_generation`
        and nowhere else, so a loop that generates without feeding the sink it
        was handed leaves this trial's cost at zero however much the generation
        spent — and the run's budget cap, which sums those per-trial figures,
        can never fire.
        :meth:`Orchestrator._refuse_an_unenforceable_cost_limit` does not cover
        it: that refusal asks whether the pricing table can price the run's
        models, not whether anything is reporting usage to price.

        Logged, not refused. The spend has already happened, so dropping the
        trial recovers neither the money nor the cap, and a trial whose
        conversation is intact still grades.
        """
        # The agent's opening line (``first_agent_message``) is recorded, not
        # generated, so it is no turn a model call should have paid for.
        assistant_turns = self._agent_generations(self.messages)
        if not assistant_turns or self.metrics.api_calls:
            return
        self._report_postcondition_finding(
            "The agent loop produced assistant turns without recording a single model "
            "call, so this trial reports no usage and no cost and cannot be held to a "
            "budget cap",
            agent_loop=self.agent_loop,
            assistant_turns=assistant_turns,
            api_calls=self.metrics.api_calls,
            cost_usd=self.metrics.cost_usd,
            remedy="call context.metrics.record_generation(result) for every generation",
        )

    def _finalise(
        self,
        *,
        status: TrialStatus,
        termination_reason: TerminationReason | None,
        start_ts: datetime,
    ) -> Trajectory:
        """Close out metrics and assemble the trial's :class:`Trajectory`.

        Shared by every way a trial can be driven, so the recorded shape does
        not depend on which one drove it.
        """
        end_ts = datetime.now(tz=timezone.utc)
        self.metrics.latency_total_s = time.time() - self.start_time
        self.metrics.turns = self._agent_generations(self.messages)
        self._apply_probe_stats()
        self._apply_harness_telemetry()
        self._apply_cost_rollup()

        recorded_calls = self.tool_call_recorder.recorded
        # Both describe the agent's tool use — the scoping stuck detection
        # and ``tool_expectations`` already apply. The guard is the agent
        # slice too: a trial whose only calls were the user's would divide
        # by zero here, and its true agent count is the sink's own 0.
        agent_calls = self.tool_call_recorder.recorded_for(ToolExecutorIdentity.AGENT)
        if agent_calls:
            success_count = sum(
                1 for call in agent_calls if call.status is ToolExecutionStatus.SUCCESS
            )
            self.metrics.tool_success_rate = success_count / len(agent_calls)
            self.metrics.tool_calls = len(agent_calls)

        self.logger.info(
            "Trial execution finished",
            status=status.value,
            turns=self.metrics.turns,
            tool_calls=self.metrics.tool_calls,
            latency_s=self.metrics.latency_total_s,
        )

        # LLM mode populates the simulator prompt on every reply; scripted mode
        # leaves it None. Re-read at every trial-end so a follow-up reply that
        # revised the prompt lands its latest version. The isinstance guard
        # keeps a non-string (a MagicMock in a test) off the Trajectory rather
        # than silently coercing it — AGENTS.md rule #1.
        sim_prompt = getattr(self.user_simulator, "last_system_prompt", None)
        if isinstance(sim_prompt, str) and sim_prompt:
            self._user_system_prompt_captured = sim_prompt

        # Both system prompts are read off the runner via the
        # :attr:`effective_system_prompt` / :attr:`user_system_prompt`
        # properties and persisted by the orchestrator into ``prompts.yaml``.
        return Trajectory(
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

    def _apply_harness_telemetry(self) -> None:
        """Replace the single-tool-call accounting with the harness's own.

        Two taps can report a harness trial's tokens, and **the CLI's printed
        totals win**: the wire records fill in only where the CLI reported no
        token counts at all.

        The other order is defensible — arguably better — for *spend*. A
        request middleware sits on the traffic, so it counts the retries a
        CLI's end-of-run summary may quietly fold away, which makes it the
        truer figure for what a run cost. It is not preferred here because the
        two sources cannot both appear: ``kimi-code`` is the only shipped
        harness routed through a proxy, and it is also the only one that prints
        no usage. So the precedence below never actually arbitrates, and
        reversing it would change nothing observable while a merge policy for
        an impossible overlap would be untestable code.
        """
        self._apply_harness_stdout_telemetry()
        self._apply_harness_wire_usage()

    def _apply_harness_stdout_telemetry(self) -> None:
        """Replace the single-tool-call accounting with what the CLI reported.

        No-op on every trial except a harness trial whose CLI printed its own
        totals, so a run that reaches none of them carries the numbers it
        always did.

        ``turns`` and ``usage`` are *replaced*, not added to: on this path the
        engine issued no LLM request, so its own figures are the artefacts of
        driving the CLI as one tool call — a turn count of 1 and an empty usage
        block — and summing them with the CLI's would double-count nothing
        while leaving the total wrong by one turn. ``tool_calls`` keeps the
        engine's count: one ``docker exec`` is what the engine executed, and
        the CLI's own tool use is a different quantity this record does not
        carry.

        Each field is applied only where the CLI actually reported it, because
        the CLIs report different subsets: ``claude-code`` gives turns, tokens
        and cost; ``codex`` gives turns and tokens but no cost; ``kimi-code``
        gives turns alone. Writing an unreported field would turn "the CLI
        never said" into "the CLI said zero".

        ``cost_usd`` is the engine's own price for the tokens the CLI reported
        — see :meth:`_price_harness_tokens` — so a cross-mode comparison
        prices every arm by one authority rather than one vendor's billing
        against another's. The CLI's own figure is preserved beside it as
        ``harness_reported_cost_usd``, and is used as ``cost_usd`` only where
        our own table cannot price the model. Precedence, highest first:

        1. our price for the reported tokens,
        2. the cost the CLI reported,
        3. whatever ``cost_usd`` already held — ``None`` on this path, since
           the engine issued no request to cost anything.
        """
        telemetry = self._harness_stdout_telemetry
        if telemetry is None:
            return
        self.metrics.harness_stdout_dialect = telemetry.dialect
        self.metrics.turns = telemetry.turns
        # Recorded wherever the CLI said anything, as the cross-check on our
        # own figure below. ``None`` where it said nothing.
        self.metrics.harness_reported_cost_usd = telemetry.cost_usd
        cost_usd = telemetry.cost_usd
        if telemetry.has_token_counts:
            self.metrics.usage = Usage(
                prompt_tokens=telemetry.prompt_tokens or 0,
                completion_tokens=telemetry.completion_tokens or 0,
                reasoning_tokens=telemetry.reasoning_tokens or 0,
                cache_read_input_tokens=telemetry.cache_read_input_tokens or 0,
                cache_creation_input_tokens=telemetry.cache_creation_input_tokens or 0,
            )
            priced = self._price_harness_tokens(self.metrics.usage)
            if priced is not None:
                cost_usd = priced
                self._warn_on_vendor_cost_divergence(priced, telemetry.cost_usd)
        if cost_usd is not None:
            self.metrics.cost_usd = cost_usd

    def _warn_on_vendor_cost_divergence(self, ours: float, theirs: float | None) -> None:
        """Compare our price for this trial against the CLI's own figure.

        ``harness_reported_cost_usd`` has been recorded as "the cross-check"
        since the field was added, and nothing ever compared the two. Where a
        CLI bills itself, that comparison is a free and continuous audit of the
        whole pricing path — the table, the token basis and the arithmetic —
        against a number the vendor computed independently. It has been exact
        when both sides were right: a live ``claude-code`` trial agreed to
        fifteen significant figures.

        A warning rather than a refusal, because the two figures are allowed to
        differ: a CLI may round, may price a retry we did not see, or may quote
        a list rate against our negotiated one. What it must not do is differ
        by a multiple, which is what a stale or wrong rate looks like.
        """
        if theirs is None or theirs <= 0:
            return
        ratio = ours / theirs
        if 1 / _VENDOR_COST_TOLERANCE <= ratio <= _VENDOR_COST_TOLERANCE:
            return
        self.logger.warning(
            "Our price for this trial disagrees with the CLI's own figure",
            ours_usd=ours,
            cli_reported_usd=theirs,
            ratio=round(ratio, 3),
            model=self.agent_client.model_name,
            remedy="check the pricing table's rates for this model against the provider",
        )

    def _read_container_usage_records(self, tool_name: str, container_path: str) -> str | None:
        """Read the wire-usage records out of the trial container via *tool_name*.

        The container is the only place those records exist. The runtime
        bind-mounts the agent service's log directory from the per-trial
        compose context — a temporary copy it deletes at teardown — so there
        is no host path to open instead, and the read has to happen while the
        container the CLI just ran in is still up.

        This is engine instrumentation, not agent action, so it stays out of
        the trial's own account of itself: nothing reaches
        :attr:`tool_call_recorder`, no message is appended, and ``tool_calls``
        remains the single ``exec`` the engine ran on the agent's behalf. The
        executor does forward it to the runner, which records every tool
        execution on its own side of the wire; a harness trial grades on the
        pack's verifier rather than on that record, and the trajectory the
        bundle is written from is the host-side one, so the extra entry
        reaches nothing a reader sees.

        Every way this can fail returns ``None`` and leaves the trial exactly
        as it would have been: records the proxy never wrote (``cat`` exits
        non-zero), an executor that raised, a container already gone. A
        telemetry read may not cost a trial its result, which is why the
        blanket ``except`` is right here and nowhere else in this class — but
        an absence that is never reported is an absence nobody can debug, so
        both arms log what happened.
        """
        try:
            result = self.tool_executor.execute(
                tool_name,
                {"command": f"cat -- {shlex.quote(container_path)}"},
                call_id=f"{_HARNESS_USAGE_READ_CALL_ID_PREFIX}{self.task_id}:{self.trial_index}",
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never fails a trial
            self.logger.info(
                "Harness usage records could not be read from the container",
                path=container_path,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        if resolve_tool_status(result) is not ToolExecutionStatus.SUCCESS:
            self.logger.info(
                "Harness usage records are absent from the container",
                path=container_path,
                detail=resolve_tool_output(result)[:_USAGE_READ_DETAIL_CHARS],
            )
            return None
        return result.output or None

    def _apply_harness_wire_usage(self) -> None:
        """Fold in the token usage a request middleware measured on the wire.

        No-op unless :meth:`run_harness` read records back out of the trial
        container, which it only attempts for a harness whose adapter named a
        usage-log path — and only a harness declaring request middleware boots
        the proxy that writes one. Also a no-op when the CLI already reported
        token counts itself: see :meth:`_apply_harness_telemetry` for why
        stdout wins.

        The records are the provider's own ``usage`` blocks, so the totals go
        in on the same inclusive basis and through the same
        :meth:`_price_harness_tokens` the stdout path prices with — one
        pricing authority across every arm of a comparison. ``turns`` is left
        alone: a request is not a turn, and the CLI that prints no usage does
        print a transcript the turn count already came from.

        Absence is normal and silent — no proxy, no provider call, or a read
        that came back empty all leave the trial exactly as it was, and a
        zeroed usage block would instead claim it spent nothing. Damage is
        not silent: a proxy killed mid-append leaves a partial line, which
        makes the total a lower bound worth reporting, though never worth
        failing a trial over.
        """
        records = self._harness_usage_records
        if records is None:
            return
        stdout_telemetry = self._harness_stdout_telemetry
        if stdout_telemetry is not None and stdout_telemetry.has_token_counts:
            return

        wire = sum_harness_usage_records(records)
        if wire is None:
            return
        if wire.skipped_lines:
            self.logger.warning(
                "Skipped unreadable harness usage records",
                skipped_lines=wire.skipped_lines,
                records=wire.requests,
            )

        if not any(
            (
                wire.prompt_tokens,
                wire.completion_tokens,
                wire.cache_read_input_tokens,
                wire.reasoning_tokens,
            )
        ):
            # Records exist, so the CLI did reach a provider — but every count
            # in them is zero, which no real exchange produces. An upstream
            # that answers without populating usage (a gateway translating a
            # streamed response, say) is reporting nothing, not reporting
            # nothing spent, and recording it as the latter puts a $0.00 in a
            # cost comparison for a trial that ran.
            self.logger.warning(
                "Harness wire usage is entirely zero; leaving the trial unmeasured",
                records=wire.requests,
            )
            return

        self.metrics.harness_usage_source = MIDDLEWARE_PROXY_USAGE_SOURCE
        self.metrics.usage = Usage(
            prompt_tokens=wire.prompt_tokens,
            completion_tokens=wire.completion_tokens,
            reasoning_tokens=wire.reasoning_tokens,
            cache_read_input_tokens=wire.cache_read_input_tokens,
        )
        priced = self._price_harness_tokens(self.metrics.usage)
        if priced is not None:
            self.metrics.cost_usd = priced

    def _price_harness_tokens(self, usage: Usage) -> float | None:
        """Our own price for *usage*, or ``None`` when the model is unpriceable.

        The agent model is read off :attr:`agent_client` — the same
        ``model_name`` the engine's own cost ladder prices its calls with
        (``LLMClient.generate``), so the two arms of a cross-mode comparison
        resolve to one pricing row and cannot diverge on how the key was
        spelled. The client itself is never called on this path: a harness CLI
        owns its planning loop, and only its model identity is needed here.

        ``None`` means the pricing table carries no row for the model, which
        the caller must not turn into a zero — :func:`estimate_cost` returns
        ``None`` for exactly that reason, and the caller leaves ``cost_usd``
        alone: the CLI's own figure where it printed one, and nothing where
        neither the table nor the CLI can price the trial.

        ``prompt_tokens`` is passed as-is: it is already the litellm-normalised
        prompt total that :func:`estimate_cost` documents, cache reads and
        writes included (each stdout parser converts to that basis), and
        adding the cache counters to it here would bill them twice.
        ``reasoning_tokens`` is deliberately **not** passed for the same
        reason: every shipped dialect counts reasoning inside its output
        total, while :func:`estimate_cost` adds the argument to
        ``output_tokens``.

        Flags the trial when the row priced observed cache tokens at its
        input rate. The preflight check warns before the run that a model
        resolves to a row without cache rates, but it cannot know whether the
        trial will actually use the cache; this is the after-the-fact half of
        the same signal, and on a cache-heavy harness trial the difference is
        a multiple rather than a rounding.
        """
        model = self.agent_client.model_name
        resolution = resolve_pricing(model)
        self.metrics.pricing_key = resolution.resolved_key
        if resolution.priced:
            self.metrics.pricing_basis = {
                rate: float(value)
                for rate, value in (MODEL_PRICING.get(resolution.resolved_key) or {}).items()
                if isinstance(value, (int, float))
            }
        cost = estimate_cost(
            model=model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
        )
        if cost is not None:
            # Each missing rate against its own counter, not both against
            # either: a row lacking only `cache_write` misprices nothing on a
            # trial that wrote no cache, and flagging it there would teach a
            # reader to ignore the flag where it does mean something.
            observed = {
                "cache_read": usage.cache_read_input_tokens,
                "cache_write": usage.cache_creation_input_tokens,
            }
            missing = resolve_pricing(model).missing_cache_rates
            if any(observed.get(rate) for rate in missing):
                self.metrics.cost_cache_rate_fallback = True
        return cost

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

    def _apply_cost_rollup(self) -> None:
        """Derive the per-role cost / token breakdown onto :class:`Metrics`.

        Groups ``usage.calls`` by ``(role, model)`` — summing each call's cost
        and tokens — then rolls the pairs up per role. Derived, never
        independently accumulated, so it cannot double-count.

        A coding-harness trial issues no per-call records yet carries
        ``cost_usd > 0`` (the engine's price for the CLI-reported tokens), so a
        call-derived rollup alone would under-count it. The reconciliation step
        attributes the residual ``cost_usd − Σ(call costs)`` — and the matching
        flat-token residual — to the agent role at the agent model, which makes
        ``sum(cost_by_role[*].cost_usd) == cost_usd`` hold on every trial. On an
        LLM-loop trial that residual is float noise and no row is emitted; a
        ``None`` ``cost_usd`` leaves the rollup purely call-derived.
        """
        token_fields = (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        cost_by_pair: dict[tuple[str, str | None], float] = {}
        tokens_by_pair: dict[tuple[str, str | None], dict[str, int]] = {}

        def _ensure(key: tuple[str, str | None]) -> None:
            cost_by_pair.setdefault(key, 0.0)
            tokens_by_pair.setdefault(key, dict.fromkeys(token_fields, 0))

        calls_cost = 0.0
        calls_tokens = dict.fromkeys(token_fields, 0)
        for call in self.metrics.usage.calls:
            key = (call.role, call.model)
            _ensure(key)
            if call.cost_usd is not None:
                cost_by_pair[key] += call.cost_usd
                calls_cost += call.cost_usd
            for field in token_fields:
                value = getattr(call, field)
                tokens_by_pair[key][field] += value
                calls_tokens[field] += value

        cost_usd = self.metrics.cost_usd
        residual = None if cost_usd is None else cost_usd - calls_cost
        if residual is not None and abs(residual) > _COST_ROLLUP_RESIDUAL_TOLERANCE_USD:
            # The residual is agent spend the per-call records did not carry (a
            # coding-harness trial prices CLI-reported tokens with no per-call
            # records). Name it at the agent model where the client exposes one;
            # an Actor whose contract omits ``model_name`` (post ADR-0051 the
            # agent is a Protocol) attributes the residual to the agent role with
            # an unknown model rather than raising.
            agent_model = getattr(self.agent_client, "model_name", None)
            if not isinstance(agent_model, str):
                agent_model = None
            key = ("agent", agent_model)
            _ensure(key)
            cost_by_pair[key] += residual
            for field in token_fields:
                residual_tokens = getattr(self.metrics.usage, field) - calls_tokens[field]
                tokens_by_pair[key][field] += residual_tokens

        self.metrics.cost_by_role_model = [
            CostByRoleModelMetrics(
                role=role,
                model=model,
                cost_usd=cost_by_pair[(role, model)],
                **tokens_by_pair[(role, model)],
            )
            for role, model in sorted(cost_by_pair, key=lambda pair: (pair[0], pair[1] or ""))
        ]

        role_cost: dict[str, float] = {}
        role_tokens: dict[str, dict[str, int]] = {}
        for (role, _model), cost in cost_by_pair.items():
            role_cost[role] = role_cost.get(role, 0.0) + cost
            bucket = role_tokens.setdefault(role, dict.fromkeys(token_fields, 0))
            for field in token_fields:
                bucket[field] += tokens_by_pair[(role, _model)][field]

        self.metrics.cost_by_role = [
            CostByRoleMetrics(role=role, cost_usd=role_cost[role], **role_tokens[role])
            for role in sorted(role_cost)
        ]

    def _seed_first_user_message(
        self,
        task_config: TaskConfig,
        policy: TurnPolicy,
        initial_user_message: str,
    ) -> None:
        """Determine and append the first user message before the loop runs.

        A task's ``first_agent_message`` is appended ahead of it, as the agent's
        first turn: the simulator then answers that line rather than the built-in
        greeting, and the agent reads it back as its own.

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

        if self._first_agent_message is not None:
            self._opening_line = Message(
                role=MessageRole.ASSISTANT,
                content=self._first_agent_message,
                ts=datetime.now(tz=timezone.utc),
            )
            self.messages.append(self._opening_line)
            self.logger.info("Agent opening delivered", chars=len(self._first_agent_message))

        first_user_calls: list[ToolCall] = []
        if decision.first_user_message is not None:
            first_user_text = decision.first_user_message
            self._first_user_message_source = FirstUserMessageSource.PINNED
        elif decision.bootstrap_via_simulator:
            first_user_text, first_user_calls = self._bootstrap_via_simulator()
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
                tool_calls=first_user_calls if first_user_calls else None,
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

    def _record_actor_spend(self, result: GenerationResult) -> None:
        """Fold a non-agent actor's usage/cost/generation-ids into the trial
        :class:`Metrics` via a :class:`_TrialMetricsSink`.

        Guarded on non-empty ``usage.calls``: scripted / mock replies carry an
        empty ``calls`` tuple, so the guard makes the fold a no-op for them and
        only real LLM-backed actor spend reaches ``api_calls`` / ``cost_usd`` /
        ``usage`` / ``openrouter_generation_ids``. Reusing the trial sink also
        fires ``trial_progress`` so the live cost total tracks the final
        ``metrics.yaml``.
        """
        if not result.usage.calls:
            return
        _TrialMetricsSink(
            self.metrics,
            events=self._events,
            trial_id=f"{self.task_id}:{self.trial_index}",
        ).record_generation(result)

    def _bootstrap_via_simulator(self) -> tuple[str, list[ToolCall]]:
        """Synthesise turn 0 by dispatching the user simulator against the agent's
        opening line, or a canned agent greeting when the task declared none.
        Retries on rate limits only.

        Returns the opening message text with any tool results inlined, and the
        calls that produced them. Only the tool-call half of a user turn is
        shared with :meth:`_dispatch_user_actor`: turn 0 does not read stop
        tokens, so a token in the opening is seeded literally rather than
        terminating the trial before the agent has spoken.

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
        greeting_context = self._greeting_context()
        if self._user_tool_turns.isolated:
            return self._bootstrap_isolated(self.user_simulator, greeting_context)
        init_attempts = 1 if self._rate_limit_probe_active else 4
        for attempt in range(1, init_attempts + 1):
            try:
                first_user_result = self.user_simulator.reply(
                    greeting_context + self.messages, observation=self._user_observation
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
                # Read before inlining, so a reply that is only a tool call
                # refuses rather than opening with the tool's own output.
                #
                # ``filler_substituted`` covers the tool-call-only opening the
                # LLM client rewrites in-closure ("Let me check that."): the
                # text is no longer empty by the time it reaches here, but the
                # substituted filler is the engine's own words, not the task
                # statement the agent must be graded against. Both empty and
                # filler-only openings fail the same way.
                if first_user_result.filler_substituted or not first_user_result.text.strip():
                    raise RuntimeError(
                        "User simulator bootstrap produced an empty first message; "
                        "a blank opening cannot seed the conversation."
                    )
                self.logger.debug("User simulator generated first message")
                self._record_actor_spend(first_user_result)
                return self._run_user_tool_calls(
                    first_user_result.text, first_user_result.tool_calls
                )
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

    def _agent_generations(self, messages: list[Message]) -> int:
        """The assistant messages in *messages* the agent generated: all but the opening line."""
        return sum(
            1
            for message in messages
            if message.role == MessageRole.ASSISTANT and message is not self._opening_line
        )

    def _greeting_context(self) -> list[Message]:
        """What leads the transcript in the simulator's turn-0 context.

        Nothing, when the task declared a ``first_agent_message``: the transcript
        already opens with it. Otherwise the canned :data:`SIMULATOR_GREETING`,
        which the simulator's view prepends to every later context as well.
        """
        if self._first_agent_message is not None:
            return []
        return [
            Message(
                role=MessageRole.ASSISTANT,
                content=SIMULATOR_GREETING,
                ts=datetime.now(tz=timezone.utc),
            )
        ]

    def _bootstrap_isolated(
        self, simulator: UserSimulator, greeting_context: list[Message]
    ) -> tuple[str, list[ToolCall]]:
        """Turn 0 under ``isolated`` tool turns: tool steps first, then the opening.

        The steps are recorded into the transcript ahead of the opening message,
        in the order they happened, and each next ask sees them after the
        greeting. The rate-limit retry wraps each ask on its own, so a step whose
        calls already ran is never run again.

        More steps than the rule allows refuse the trial rather than end it with
        ``USER_TOOL_LOOP_LIMIT``: the agent has not spoken yet, so a simulator
        that loops before its opening is a defect of the harness's own actor,
        not an outcome the agent could be graded on.

        Like the rest of turn 0 this runs before the loop, so no episode-timeout
        check interrupts it; ``max_tool_steps`` bounds it, its wall time is spent
        from the episode budget, and the loop's first turn ends a trial whose
        opening ran past that budget with ``TIMEOUT``.
        """
        opening = self._bootstrap_reply(simulator, greeting_context + self.messages)
        steps = 0
        while opening.tool_calls:
            steps += 1
            if steps > self._user_tool_turns.max_steps:
                raise RuntimeError(
                    f"User simulator took more than {self._user_tool_turns.max_steps} tool "
                    "step(s) before its opening message; the dialogue cannot start. The "
                    f"next step called {_call_names(opening.tool_calls)}."
                )
            self._record_user_tool_step(self.messages, opening)
            opening = self._bootstrap_reply(simulator, greeting_context + self.messages)
        if not opening.text.strip():
            raise RuntimeError(
                "User simulator bootstrap produced an empty first message; "
                "a blank opening cannot seed the conversation."
            )
        self.logger.debug("User simulator generated first message", tool_steps=steps)
        return opening.text, []

    def _bootstrap_reply(
        self, simulator: UserSimulator, context: list[Message]
    ) -> GenerationResult:
        """One turn-0 ask of the simulator, retried on rate limits only.

        The same budget :meth:`_bootstrap_via_simulator` applies to its single
        ask, spent per ask here. Its probe-mode collapse to one attempt is kept in
        step with that method, though a built trial never takes it: the conductor
        refuses isolated tool turns under probe mode.
        """
        init_attempts = 1 if self._rate_limit_probe_active else 4
        for attempt in range(1, init_attempts + 1):
            try:
                result = simulator.reply(context, observation=self._user_observation)
            except UserReplyRefused as exc:
                self._record_user_reply_guard(
                    message_index=len(self.messages),
                    outcome=UserReplyOutcome.REFUSED,
                    rejected=exc.rejected,
                )
                raise
            except Exception as exc:
                self._wait_to_retry_the_opening(exc, attempt, init_attempts)
                continue
            self._record_user_reply_guard(
                message_index=len(self.messages),
                outcome=UserReplyOutcome.DELIVERED,
                rejected=result.guard_rejections,
            )
            return result
        raise RuntimeError("Failed to generate initial user message")

    def _wait_to_retry_the_opening(self, exc: Exception, attempt: int, init_attempts: int) -> None:
        """Back off before the next turn-0 ask, or re-raise *exc*.

        Only a rate limit is retried, and only while attempts remain.
        """
        if not self._is_rate_limit_error(exc) or attempt >= init_attempts:
            raise exc
        wait_s = min(2**attempt, 12)
        self.logger.warning(
            "Initial user generation rate-limited; retrying",
            attempt=attempt,
            max_attempts=init_attempts,
            wait_s=wait_s,
            error=str(exc),
        )
        time.sleep(wait_s)

    def _agent_termination(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None:
        """Agent termination policy: stuck detection and the completion signal.

        The agent's prose is never read for a completion signal — who speaks
        next, and whether anyone can, is the :class:`TurnPolicy`'s decision. A
        *tool call* is read, and only a call to a tool the registry declares a
        completion tool and the run actually enabled. That is the same shape the
        rubric judge terminates on (``submit_report``), moved to the agent side:
        the terminal act is an action the model takes, not a sentence it writes,
        so a model that never narrates can still end its own episode.

        Stuck sets ``metrics.stuck_detected`` as a side effect, which is why it
        lives here rather than in the policy. Stuck is checked first: an agent
        that has been repeating itself has already failed, and letting a
        ``submit`` on that turn overwrite the diagnosis would hide it.
        """
        del turn, messages
        if self.stuck_detector and self.stuck_detector.is_stuck(
            self.tool_call_recorder.recorded_for(ToolExecutorIdentity.AGENT)
        ):
            self.metrics.stuck_detected = True
            self.logger.warning("Stuck condition detected")
            return TerminationDecision(
                reason=TerminationReason.STUCK_DETECTED,
                system_message="Stuck condition detected. Dialogue terminated.",
            )

        if self._completion_tools:
            for call in result.tool_calls:
                if call.name in self._completion_tools:
                    self.logger.info("Agent signalled completion", tool=call.name)
                    return TerminationDecision(
                        reason=TerminationReason.AGENT_SUBMITTED,
                        system_message=(
                            f"Agent called {call.name}. Episode terminated at the "
                            "agent's own signal."
                        ),
                        status=TrialStatus.COMPLETED,
                    )

        return None

    def _session_id(self, role: LLMCallRole) -> str | None:
        return None if self._trace_id is None else conversation_session_id(self._trace_id, role)

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
            turn_index=self._agent_generations(messages),
        )
        decision = policy.next_actor(state)
        if decision is None:
            return UserTurnResult()
        if isinstance(decision, TerminationDecision):
            return UserTurnResult(termination=decision)
        return self._dispatch_user_actor(decision.actor, messages)

    def _dispatch_user_actor(self, actor: Actor, messages: list[Message]) -> UserTurnResult:
        """Run one user actor turn: reply, stop-token detection, user tools.

        Under ``isolated`` tool turns a reply that calls tools is a tool step,
        not the turn's reply (see :meth:`_run_user_tool_steps`). A stop token
        inside a step is not a stop, since a step is addressed to the
        environment.

        The trial's :class:`UserStopRule` names the tokens; the earliest one in the
        reply decides. Stop-token handling has three shapes:

        * Under ``stop_with_text: end``, any reply carrying a token — record the
          reply as the simulator wrote it, token included, as the dialogue's last
          USER message and terminate in the same turn, so the agent never answers
          it (:meth:`_end_on_user_stop`).
        * Under ``stop_with_text: deliver``, a bare token (or the token with only
          whitespace before it) — terminate immediately with ``USER_STOP``.
        * Under ``stop_with_text: deliver``, substantive text before the token —
          deliver the pre-token text as a normal USER message, hold the stop
          pending, and terminate on the following user turn. Guarantees the agent
          sees the final reply (e.g. a backstory-mandated verbal decline) before
          the dialogue ends.
        """
        if self._pending_user_stop is not None:
            stop = self._pending_user_stop
            self.logger.info(f"User signaled completion ({stop.token} after final reply)")
            self._pending_user_stop = None
            return UserTurnResult(
                termination=self._user_stop_decision(stop, after_final_reply=True)
            )

        user_result = self._reply_as_user(actor, messages)
        if self._user_tool_turns.isolated:
            outcome = self._run_user_tool_steps(actor, messages, user_result)
            if isinstance(outcome, TerminationDecision):
                return UserTurnResult(termination=outcome)
            user_result = outcome

        stop = self._user_stop.find(user_result.text)
        if stop is not None and self._user_stop.with_text == "end":
            return self._end_on_user_stop(stop, user_result)
        if stop is not None and stop.dropped:
            self.logger.info(
                f"Dropped the text after {stop.token} in the user reply",
                dropped_chars=len(stop.dropped),
            )
        if stop is not None and not stop.text:
            self.logger.info(
                f"User signaled completion ({stop.token})",
                dropped_tool_calls=len(user_result.tool_calls or []),
            )
            return UserTurnResult(termination=self._user_stop_decision(stop))
        if stop is not None:
            user_result.text = stop.text

        user_message_text, executed_calls = self._run_user_tool_calls(
            user_result.text, user_result.tool_calls
        )
        message = Message(
            role=MessageRole.USER,
            content=user_message_text,
            tool_calls=executed_calls if executed_calls else None,
            ts=datetime.now(tz=timezone.utc),
        )

        if stop is None:
            return UserTurnResult(message=message)
        self.logger.info(
            f"User sent final reply with {stop.token} — delivering reply, stop pending"
        )
        self._pending_user_stop = stop
        return UserTurnResult(message=message)

    def _end_on_user_stop(self, stop: UserStop, user_result: GenerationResult) -> UserTurnResult:
        """``stop_with_text: end``: record the stop reply as written and end the dialogue.

        The reply becomes the dialogue's last USER message exactly as the simulator
        wrote it — what precedes the token, the token and whatever follows it — so
        a transcript keeps the stop reply verbatim, and the dialogue ends in the
        same turn, so the agent never answers it. A bare token is recorded the
        same way. The calls on a reply with text run and are recorded on the
        message first, their results appended after the text as on any user
        turn that calls tools; a bare token's calls are not run.
        """
        calls = user_result.tool_calls if stop.text else []
        if not stop.text:
            self.logger.info(
                f"User signaled completion ({stop.token})",
                dropped_tool_calls=len(user_result.tool_calls or []),
            )
        user_message_text, executed_calls = self._run_user_tool_calls(user_result.text, calls)
        message = Message(
            role=MessageRole.USER,
            content=user_message_text,
            tool_calls=executed_calls if executed_calls else None,
            ts=datetime.now(tz=timezone.utc),
        )
        self.logger.info(
            f"User sent final reply with {stop.token} — recording reply, dialogue ends"
        )
        return UserTurnResult(message=message, termination=self._user_stop_decision(stop))

    def _reply_as_user(self, actor: Actor, messages: list[Message]) -> GenerationResult:
        """Ask *actor* for its reply to *messages*, recording what the reply guard spent.

        The guard event's index is read before the dispatch: it is the position
        the reply's USER message will occupy, and on a bare stop token under
        ``deliver`` or a refusal the loop puts its own SYSTEM message there instead.
        """
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
        self._record_actor_spend(user_result)
        return user_result

    def _run_user_tool_steps(
        self, actor: Actor, messages: list[Message], reply: GenerationResult
    ) -> GenerationResult | TerminationDecision:
        """Record *reply*'s tool steps until *actor* answers with text alone.

        Each step is recorded into *messages* — never the agent's wire — and the
        actor is asked again with its results in view. Returns the text reply, or
        the decision that ends the dialogue: a step past the rule's
        ``max_steps``, none of whose calls run, or the episode timeout, which is
        checked after every step.
        """
        steps = 0
        while reply.tool_calls:
            steps += 1
            if steps > self._user_tool_turns.max_steps:
                return self._user_tool_loop_limit_decision(reply.tool_calls)
            self._record_user_tool_step(messages, reply)
            timeout = episode_timeout_decision(self.start_time, self.episode_timeout_s, self.logger)
            if timeout is not None:
                return timeout
            reply = self._reply_as_user(actor, messages)
        return reply

    def _user_tool_loop_limit_decision(self, unrun: list[ToolCall]) -> TerminationDecision:
        """End the dialogue on a step past the limit, naming the calls it would have run.

        The unrun step is not recorded into the transcript, so the log line and the
        system message are where its calls stay visible.
        """
        limit = self._user_tool_turns.max_steps
        self.logger.warning(
            "User tool loop reached its step limit",
            max_steps=limit,
            unrun_calls=[{"name": call.name, "arguments": call.arguments} for call in unrun],
        )
        return TerminationDecision(
            reason=TerminationReason.USER_TOOL_LOOP_LIMIT,
            system_message=(
                f"User took {limit} tool step(s) without replying; the next step's "
                f"{len(unrun)} call(s), {_call_names(unrun)}, were not run. Dialogue terminated."
            ),
        )

    def _record_user_tool_step(self, messages: list[Message], result: GenerationResult) -> None:
        """Run and record one isolated tool step: the user's calls, then a TOOL message each.

        Appended to the recorded transcript as the step runs, so a failure part-way
        leaves no executed call the transcript does not show. Each call is keyed
        through the trial's assigner and recorded as the user's, like a ``shared``
        user call. Results are not capped: ``tool_output_max_chars`` bounds what
        the agent's model reads, and none of this reaches the agent.

        When the executor raises, the failing call and every call after it in the
        step still get a TOOL message before the exception propagates. The loop's
        API-error retry can bring the same simulator back to this transcript, and
        its provider refuses a request whose calls lack results.

        Raises:
            RuntimeError: the step carries calls and the trial has no user-side
                executor (see :meth:`_run_user_tool_calls`).
        """
        if self.user_tool_executor is None:
            raise RuntimeError(
                f"the user simulator emitted {len(result.tool_calls)} tool call(s) "
                f"({', '.join(call.name for call in result.tool_calls)}) and this trial has no "
                "user-side executor to run them. The trial is built with both or neither"
            )
        calls = [
            call.model_copy(update={"id": self._call_ids.assign(call.id)})
            for call in result.tool_calls
        ]
        messages.append(
            Message(
                role=MessageRole.USER,
                content=result.text,
                tool_calls=calls,
                reasoning=result.reasoning,
                ts=datetime.now(tz=timezone.utc),
            )
        )
        for position, call in enumerate(calls):
            tool_start = time.time()
            try:
                tool_result = self.user_tool_executor.execute(
                    call.name, call.arguments, call_id=call.id
                )
            except Exception as exc:
                self._answer_a_failed_step(messages, calls[position:], exc)
                raise
            tool_duration = time.time() - tool_start
            self.tool_call_recorder.record(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments or {},
                executor=ToolExecutorIdentity.USER,
                status=resolve_tool_status(tool_result),
                output=resolve_tool_output(tool_result),
                latency_seconds=tool_duration,
            )
            self.logger.debug(
                "User tool executed",
                tool=call.name,
                success=tool_result.success,
                duration_s=tool_duration,
            )
            content = (
                tool_result.output
                if tool_result.success
                else f"Error: {resolve_tool_output(tool_result)}"
            )
            messages.append(self._user_tool_message(call.id, content))

    def _answer_a_failed_step(
        self, messages: list[Message], calls: list[ToolCall], exc: Exception
    ) -> None:
        """TOOL messages for a step whose first remaining call raised: its error, then
        a "not run" answer for each call after it."""
        failed, *unrun = calls
        messages.append(self._user_tool_message(failed.id, f"Error: {exc}"))
        messages.extend(
            self._user_tool_message(call.id, "Error: not run, an earlier call of this step raised.")
            for call in unrun
        )

    @staticmethod
    def _user_tool_message(call_id: str, content: str) -> Message:
        return Message(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id=call_id,
            ts=datetime.now(tz=timezone.utc),
        )

    @staticmethod
    def _user_stop_decision(
        stop: UserStop, *, after_final_reply: bool = False
    ) -> TerminationDecision:
        when = " after final reply" if after_final_reply else ""
        return TerminationDecision(
            reason=TerminationReason.USER_STOP,
            system_message=f"User signaled stop ({stop.token}{when}). Dialogue ended.",
        )

    def _run_user_tool_calls(
        self, reply_text: str, tool_calls: list[ToolCall]
    ) -> tuple[str, list[ToolCall]]:
        """Execute one user reply's tool calls; return the message text and the calls.

        Every call is keyed through the trial's assigner before it is executed,
        recorded or written onto the message, so a raw provider id the agent
        already used is disambiguated rather than recorded twice.

        Results are embedded in the user message text — Anthropic does not
        accept ``tool_use`` from the USER role — while the calls themselves ride
        on the message so ``transcript_rules.required_actions`` can match them.

        Raises:
            RuntimeError: If the reply carries calls and the trial has no user-side
                executor. The conductor builds the executor and offers the schemas
                together, so a simulator with no executor is offered no tools and a
                provider offered no tools emits no calls — the pair is unreachable
                from a real run, and running on would silently drop the calls a rule
                may be grading.
        """
        if not tool_calls:
            return reply_text, []
        if self.user_tool_executor is None:
            raise RuntimeError(
                f"the user simulator emitted {len(tool_calls)} tool call(s) "
                f"({', '.join(call.name for call in tool_calls)}) and this trial has no "
                "user-side executor to run them. The trial is built with both or neither"
            )

        executed: list[ToolCall] = []
        results_text: list[str] = []
        for call in tool_calls:
            keyed = call.model_copy(update={"id": self._call_ids.assign(call.id)})
            executed.append(keyed)

            tool_start = time.time()
            tool_result = self.user_tool_executor.execute(
                keyed.name, keyed.arguments, call_id=keyed.id
            )
            tool_duration = time.time() - tool_start

            self.tool_call_recorder.record(
                call_id=keyed.id,
                tool_name=keyed.name,
                arguments=keyed.arguments or {},
                executor=ToolExecutorIdentity.USER,
                status=resolve_tool_status(tool_result),
                output=resolve_tool_output(tool_result),
                latency_seconds=tool_duration,
            )

            self.logger.debug(
                "User tool executed",
                tool=keyed.name,
                success=tool_result.success,
                duration_s=tool_duration,
            )

            outcome = tool_result.output if tool_result.success else f"Error: {tool_result.error}"
            results_text.append(f"{keyed.name}() result: {outcome}")

        return f"{reply_text}\n\n" + "\n".join(results_text), executed


class _TrialMetricsSink(MetricsSink):
    """Accumulates an in-trial actor's per-call usage/cost and tool counts into
    the trial :class:`Metrics`, preserving the original field-wise semantics.

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
        self._last_prompt_tokens: int | None = None

    def record_generation(self, result: GenerationResult) -> None:
        self._metrics.api_calls += 1
        self._metrics.usage = self._metrics.usage + result.usage
        if result.openrouter_generation_id is not None:
            self._metrics.openrouter_generation_ids.append(result.openrouter_generation_id)
        if result.cost_usd is not None:
            if self._metrics.cost_usd is None:
                self._metrics.cost_usd = result.cost_usd
            else:
                self._metrics.cost_usd += result.cost_usd
        # Sticky: one call priced without the cache rate it needed makes the
        # trial's summed ``cost_usd`` an overestimate, whatever the other
        # calls did.
        if result.cost_cache_rate_fallback:
            self._metrics.cost_cache_rate_fallback = True
        if result.reasoning_billed_not_captured:
            self._metrics.reasoning_billed_not_captured += 1
        # Sticky for the same reason the cache-rate flag is: once a codec
        # declines to replay, it declines on every turn, so a count would only
        # restate the turn count.
        if result.reasoning_replay_dropped:
            self._metrics.reasoning_replay_dropped = True
        self._last_prompt_tokens = result.usage.prompt_tokens
        self._events.trial_progress(
            trial_id=self._trial_id,
            prompt_tokens_delta=result.usage.prompt_tokens,
            completion_tokens_delta=result.usage.completion_tokens,
            cost_delta_usd=result.cost_usd if result.cost_usd is not None else 0.0,
        )

    def record_tool_call(self) -> None:
        self._metrics.tool_calls += 1

    def record_tool_output_truncated(self, omitted_chars: int) -> None:
        self._metrics.tool_output_chars_truncated += omitted_chars

    def record_parser_errors(self, errors: tuple[ParserError, ...]) -> None:
        self._metrics.parser_errors.extend(
            ParserErrorRecord(
                tool_name=e.tool_name,
                raw_arguments=e.raw_arguments,
                reason=e.reason,
            )
            for e in errors
        )

    @property
    def last_prompt_tokens(self) -> int | None:
        return self._last_prompt_tokens
