"""Generic multi-turn tool-calling loop engine.

This module lifts the agent's multi-turn tool-calling loop out of
:class:`tolokaforge.core.runner.TrialRunner` into a reusable engine so that
both the agent (today) and a read-only rubric judge (a later stage) can run on
the same machinery.

The engine is deliberately behaviour-light: it owns turn structure (episode
timeout, generate, accumulate, append assistant, terminate, execute tools,
optional user turn, error classification, max-turns), and delegates every
*policy* decision to pluggable seams:

* :class:`LoopLLMClient` — the provider-agnostic generate seam (the agent's
  :class:`~tolokaforge.core.llm.client.LLMClient` already satisfies it).
* :class:`TerminationPolicy` — a callback ``(result, turn, messages) ->
  TerminationDecision | None`` checked after the assistant message is appended
  and before tool execution. The agent terminates on stuck-detection and its
  ``###STOP###``/``_is_done`` marker; the judge will terminate when
  ``submit_report`` is called.
* :class:`UserTurn` — OPTIONAL. When absent, the loop never references
  user-simulator concepts (no ``###STOP###`` handling, no user reply turn). The
  judge runs without one.
* :class:`MetricsSink` — accumulates per-call usage/cost/tool counts. The agent
  threads its trial :class:`~tolokaforge.core.models.Metrics`; the judge will
  thread its own.
* :class:`~tolokaforge.core.models.ToolCallRecorder` — OPTIONAL. The trial's
  ordered tool-call record. The agent threads the trial's recorder; the judge
  threads none, so its own tool calls stay out of the graded record.
* :data:`ErrorClassifier` — maps a raised exception to a terminal reason +
  message. :func:`classify_loop_error` reproduces the agent's exact
  classification and is the default both paths use.

See ``docs/RUBRIC_GRADING_DESIGN.md`` Stage 1 for design rationale.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from tolokaforge.core.llm.client import GenerationResult, LLMApiTimeoutError
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Message,
    MessageRole,
    TerminationReason,
    ToolCallRecorder,
    ToolExecutorIdentity,
    TrialStatus,
)
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.tools.registry import ToolExecutor, resolve_tool_status


class LoopLLMClient(Protocol):
    """The generate seam the loop drives each turn.

    The agent's :class:`~tolokaforge.core.llm.client.LLMClient` satisfies this
    structurally; the judge constructs its own client over the same contract.
    """

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult: ...


@dataclass(frozen=True)
class LoopConfig:
    """In-process loop budget. Frozen value object (AGENTS.md type table).

    The loop is bounded by ``max_turns`` and by wall-time via
    ``episode_timeout_s`` (enforced in :meth:`ToolCallingLoop._check_episode_timeout`).
    There is deliberately no per-turn timeout: a previously declared
    ``turn_timeout_s`` field was never enforced (dead on ``main`` and through
    Stage 1) and was dropped in Stage 4 — per-turn LLM timeouts are already
    handled inside :class:`~tolokaforge.core.llm.client.LLMClient`
    (``api_call_timeout_s`` + bounded retry), so episode wall-time is the only
    loop-level time bound.
    """

    max_turns: int = 50
    episode_timeout_s: int = 1200


@dataclass(frozen=True)
class TerminationDecision:
    """A loop-terminating verdict and the system message that records it.

    Returned by a :class:`TerminationPolicy`. The engine appends ``system_message``
    as a :data:`MessageRole.SYSTEM` message and stops the loop with ``reason``.
    ``status`` lets a policy promote the trial status (e.g. ``TIMEOUT``); when
    ``None`` the engine leaves the optimistic ``COMPLETED`` default in place.
    """

    reason: TerminationReason
    system_message: str
    status: TrialStatus | None = None


class TerminationPolicy(Protocol):
    """Decides, after each assistant turn, whether the loop should stop.

    Called with the just-received ``result``, the zero-based ``turn`` index, and
    the live ``messages`` list (assistant message already appended). Returns a
    :class:`TerminationDecision` to stop, or ``None`` to continue. Any side
    effect a policy needs (e.g. flipping ``metrics.stuck_detected``) is the
    policy's own responsibility — the engine only consumes the decision.
    """

    def __call__(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None: ...


@dataclass(frozen=True)
class UserTurnResult:
    """Outcome of an optional user turn.

    Either the loop terminates (``termination`` set — e.g. ``###STOP###``) or a
    user :class:`Message` is appended and the loop continues (``message`` set).
    Exactly one is populated.
    """

    message: Message | None = None
    termination: TerminationDecision | None = None


class UserTurn(Protocol):
    """Optional seam invoked when an assistant turn produced no tool calls.

    Absent for the judge: when the engine has no :class:`UserTurn`, a
    no-tool-call assistant turn simply advances to the next turn (re-prompt),
    and the loop never touches user-simulator concepts.
    """

    def __call__(self, messages: list[Message]) -> UserTurnResult: ...


class MetricsSink(Protocol):
    """Accumulates per-call usage/cost and tool-call counts.

    Parameterised so the agent threads its trial ``Metrics`` while a future
    judge threads its own accounting object.
    """

    def record_generation(self, result: GenerationResult) -> None: ...

    def record_tool_call(self) -> None: ...


ErrorClassifier = Callable[[Exception], TerminationDecision]


def classify_loop_error(exc: Exception) -> TerminationDecision:
    """Classify a turn-loop exception into a terminal reason + message.

    Reproduces the agent's exact classification. ``LLMApiTimeoutError`` is
    matched by *type* — substring matching on "timeout" would mis-classify
    tool / sandbox / browser timeouts that are not upstream-LLM timeouts.
    """
    error_str = str(exc)
    if isinstance(exc, LLMApiTimeoutError):
        return TerminationDecision(
            reason=TerminationReason.API_TIMEOUT,
            system_message=f"API timeout: {error_str}. Dialogue terminated.",
            status=TrialStatus.ERROR,
        )
    lowered = error_str.lower()
    if "429" in error_str or "RateLimitError" in error_str or "rate limit" in lowered:
        return TerminationDecision(
            reason=TerminationReason.RATE_LIMIT,
            system_message=f"Rate limit error: {error_str}. Dialogue terminated.",
            status=TrialStatus.ERROR,
        )
    if "API" in error_str or "OpenAI" in error_str or "Anthropic" in error_str:
        return TerminationDecision(
            reason=TerminationReason.API_ERROR,
            system_message=f"API error: {error_str}. Dialogue terminated.",
            status=TrialStatus.ERROR,
        )
    return TerminationDecision(
        reason=TerminationReason.ERROR,
        system_message=f"Error: {error_str}. Dialogue terminated.",
        status=TrialStatus.ERROR,
    )


@dataclass
class LoopOutcome:
    """What the loop produced. The caller owns ``messages`` and builds the
    trajectory; this carries only the loop-level verdict."""

    status: TrialStatus
    termination_reason: TerminationReason | None
    captured_effective_system_prompt: str | None = None


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class ToolCallingLoop:
    """Generic multi-turn tool-calling engine.

    Drives turn structure and delegates every policy decision to its seams. The
    same instance owns no trial state beyond the loop; ``messages`` and the
    metrics sink are supplied by the caller so the agent and the judge can each
    bring their own accumulation target.
    """

    llm_client: LoopLLMClient
    tool_executor: ToolExecutor
    tool_schemas: list[dict[str, Any]]
    config: LoopConfig
    metrics: MetricsSink
    should_terminate: TerminationPolicy
    logger: StructuredLogger
    user_turn: UserTurn | None = None
    # The trial's ordered tool-call record. Injected rather than owned: the
    # rubric judge runs this same loop over its own read-only tools, and a
    # grading-time tool call must never enter the trial's record.
    recorder: ToolCallRecorder | None = None
    request_limiter: Any | None = None
    normalize_tool_arguments: Callable[[str, dict[str, Any] | None, str], dict[str, Any]] | None = (
        None
    )
    classify_error: ErrorClassifier = classify_loop_error
    call_observation: LLMCallObservation | None = None

    # Captured from the first generation's effective system prompt.
    _captured_effective_prompt: str | None = field(default=None, init=False)
    _captured: bool = field(default=False, init=False)

    def run(self, system_prompt: str, messages: list[Message], start_time: float) -> LoopOutcome:
        """Run the turn loop, mutating ``messages`` in place.

        ``start_time`` is the ``time.time()`` epoch the episode began at — the
        episode-timeout budget is measured against it. Returns the loop verdict;
        the caller assembles the trajectory.
        """
        status = TrialStatus.COMPLETED
        termination_reason: TerminationReason | None = None

        for turn in range(self.config.max_turns):
            timeout_decision = self._check_episode_timeout(start_time)
            if timeout_decision is not None:
                status = timeout_decision.status or status
                termination_reason = timeout_decision.reason
                messages.append(self._system_message(timeout_decision.system_message))
                break

            try:
                turn_status, turn_reason, stop = self._run_turn(turn, system_prompt, messages)
            except Exception as exc:  # noqa: BLE001 — classified, then surfaced via status
                self.logger.error(
                    "Error during turn execution",
                    turn=turn,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                decision = self.classify_error(exc)
                messages.append(self._system_message(decision.system_message))
                status = decision.status or TrialStatus.ERROR
                termination_reason = decision.reason
                break

            if turn_status is not None:
                status = turn_status
            if stop:
                termination_reason = turn_reason
                break
        else:
            termination_reason = TerminationReason.MAX_TURNS
            messages.append(
                self._system_message(
                    f"Maximum turns ({self.config.max_turns}) reached. Dialogue terminated."
                )
            )

        return LoopOutcome(
            status=status,
            termination_reason=termination_reason,
            captured_effective_system_prompt=self._captured_effective_prompt,
        )

    def _run_turn(
        self, turn: int, system_prompt: str, messages: list[Message]
    ) -> tuple[TrialStatus | None, TerminationReason | None, bool]:
        """Execute a single turn. Returns ``(status_override, reason, stop)``.

        ``stop`` requests the loop break with ``reason``. A non-``None``
        ``status_override`` promotes the trial status. Raised exceptions are
        classified by the caller.
        """
        result = self._generate(turn, system_prompt, messages)
        self._capture_effective_prompt(result)
        self.metrics.record_generation(result)
        self._log_generation(turn, result)
        messages.append(self._assistant_message(result))

        decision = self.should_terminate(result, turn, messages)
        if decision is not None:
            messages.append(self._system_message(decision.system_message))
            return decision.status, decision.reason, True

        if result.tool_calls:
            self._execute_tool_calls(result, messages)
            return None, None, False

        return self._advance_user_turn(messages)

    def _generate(self, turn: int, system_prompt: str, messages: list[Message]) -> GenerationResult:
        self.logger.debug("Requesting agent response", turn=turn)
        if self.request_limiter is not None:
            self.request_limiter.acquire()
        return self.llm_client.generate(
            system=system_prompt,
            messages=messages,
            tools=self.tool_schemas,
            tool_choice="auto",
            observation=self.call_observation,
        )

    def _advance_user_turn(
        self, messages: list[Message]
    ) -> tuple[TrialStatus | None, TerminationReason | None, bool]:
        """No tool calls and no termination: hand to the optional user turn.

        Without a user turn (the judge), the loop simply advances to the next
        turn — user-simulator concepts are never referenced.
        """
        if self.user_turn is None:
            return None, None, False

        outcome = self.user_turn(messages)
        if outcome.termination is not None:
            messages.append(self._system_message(outcome.termination.system_message))
            return outcome.termination.status, outcome.termination.reason, True

        if outcome.message is not None:
            messages.append(outcome.message)
        return None, None, False

    def _execute_tool_calls(self, result: GenerationResult, messages: list[Message]) -> None:
        for tc in result.tool_calls:
            self._maybe_recover_arguments(tc, result.text)
            tool_start = time.time()
            tool_result = self.tool_executor.execute(tc.name, tc.arguments, call_id=tc.id)
            tool_duration = time.time() - tool_start
            self.metrics.record_tool_call()

            if self.recorder is not None:
                self.recorder.record(
                    call_id=tc.id,
                    tool_name=tc.name,
                    arguments=tc.arguments or {},
                    executor=ToolExecutorIdentity.AGENT,
                    status=resolve_tool_status(tool_result),
                    output=(
                        tool_result.output if tool_result.success else (tool_result.error or "")
                    ),
                    latency_seconds=tool_duration,
                )

            if tool_result.success:
                self.logger.debug(
                    "Tool executed successfully", tool=tc.name, duration_s=tool_duration
                )
            else:
                self.logger.warning("Tool execution failed", tool=tc.name, error=tool_result.error)

            messages.append(
                Message(
                    role=MessageRole.TOOL,
                    content=(
                        tool_result.output if tool_result.success else f"Error: {tool_result.error}"
                    ),
                    content_blocks=(tool_result.content_blocks if tool_result.success else None),
                    tool_call_id=tc.id,
                    ts=_now(),
                )
            )

    def _maybe_recover_arguments(self, tc: Any, assistant_text: str) -> None:
        if self.normalize_tool_arguments is None:
            return
        normalized_args = self.normalize_tool_arguments(tc.name, tc.arguments, assistant_text)
        if normalized_args != tc.arguments:
            self.logger.warning(
                "Recovered malformed tool arguments from assistant text",
                tool=tc.name,
                recovered_keys=sorted(
                    set(normalized_args.keys()) - set((tc.arguments or {}).keys())
                ),
            )
            tc.arguments = normalized_args

    def _check_episode_timeout(self, start_time: float) -> TerminationDecision | None:
        elapsed = time.time() - start_time
        if elapsed <= self.config.episode_timeout_s:
            return None
        self.logger.warning(
            "Episode timeout reached",
            elapsed_s=elapsed,
            timeout_s=self.config.episode_timeout_s,
        )
        return TerminationDecision(
            reason=TerminationReason.TIMEOUT,
            system_message=(
                f"Episode timeout reached ({self.config.episode_timeout_s}s). Dialogue terminated."
            ),
            status=TrialStatus.TIMEOUT,
        )

    def _capture_effective_prompt(self, result: GenerationResult) -> None:
        if result.effective_system_prompt and not self._captured:
            self._captured_effective_prompt = result.effective_system_prompt
            self._captured = True

    def _log_generation(self, turn: int, result: GenerationResult) -> None:
        self.logger.debug(
            "Agent response received",
            turn=turn,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            reasoning_tokens=result.usage.reasoning_tokens,
            cache_read_input_tokens=result.usage.cache_read_input_tokens,
        )
        if result.tool_calls:
            self.logger.debug(
                "Agent requested tool calls",
                count=len(result.tool_calls),
                tools=[tc.name for tc in result.tool_calls],
            )

    @staticmethod
    def _assistant_message(result: GenerationResult) -> Message:
        return Message(
            role=MessageRole.ASSISTANT,
            content=result.text,
            tool_calls=result.tool_calls if result.tool_calls else None,
            reasoning=result.reasoning,
            ts=_now(),
        )

    @staticmethod
    def _system_message(content: str) -> Message:
        return Message(role=MessageRole.SYSTEM, content=content, ts=_now())
