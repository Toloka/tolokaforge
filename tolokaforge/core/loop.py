"""Generic multi-turn tool-calling loop engine.

This module lifts the agent's multi-turn tool-calling loop out of
:class:`tolokaforge.core.runner.TrialRunner` into a reusable engine so that
both the agent (today) and a read-only rubric judge (a later stage) can run on
the same machinery.

The engine is deliberately behaviour-light: it owns turn structure (episode
timeout, generate, accumulate, append assistant, terminate, execute tools,
optional user turn, error classification, max-turns), and delegates every
*policy* decision to pluggable seams. The engine consumes three wire-shape
observations directly: the provider-shaped *empty completion* —
``result.text == "" and not result.tool_calls``, with no reasoning billed and
no reasoning billed — resamples up to ``LoopConfig.empty_retry_count`` times
without appending the empty assistant message, and on the ``(N + 1)``-th empty
result terminates the trial with :attr:`TerminationReason.EMPTY_COMPLETION`
(the Gemini-legal-tail invariant is preserved end-to-end because the empty
message is never appended — appending it would send a request whose tail is an
empty ``role=model`` turn on the next iteration and providers such as Gemini
reject that as an API error rather than pass it through); the *reasoning
without action* — the same actionless shape, but with reasoning tokens billed
or ``finish_reason == "length"``, covering both a model cut off mid-thought at
the ceiling and one that deliberated briefly and then returned nothing —
resamples with a ``role=user`` feedback turn under
``LoopConfig.reasoning_stall_retry_count`` and terminates with
:attr:`TerminationReason.REASONING_WITHOUT_ACTION`, carrying the
``finish_reason`` and token counts it was read from; the content-carrying *max-tokens truncation* —
``result.finish_reason == "length"`` on a content-carrying result —
resamples with a ``role=user`` feedback turn under
``LoopConfig.output_length_retry_count`` before falling through to
accept-and-continue; and the *un-parseable tool_call arguments* —
``result.parser_errors`` non-empty — resamples with a ``role=user``
feedback turn naming the failing tools and quoting the raw arguments under
``LoopConfig.parser_error_retry_count`` before falling through to accept the
``{}``-coerced response.

Every tool call the loop makes travels through :class:`ToolCallFunnel`, the
shared discharge point for the obligations grading enforces and the type
checker does not — the episode-unique call id, the failed-call message
prefix, the recorder entry, the output cap, the metrics tick and the observer
notification. An external loop reaches the same implementation through
``tolokaforge.core.plugin_registry``.

The funnel also owns a defensive bound on tool-output size that lands in the
message history: when ``LoopConfig.tool_output_max_chars`` is set,
:meth:`ToolCallFunnel.cap_tool_message_content` middle-elides the ``role=tool``
message ``content`` via
:func:`~tolokaforge.core.tool_output_truncation.keep_head_and_tail` before the
message is appended, so accumulated context stays predictable across turns.
The recorder and the grader still see the full text — the cap sits below the
recorder's :func:`resolve_tool_output` read.

* :class:`LoopLLMClient` — the provider-agnostic generate seam (the agent's
  :class:`~tolokaforge.core.llm.client.LLMClient` already satisfies it).
* :class:`TerminationPolicy` — a callback ``(result, turn, messages) ->
  TerminationDecision | None`` checked after the assistant message is appended
  and before tool execution. The agent terminates on stuck-detection; the judge
  terminates when ``submit_report`` is called. Neither reads assistant prose.
* :class:`UserTurn` — OPTIONAL. When absent, the loop never references
  user-simulator concepts (no exit-token handling, no user reply turn). The
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

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import litellm.exceptions

from tolokaforge.core.llm.client import (
    GenerationResult,
    LLMApiTimeoutError,
    ParserError,
    is_typed_rate_limit_exception,
    matches_rate_limit_text,
)
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Message,
    MessageRole,
    TerminationReason,
    ToolCall,
    ToolCallRecorder,
    ToolExecutorIdentity,
    TrialStatus,
)
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.simulation_budget import SimulationBudget
from tolokaforge.core.summarize_policy import SummarizePolicy, SummarizerFailedError
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.core.tool_message_format import TOOL_ERROR_MESSAGE_PREFIX
from tolokaforge.core.tool_output_truncation import keep_head_and_tail
from tolokaforge.runner.protocol import TrialNotRegisteredError
from tolokaforge.tools.registry import (
    ToolExecuting,
    ToolExecutionStatus,
    ToolResult,
    resolve_tool_output,
    resolve_tool_status,
)

if TYPE_CHECKING:
    from tolokaforge.observability.observer import LoopObserver


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
    There is deliberately no per-turn timeout: per-turn LLM timeouts are handled
    inside :class:`~tolokaforge.core.llm.client.LLMClient` (``api_call_timeout_s``
    + bounded retry), so episode wall-time is the only loop-level time bound.

    ``api_error_retries`` and ``api_error_backoff_s`` set the loop-level bounded
    retry that fires only on :attr:`TerminationReason.API_ERROR` — a transient
    provider fault the classifier could not attribute to a typed reason.
    ``empty_retry_count`` sets the resample budget for a returned empty
    completion; ``output_length_retry_count`` sets the resample budget for a
    content-carrying max-tokens truncation; ``parser_error_retry_count`` sets
    the resample budget for a response whose ``tool_call.function.arguments``
    string could not be decoded. The four retry classes are orthogonal — the
    API-error retry replays a raised exception, the empty-completion retry
    resamples a returned empty-shape result without appending the empty
    assistant message and without advancing the outer turn counter, the
    output-length retry appends a ``role=user`` feedback turn and resamples a
    truncated content-carrying result under its own budget before falling
    through to accept-and-continue, and the parser-error retry appends a
    ``role=user`` feedback turn naming the failing tools and resamples under
    its own budget before falling through to accept the ``{}``-coerced
    response. Each owns a dedicated ``LoopConfig`` field and each fires on a
    distinct trigger. ``RATE_LIMIT``, ``API_TIMEOUT`` and ``TRIAL_LOST`` stay
    one-shot terminal because each owns a dedicated path (typed 429 handling,
    transport-timeout retry, substrate re-registration) and retrying them
    here would double-count them.

    ``tool_output_max_chars`` is the per-model backstop cap on the
    ``role=tool`` message ``content``.
    :meth:`ToolCallFunnel.cap_tool_message_content` middle-elides via
    :func:`~tolokaforge.core.tool_output_truncation.keep_head_and_tail` using
    the tighter of this cap and the tool's own
    :attr:`~tolokaforge.tools.registry.ToolPolicy.output_max_chars` — carried
    into the loop as :attr:`ToolCallingLoop.tool_output_max_chars_by_tool` —
    before the message is appended; ``None`` on both axes threads tool output
    through verbatim.

    ``max_context_tokens``, ``context_watermark`` and ``summarize_policy``
    arm the context-window summarize seam (see
    :mod:`tolokaforge.core.summarize_policy`). The pre-turn watermark check
    fires only when **all three** are set and
    ``MetricsSink.last_prompt_tokens + context_watermark >=
    max_context_tokens``; any ``None`` leaves the loop's pre-opt-in
    behaviour intact.
    """

    max_turns: int = 50
    episode_timeout_s: int = 1200
    api_error_retries: int = 1
    api_error_backoff_s: float = 1.0
    empty_retry_count: int = 0
    reasoning_stall_retry_count: int = 1
    reasoning_stall_turn_limit: int = 0
    output_length_retry_count: int = 0
    parser_error_retry_count: int = 0
    tool_output_max_chars: int | None = None
    max_context_tokens: int | None = None
    context_watermark: int | None = None
    summarize_policy: SummarizePolicy | None = None


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
    excluding_reason_evidence: str | None = None
    """The typed observation behind a denominator-excluding ``reason``.

    Non-``None`` only where ``reason`` is in
    :data:`~tolokaforge.core.failure_attribution.EXCLUDED_TYPED_REASONS` and the
    decision was reached from an exception type or an HTTP status rather than
    from matching prose. :func:`classify_loop_error` fills it on exactly those
    branches, which is what makes a classified exception self-evidencing: a
    loop that routes its exception through ``context.classify_error`` copies
    this onto :attr:`LoopOutcome.excluding_reason_evidence` and has earned the
    exclusion without inspecting the exception itself.
    """


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

    ``message`` alone appends a user :class:`Message` and the loop continues.
    ``termination`` alone stops the loop (e.g. a bare stop token). Both together
    append the message as the dialogue's last turn and then stop, so the agent
    never answers it. With neither set, the loop moves on to the next agent turn
    without a user message.
    """

    message: Message | None = None
    termination: TerminationDecision | None = None


class UserTurn(Protocol):
    """Optional seam invoked when an assistant turn produced no tool calls.

    Absent for the judge: when the engine has no :class:`UserTurn`, a
    no-tool-call assistant turn simply advances to the next turn (re-prompt),
    and the loop never touches user-simulator concepts.

    ``messages`` is the recorded transcript. A user turn may append to it
    before returning — the messages it appends are recorded and never sent to
    the agent, which is how an ``isolated`` user's tool steps are kept; only
    :attr:`UserTurnResult.message` is mirrored into the agent's wire.
    """

    def __call__(self, messages: list[Message]) -> UserTurnResult: ...


class MetricsSink(Protocol):
    """Accumulates per-call usage/cost and tool-call counts.

    Parameterised so the agent threads its trial ``Metrics`` while a future
    judge threads its own accounting object.

    ``last_prompt_tokens`` exposes the ``prompt_tokens`` charged on the most
    recent ``record_generation`` call. The engine reads it to arm the
    pre-turn summarize check; a subclass that never overrides inherits
    ``None`` and never trips the check.
    """

    def record_generation(self, result: GenerationResult) -> None: ...

    def record_tool_call(self) -> None: ...

    @property
    def last_prompt_tokens(self) -> int | None:
        return None

    def record_tool_output_truncated(self, omitted_chars: int) -> None:
        """Accumulate a per-trial count of characters clipped from tool outputs.

        The funnel calls this every time ``cap_tool_message_content`` actually
        elides a ``role=tool`` message. A trial with the cumulative count at
        zero saw no truncation, either because no cap fired or because every
        raw output fit within the effective cap. Default no-op so a sink that
        does not track truncation stays satisfied.
        """
        return None

    def record_parser_errors(self, errors: tuple[ParserError, ...]) -> None:
        """Persist per-turn ``tool_call.function.arguments`` parse errors.

        The loop calls this on every generation whose ``parser_errors``
        sidecar is non-empty, regardless of whether ``parser_error_retry_count``
        subsequently resamples. Default no-op — a sink that does not track
        parser errors stays satisfied.
        """
        return None


ErrorClassifier = Callable[[Exception], TerminationDecision]
"""One-arg callable ``ToolCallingLoop`` invokes on a turn-loop exception.

The provider's rate-limit text patterns are closed over by the callable
(``LLMClient.classify_loop_error`` binds them from the client's
:class:`ProviderBinding`), so the loop itself never carries provider state.
:func:`classify_loop_error` is the two-arg module-level implementation the
bound method delegates to.
"""


def _exception_type_evidence(exc: BaseException) -> str:
    """The exception chain's type names, outermost first.

    Names what the typed branches of :func:`classify_loop_error` matched on.
    Types rather than message text, and the whole ``__cause__`` chain because
    the client re-raises every provider error wrapped — the 429 the rate-limit
    predicate found is a cause, not the outermost type.
    """
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__qualname__)
        current = current.__cause__
    return " <- ".join(names)


def classify_loop_error(
    exc: Exception, patterns: tuple[re.Pattern[str], ...]
) -> TerminationDecision:
    """Classify a turn-loop exception into a terminal reason + message.

    ``TRIAL_LOST`` is matched by type first and is the one typed reason here that
    keeps the trial in the denominator: a runner that no longer holds the trial
    is our fault, and the call it refused reached no tool, so the trial ends at
    the fault rather than handing the agent a failure to retry against.

    ``API_TIMEOUT`` and ``RATE_LIMIT`` are matched by *type* — through the
    ``__cause__`` chain for the rate limit, since the client re-raises every
    provider error wrapped. Both reasons exclude the trial from the measured
    denominator, and a substring is not strong enough evidence to spend that:
    "timeout" also names tool / sandbox / browser timeouts, and a 429 shape can
    appear inside a provider response body that echoes request content.

    An exception whose *text* looks like a rate limit but carries no typed
    evidence terminates as ``ERROR``. Something in the stack stringified a
    provider exception instead of chaining it — our defect, so the trial counts
    and the message says why it was not treated as a rate limit. Text matching
    consults *patterns* — the caller's compiled per-provider list.
    """
    error_str = str(exc)
    if isinstance(exc, TrialNotRegisteredError):
        return TerminationDecision(
            reason=TerminationReason.TRIAL_LOST,
            system_message=f"{error_str} Dialogue terminated.",
            status=TrialStatus.ERROR,
        )
    if isinstance(exc, litellm.exceptions.ContextWindowExceededError):
        return TerminationDecision(
            reason=TerminationReason.CONTEXT_WINDOW_EXCEEDED,
            system_message=f"Context window exceeded: {error_str}. Dialogue terminated.",
            status=TrialStatus.FAILED,
        )
    if isinstance(exc, LLMApiTimeoutError):
        return TerminationDecision(
            reason=TerminationReason.API_TIMEOUT,
            system_message=f"API timeout: {error_str}. Dialogue terminated.",
            status=TrialStatus.ERROR,
            excluding_reason_evidence=_exception_type_evidence(exc),
        )
    if is_typed_rate_limit_exception(exc):
        return TerminationDecision(
            reason=TerminationReason.RATE_LIMIT,
            system_message=f"Rate limit error: {error_str}. Dialogue terminated.",
            status=TrialStatus.ERROR,
            excluding_reason_evidence=_exception_type_evidence(exc),
        )
    if matches_rate_limit_text(error_str, patterns):
        return TerminationDecision(
            reason=TerminationReason.ERROR,
            system_message=(
                "Rate-limit-shaped error with no typed provider exception behind it, "
                f"so the trial is counted rather than excluded: {error_str}. "
                "Dialogue terminated."
            ),
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


def episode_timeout_decision(
    start_time: float, episode_timeout_s: float, logger: StructuredLogger
) -> TerminationDecision | None:
    """The episode-timeout verdict once ``episode_timeout_s`` has passed since ``start_time``.

    The loop checks it between turns; a user turn that asks its simulator more
    than once checks it between those asks, since one turn can then run long.
    """
    elapsed = time.time() - start_time
    if elapsed <= episode_timeout_s:
        return None
    logger.warning("Episode timeout reached", elapsed_s=elapsed, timeout_s=episode_timeout_s)
    return TerminationDecision(
        reason=TerminationReason.TIMEOUT,
        system_message=f"Episode timeout reached ({episode_timeout_s}s). Dialogue terminated.",
        status=TrialStatus.TIMEOUT,
    )


@dataclass
class LoopOutcome:
    """What the loop produced. The caller owns ``messages`` and builds the
    trajectory; this carries only the loop-level verdict."""

    status: TrialStatus
    termination_reason: TerminationReason | None
    captured_effective_system_prompt: str | None = None
    excluding_reason_evidence: str | None = None
    """The typed observation behind a denominator-excluding termination reason.

    Set only when :attr:`termination_reason` is one of
    :data:`~tolokaforge.core.failure_attribution.EXCLUDED_TYPED_REASONS` and the
    loop reached it from an exception type, an HTTP status or a typed
    empty-completion observation — never from matching prose against an
    exception message. The value names that observation.

    A loop that routes its exceptions through ``context.classify_error`` copies
    :attr:`TerminationDecision.excluding_reason_evidence` here and needs no
    typing knowledge of its own.

    ``None`` on every other reason, and ``None`` on an excluding reason the loop
    cannot evidence: the caller then counts the trial as
    :attr:`~tolokaforge.core.models.TerminationReason.ERROR` rather than
    dropping it from the measured denominator. See obligation 4 on
    :class:`AgentLoop`.
    """


@runtime_checkable
class AgentLoop(Protocol):
    """The in-process driver of one trial's agent turn cycle.

    An implementation owns turn structure — generate, act, observe, decide
    whether to continue — and reports the trial-level verdict. The caller owns
    ``messages``, the tool-call recorder and the trajectory: the loop appends to
    ``messages`` in place, records each tool call it executes, and returns a
    :class:`LoopOutcome`.

    Four obligations are enforced downstream — in grading, and in the caller's
    accounting — rather than by the type checker. Each is silent or fatal after
    the fact, never at write time.
    :class:`ToolCallFunnel` discharges the first two for any implementation
    that routes its tool calls through it — ``ToolCallFunnel.from_context(ctx)``
    once per episode, then :meth:`ToolCallFunnel.assign_ids` before the
    assistant message and :meth:`ToolCallFunnel.execute` after it. An
    implementation that writes the sequence itself owes what follows.

    **1. Every call id comes from the context's assigner.** An implementation
    MUST key each tool call by ``context.call_ids.assign(<provider id>)`` and
    use that one key in all three places: the ``id`` of the
    :class:`~tolokaforge.core.models.ToolCall` on the assistant
    :class:`~tolokaforge.core.models.Message` it appends, the ``call_id`` handed
    to ``recorder.record(...)``, and the ``call_id`` handed to
    ``tool_executor.execute(...)``.
    :func:`~tolokaforge.core.grading.trace_timeline.build_trial_timeline` joins
    the message view to the record view by that id alone — never by position —
    and ``_require_records_reconcile`` raises
    :class:`~tolokaforge.core.grading.trace_timeline.TimelineInconsistencyError`
    when a record answers no declaration, or names a tool its declaration did
    not. Agreeing on a *raw* provider id is not enough: each view re-derives its
    keys with :func:`~tolokaforge.core.tool_call_ids.episode_unique_call_ids`
    over its own ordering — declaration order for the messages, execution order
    for the records — so a provider that repeats a raw id within an episode (the
    ``<tool>:<index within the turn>`` shape in
    :mod:`tolokaforge.core.tool_call_ids`) plus calls executed out of
    declaration order makes the two derivations disagree. The disagreement
    raises when the mis-paired calls name different tools and mis-joins silently
    when they name the same one. Pre-assigned ids are already episode-unique, so
    both derivations are the identity and the order cannot matter.

    An implementation whose action format is text rather than provider
    ``tool_calls`` normalises every parsed action into a ``ToolCall`` carrying
    that id *before* appending the assistant message; prose plus a separate
    record leaves the trial ungradeable.

    **2. A failed tool call's message content carries**
    :data:`~tolokaforge.core.tool_message_format.TOOL_ERROR_MESSAGE_PREFIX`. The
    message view records no status, so a trial re-graded from messages alone
    recovers the result text by stripping that prefix. A loop that formats tool
    errors any other way makes every failed call read as a successful one to
    ``result:`` trace checks — a wrong grade, not an error.

    **3. The non-optional context fields are obligations, not offers.** See
    :class:`AgentLoopContext` for which fields an implementation may ignore and
    what ignoring the rest costs.

    **4. A denominator-excluding termination reason needs typed evidence.** The
    reasons in
    :data:`~tolokaforge.core.failure_attribution.EXCLUDED_TYPED_REASONS` —
    ``RATE_LIMIT``, ``API_TIMEOUT``, ``EMPTY_COMPLETION``, ``PROVISION_ERROR``
    — take a trial out of the measured denominator *and* leave it with no
    grade, so a loop that reaches one by matching prose against an exception
    message deletes its own failures from the results instead of reporting
    them. An implementation emits one of these reasons only when it reached it
    from an exception type, an HTTP status or a typed empty-completion
    observation, and names that observation in
    :attr:`LoopOutcome.excluding_reason_evidence`. With no such evidence it
    emits :attr:`~tolokaforge.core.models.TerminationReason.ERROR`, which is
    counted. An outcome that claims an excluding reason and carries no evidence
    is downgraded to ``ERROR`` by the caller.

    Routing the exception through ``context.classify_error`` discharges this:
    the returned :class:`TerminationDecision` carries
    :attr:`~TerminationDecision.excluding_reason_evidence` on exactly the typed
    branches, and an implementation copies it onto the outcome beside the
    reason it copies from the same decision. Only a loop that reaches an
    excluding reason on its own — an empty-completion observation of its own
    making, say — words the evidence itself.

    :class:`ToolCallingLoop` satisfies this contract; implementations resolve
    through the ``tolokaforge.agent_loops`` entry-point group.
    """

    def run(self, system_prompt: str, messages: list[Message], start_time: float) -> LoopOutcome:
        """Run the turn cycle, mutating ``messages`` in place.

        ``start_time`` is the ``time.time()`` epoch the episode began at — any
        episode-timeout budget is measured against it. Returns the loop-level
        verdict; the caller assembles the trajectory from ``messages``.
        """
        ...


@dataclass(frozen=True)
class AgentLoopContext:
    """The trial-scoped dependencies an agent-loop factory receives.

    The union of what a loop over the trial's agent may need.

    **Optional to read**: ``request_limiter``, ``normalize_tool_arguments``,
    ``call_observation``, ``observer``, ``validation_schemas_by_tool``,
    ``tool_output_max_chars_by_tool``. Ignoring one costs the run a rate-limit
    bound, an argument-shape repair, a display or tracing signal, or a
    defensive cap — degraded, and visible as such.

    **Not optional**, whatever the type annotations allow:

    ``metrics``
        Every generation's usage and cost must reach the sink. The run's
        accumulated spend is the sum of the trials' ``metrics.cost_usd``, so a
        loop that never feeds it reports zero for every trial and the run's
        cost limit (``compute.max_budget_usd``) never fires however much the
        run actually spends.
    ``should_terminate``
        Called after the assistant message is appended and before tool
        execution. It is the trial's stuck detection; skipping the call
        disables it silently.
    ``user_turn``
        ``None`` only when the trial's turn policy dispatches no user — the
        caller decides that, not the loop. When it is supplied, a loop that
        never calls it runs a ``conversational`` trial agent-only, and per
        ADR-0050 the interaction-mode axis and the loop axis are orthogonal.
    ``recorder`` and ``call_ids``
        The two halves of the join key the :class:`AgentLoop` contract pins:
        the recorder is the trial's ordered tool-call record, and the assigner
        is the episode-wide id sequence both the agent's loop and the trial's
        second actor draw from, so one actor's raw provider id is
        disambiguated rather than recorded twice. A ``None`` ``recorder`` is
        the judge's read-only shape, not a licence to execute tools unrecorded.
    ``agent_view``
        When set, what of the recorded transcript the agent may read: a user
        turn records steps the agent is never sent, so a loop that builds its
        input from ``messages`` passes them through this first. ``None`` reads
        the whole record.
    """

    llm_client: LoopLLMClient
    tool_executor: ToolExecuting
    tool_schemas: list[dict[str, Any]]
    config: LoopConfig
    metrics: MetricsSink
    should_terminate: TerminationPolicy
    logger: StructuredLogger
    classify_error: ErrorClassifier
    call_ids: EpisodeUniqueCallIds
    user_turn: UserTurn | None = None
    recorder: ToolCallRecorder | None = None
    request_limiter: Any | None = None
    normalize_tool_arguments: Callable[[str, dict[str, Any] | None, str], dict[str, Any]] | None = (
        None
    )
    call_observation: LLMCallObservation | None = None
    observer: LoopObserver | None = None
    validation_schemas_by_tool: dict[str, dict[str, Any]] | None = None
    tool_output_max_chars_by_tool: dict[str, int] | None = None
    agent_view: Callable[[list[Message]], list[Message]] | None = None
    simulation_budget: SimulationBudget | None = None


AgentLoopFactory = Callable[[AgentLoopContext], AgentLoop]

_EMPTY_COMPLETION_EVIDENCE = (
    "GenerationResult with empty text and no tool calls, after the empty-completion retry budget"
)
"""Evidence wording for the one excluding reason the loop observes itself.

``EMPTY_COMPLETION`` is reached from the shape of a returned
:class:`~tolokaforge.core.llm.client.GenerationResult`, not from an exception,
so no classifier decision carries its evidence.
"""


def _is_reasoning_only(result: Any) -> bool:
    """Whether an actionless result is one the model deliberated before.

    Two shapes reach here and both are worth another sample, because the
    provider billed output tokens either way:

    * the model spent its whole budget thinking and was cut off before it
      could act — ``finish_reason == "length"``, reasoning filling the
      completion;
    * the model deliberated briefly and then returned nothing of its own
      accord — ``finish_reason == "stop"`` with a few hundred reasoning
      tokens and no text.

    Hence both signals rather than either alone. A result carrying neither is
    one the provider returned nothing for, where resampling buys nothing.

    The reason this is a predicate and not an inferred cause: naming the
    second shape "budget exhausted" would be false, and the counts that tell
    the two apart are recorded on the terminal evidence instead.
    """
    if result.finish_reason == "length":
        return True
    usage = getattr(result, "usage", None)
    return bool(usage is not None and usage.reasoning_tokens)


def _reasoning_stall_evidence(result: Any) -> str:
    """What was actually observed, in the terms the next reader will need.

    Spelled out rather than asserted because the same observation was once
    reported as "the model returned an empty completion", which named the
    wrong party and cost an investigation to correct. ``finish_reason`` and
    the token split are what separate a truncation at the ceiling from a model
    that stopped early, so both ride the evidence.
    """
    usage = getattr(result, "usage", None)
    reasoning_tokens = usage.reasoning_tokens if usage is not None else 0
    completion_tokens = usage.completion_tokens if usage is not None else 0
    return (
        f"GenerationResult with no text and no tool calls after "
        f"{completion_tokens} completion tokens, {reasoning_tokens} of them reasoning "
        f"(finish_reason={result.finish_reason!r}), after the resample budget"
    )


def _now() -> datetime:
    return datetime.now(tz=UTC)


ToolMessageAppender = Callable[[Message], int]
"""Appends a ``role: tool`` message to the caller's message list.

Returns the message's index in that list. The funnel hands that index to
:meth:`~tolokaforge.observability.observer.LoopObserver.tool_call` so a live
span and the bundle's observation name one position.
"""


class UnassignedToolCallError(RuntimeError):
    """A tool call reached :meth:`ToolCallFunnel.execute` carrying an id the
    funnel never assigned.

    :meth:`ToolCallFunnel.assign_ids` is the only source of an executable id:
    the key it returns is what the assistant message, the executor, the
    recorder and the ``role: tool`` message all carry. A call keyed anywhere
    else cannot be joined back to its result at grade time.
    """


@dataclass
class ToolCallFunnel:
    """The one path from a parsed tool call to its executed, recorded result.

    Every obligation the grading path enforces on a tool call and the type
    checker does not — the episode-unique id shared by all four views, the
    :data:`~tolokaforge.core.tool_message_format.TOOL_ERROR_MESSAGE_PREFIX` on
    a failed call's ``role: tool`` content, the recorder entry, the output cap,
    the metrics tick and the observer notification — is discharged here. A loop
    that routes its calls through the funnel satisfies them by construction.

    Two calls per turn, in this order:

    1. :meth:`assign_ids` over the turn's parsed calls. The returned calls are
       the ones the assistant :class:`~tolokaforge.core.models.Message` carries.
    2. :meth:`execute` (or :meth:`execute_all`) once the assistant message is
       appended.

    :meth:`execute` refuses a call whose id did not come from
    :meth:`assign_ids`, so the order cannot be inverted and the id cannot be
    minted elsewhere.

    One funnel per episode, built from the trial's
    :class:`AgentLoopContext` via :meth:`from_context`: it draws from that
    context's assigner, so the trial's second actor disambiguates against the
    same sequence.
    """

    tool_executor: ToolExecuting
    call_ids: EpisodeUniqueCallIds
    metrics: MetricsSink
    logger: StructuredLogger
    recorder: ToolCallRecorder | None = None
    observer: LoopObserver | None = None
    normalize_tool_arguments: Callable[[str, dict[str, Any] | None, str], dict[str, Any]] | None = (
        None
    )
    validation_schemas_by_tool: dict[str, dict[str, Any]] | None = None
    tool_output_max_chars_by_tool: dict[str, int] | None = None
    tool_output_max_chars: int | None = None

    _assigned_ids: set[str] = field(default_factory=set, init=False)

    @classmethod
    def from_context(cls, context: AgentLoopContext) -> ToolCallFunnel:
        """The funnel for a loop built over ``context``."""
        return cls(
            tool_executor=context.tool_executor,
            call_ids=context.call_ids,
            metrics=context.metrics,
            logger=context.logger,
            recorder=context.recorder,
            observer=context.observer,
            normalize_tool_arguments=context.normalize_tool_arguments,
            validation_schemas_by_tool=context.validation_schemas_by_tool,
            tool_output_max_chars_by_tool=context.tool_output_max_chars_by_tool,
            tool_output_max_chars=context.config.tool_output_max_chars,
        )

    def assign_ids(self, calls: Sequence[ToolCall]) -> list[ToolCall]:
        """Give every parsed call the episode-unique id, before anything reads it.

        Returns the calls to put on the assistant message — a call whose
        provider id was already unique is returned unchanged, so a provider
        that mints unique ids sees its own ids back. Call this between the
        generation and the assistant message so all four consumers downstream
        — the assistant message, the executor (hence the runner's own record),
        the trial recorder and the ``role: tool`` message — carry one id per
        call.
        """
        assigned: list[ToolCall] = []
        for call in calls:
            key = self.call_ids.assign(call.id)
            self._assigned_ids.add(key)
            if key == call.id:
                assigned.append(call)
                continue
            self.logger.warning(
                "Provider reused a tool-call id within the episode; assigned a unique one",
                tool=call.name,
                provider_call_id=call.id,
                assigned_call_id=key,
            )
            assigned.append(call.model_copy(update={"id": key}))
        return assigned

    def execute_all(
        self,
        calls: Sequence[ToolCall],
        append_tool_message: ToolMessageAppender,
        assistant_text: str = "",
    ) -> list[ToolResult]:
        """:meth:`execute` over ``calls``, in declaration order."""
        return [self.execute(call, append_tool_message, assistant_text) for call in calls]

    def execute(
        self,
        call: ToolCall,
        append_tool_message: ToolMessageAppender,
        assistant_text: str = "",
    ) -> ToolResult:
        """Execute one assigned call and write everything the call owes.

        ``assistant_text`` is the turn's assistant prose, read only by the
        optional argument-recovery seam.

        Raises:
            UnassignedToolCallError: ``call.id`` did not come from
                :meth:`assign_ids`.
        """
        if call.id not in self._assigned_ids:
            raise UnassignedToolCallError(
                f"tool call {call.id!r} for tool {call.name!r} was not assigned by this funnel: "
                "pass the turn's calls through ToolCallFunnel.assign_ids and put the returned "
                "ids on the assistant message before executing any of them"
            )
        self._recover_arguments(call, assistant_text)
        tool_start = time.time()
        if self.validation_schemas_by_tool is None:
            tool_result = self.tool_executor.execute(call.name, call.arguments, call_id=call.id)
        else:
            tool_result = self.tool_executor.execute(
                call.name,
                call.arguments,
                call_id=call.id,
                validation_schema=self.validation_schemas_by_tool.get(call.name),
            )
        tool_duration = time.time() - tool_start
        self.metrics.record_tool_call()

        if self.recorder is not None:
            self.recorder.record(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments or {},
                executor=ToolExecutorIdentity.AGENT,
                status=resolve_tool_status(tool_result),
                output=resolve_tool_output(tool_result),
                latency_seconds=tool_duration,
            )

        if tool_result.success:
            self.logger.debug(
                "Tool executed successfully", tool=call.name, duration_s=tool_duration
            )
        else:
            self.logger.warning("Tool execution failed", tool=call.name, error=tool_result.error)

        raw_content = (
            tool_result.output
            if tool_result.success
            else f"{TOOL_ERROR_MESSAGE_PREFIX}{resolve_tool_output(tool_result)}"
        )
        message = Message(
            role=MessageRole.TOOL,
            content=self.cap_tool_message_content(call.name, raw_content),
            content_blocks=(tool_result.content_blocks if tool_result.success else None),
            tool_call_id=call.id,
            tool_status=resolve_tool_status(tool_result),
            ts=_now(),
        )
        index = append_tool_message(message)
        if self.observer is not None:
            ended_at = message.ts or _now()
            self.observer.tool_call(
                index=index,
                call=call,
                result=tool_result,
                started_at=ended_at - timedelta(seconds=max(0.0, tool_duration)),
                ended_at=ended_at,
            )
        return tool_result

    def cap_tool_message_content(self, tool_name: str, raw: str) -> str:
        """Apply the tighter tool-output cap to a ``role=tool`` message content.

        Two axes compose here: :attr:`tool_output_max_chars` is the per-model
        backstop and :attr:`tool_output_max_chars_by_tool` carries the tool's
        own declared
        :attr:`~tolokaforge.tools.registry.ToolPolicy.output_max_chars`. The
        tighter set cap wins per call; ``None`` on both axes threads the
        content through verbatim. The recorder read in :meth:`execute` runs
        earlier against the untruncated tool result, so the trial's ordered
        record and the grader inputs are unaffected by the cap.
        """
        tool_cap = (self.tool_output_max_chars_by_tool or {}).get(tool_name)
        cap_cap = self.tool_output_max_chars
        candidates = [x for x in (tool_cap, cap_cap) if x is not None]
        if not candidates:
            return raw
        effective = min(candidates)
        capped, omitted = keep_head_and_tail(raw, effective)
        if omitted:
            self.metrics.record_tool_output_truncated(omitted)
            self.logger.info(
                "Capped tool output before append",
                tool=tool_name,
                cap_chars=effective,
                tool_cap_chars=tool_cap,
                capability_cap_chars=cap_cap,
                omitted_chars=omitted,
                original_chars=len(raw),
            )
        return capped

    def _recover_arguments(self, call: ToolCall, assistant_text: str) -> None:
        if self.normalize_tool_arguments is None:
            return
        normalized_args = self.normalize_tool_arguments(call.name, call.arguments, assistant_text)
        if normalized_args != call.arguments:
            self.logger.warning(
                "Recovered malformed tool arguments from assistant text",
                tool=call.name,
                recovered_keys=sorted(
                    set(normalized_args.keys()) - set((call.arguments or {}).keys())
                ),
            )
            call.arguments = normalized_args


def _without_last_assistant_reasoning(wire: list[Message]) -> list[Message]:
    """*wire* with reasoning dropped from its most recent assistant message.

    Returns the list unchanged when the last assistant message carries no
    reasoning, so a resample on a route that never replays reasoning sends the
    identical object it would have sent anyway.
    """
    for index in range(len(wire) - 1, -1, -1):
        message = wire[index]
        if message.role is not MessageRole.ASSISTANT:
            continue
        if message.reasoning is None:
            return wire
        stripped = list(wire)
        stripped[index] = message.model_copy(update={"reasoning": None})
        return stripped
    return wire


def _opening(wire: list[Message]) -> list[Message]:
    """The head of *wire* a summarize keeps: everything through the first user turn.

    That is the user's opening, and the agent's opening line ahead of it when the
    task declared one (``actors.user.first_agent_message``).
    """
    for index, message in enumerate(wire):
        if message.role is MessageRole.USER:
            return wire[: index + 1]
    return wire[:1]


@dataclass
class ToolCallingLoop:
    """Generic multi-turn tool-calling engine.

    Drives turn structure and delegates every policy decision to its seams. The
    same instance owns no trial state beyond the loop; ``messages`` and the
    metrics sink are supplied by the caller so the agent and the judge can each
    bring their own accumulation target.
    """

    llm_client: LoopLLMClient
    tool_executor: ToolExecuting
    tool_schemas: list[dict[str, Any]]
    config: LoopConfig
    metrics: MetricsSink
    should_terminate: TerminationPolicy
    logger: StructuredLogger
    classify_error: ErrorClassifier
    user_turn: UserTurn | None = None
    # The trial's ordered tool-call record. Injected rather than owned: the
    # rubric judge runs this same loop over its own read-only tools, and a
    # grading-time tool call must never enter the trial's record.
    recorder: ToolCallRecorder | None = None
    request_limiter: Any | None = None
    normalize_tool_arguments: Callable[[str, dict[str, Any] | None, str], dict[str, Any]] | None = (
        None
    )
    call_observation: LLMCallObservation | None = None
    # Live tracing seam (ADR-0047): told about every recorded assistant turn and tool result with
    # the message's position in ``messages``, so a live span and the bundle uploader's observation
    # share one id. ``None`` observes nothing; a raising observer never reaches the loop.
    observer: LoopObserver | None = None
    # Per-tool ``parameters`` schema the model was shown for this loop's tools,
    # keyed by tool name. Wired at construction from
    # :meth:`LLMClient.sanitize_tools_for_execution`. When present, the executor
    # validates arguments against the schema for each call's tool; a call whose
    # tool name is absent from the map falls back to the tool's own declared
    # schema. When the whole field is ``None``, no schema is passed and the
    # executor validates against the tool's declared schema — the path taken by
    # tests that construct the loop without an LLM in scope.
    validation_schemas_by_tool: dict[str, dict[str, Any]] | None = None
    # Per-tool declared ``ToolPolicy.output_max_chars``, keyed by tool name.
    # Callers that construct the loop over a live
    # :class:`~tolokaforge.tools.registry.ToolRegistry` wire this from
    # :meth:`~tolokaforge.tools.registry.ToolRegistry.output_max_chars_by_tool`.
    # Only tools that declare a cap appear; a tool absent from the map defers
    # to :attr:`LoopConfig.tool_output_max_chars`. When both axes name a cap,
    # :meth:`ToolCallFunnel.cap_tool_message_content` picks the tighter one.
    tool_output_max_chars_by_tool: dict[str, int] | None = None
    # Bounded API-error retry sleep seam. Parallels ``LLMClient._retry_sleep``:
    # tests bind a no-op so the loop's retry backoff is instant. See
    # :attr:`LoopConfig.api_error_backoff_s` for the wait, and the retry class
    # rules in the config docstring for which classified reasons trigger it.
    retry_sleep: Callable[[float], None] = time.sleep
    # The episode's id assigner. Injected rather than owned: a trial's second
    # actor executes tool calls outside this loop and must draw from the same
    # sequence, while a rubric judge's loop takes the default and disambiguates
    # only against its own calls.
    call_ids: EpisodeUniqueCallIds = field(default_factory=EpisodeUniqueCallIds)
    # What of the recorded transcript the agent reads, where the loop builds its
    # input from the record rather than from its own wire: the wire's start, the
    # summarizer's input and the observer's view of a generation's request.
    # ``None`` reads the whole record; a trial whose user takes isolated tool
    # steps passes :func:`~tolokaforge.core.actors.tool_turns.agent_view`.
    agent_view: Callable[[list[Message]], list[Message]] | None = None
    simulation_budget: SimulationBudget | None = None

    # The single path every tool call this loop makes travels: id assignment,
    # execution, recording, error wording, output cap, metrics and observer.
    funnel: ToolCallFunnel = field(init=False)

    # Captured from the first generation's effective system prompt.
    _captured_effective_prompt: str | None = field(default=None, init=False)
    _captured: bool = field(default=False, init=False)
    # The typed observation behind this episode's termination reason, where that
    # reason excludes the trial from the measured denominator. Written by the
    # two paths that can reach such a reason — the classifier's decision and the
    # empty-completion observation — and read once, into ``LoopOutcome``.
    _excluding_reason_evidence: str | None = field(default=None, init=False)
    _consecutive_stall_turns: int = field(default=0, init=False)
    """Unbroken run of turns that each stalled on reasoning and then recovered.

    Cross-turn because the resample budgets are not: they are per-turn locals
    re-zeroed every turn, so a model that stalls once per turn, every turn,
    exhausts nothing and the shape is invisible from inside a single turn.

    Counts recoveries, not deaths — a turn whose stall outlives its resample
    ends the trial before reaching here. So this is the "limping along" signal:
    a model paying for deliberation every turn and still moving.
    """
    # Wire message list sent to the provider. Distinct from the caller-owned
    # ``messages`` (which becomes ``Trajectory.messages``) so a summarize event
    # can rewrite the wire view while the recorded history keeps the full
    # pre-summarize timeline for grading.
    _wire_messages: list[Message] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.funnel = ToolCallFunnel(
            tool_executor=self.tool_executor,
            call_ids=self.call_ids,
            metrics=self.metrics,
            logger=self.logger,
            recorder=self.recorder,
            observer=self.observer,
            normalize_tool_arguments=self.normalize_tool_arguments,
            validation_schemas_by_tool=self.validation_schemas_by_tool,
            tool_output_max_chars_by_tool=self.tool_output_max_chars_by_tool,
            tool_output_max_chars=self.config.tool_output_max_chars,
        )

    def run(self, system_prompt: str, messages: list[Message], start_time: float) -> LoopOutcome:
        """Run the turn loop, mutating ``messages`` in place.

        ``start_time`` is the ``time.time()`` epoch the episode began at — the
        episode-timeout budget is measured against it. Returns the loop verdict;
        the caller assembles the trajectory.
        """
        status = TrialStatus.COMPLETED
        termination_reason: TerminationReason | None = None
        self._wire_messages = self._read_as_agent(messages)
        self._excluding_reason_evidence = None
        self._consecutive_stall_turns = 0

        for turn in range(self.config.max_turns):
            outcome = self._attempt_turn(turn, system_prompt, messages, start_time)
            if isinstance(outcome, TerminationDecision):
                self._append_both(messages, self._system_message(outcome.system_message))
                status = outcome.status or status
                termination_reason = outcome.reason
                self._excluding_reason_evidence = outcome.excluding_reason_evidence
                break
            turn_status, turn_reason, stop = outcome
            if turn_status is not None:
                status = turn_status
            if stop:
                termination_reason = turn_reason
                break
        else:
            termination_reason = TerminationReason.MAX_TURNS
            self._append_both(
                messages,
                self._system_message(
                    f"Maximum turns ({self.config.max_turns}) reached. Dialogue terminated."
                ),
            )

        return LoopOutcome(
            status=status,
            termination_reason=termination_reason,
            captured_effective_system_prompt=self._captured_effective_prompt,
            excluding_reason_evidence=self._excluding_reason_evidence,
        )

    def _stop_on(
        self, decision: TerminationDecision
    ) -> tuple[TrialStatus | None, TerminationReason | None, bool]:
        """The turn's stop triple for *decision*, with its evidence kept beside it.

        The triple carries no room for the evidence, so it lands on the loop
        and :meth:`run` reads it into the outcome alongside the reason.
        """
        self._excluding_reason_evidence = decision.excluding_reason_evidence
        return decision.status, decision.reason, True

    def _append_both(self, messages: list[Message], message: Message) -> None:
        """Append to the caller-owned recorded list and the wire list.

        A summarize event rewrites ``_wire_messages`` in place and appends a
        ``role=system`` reset marker to both lists; the pre-summarize turn
        content stays in ``messages`` so grading reads the full history via
        ``Trajectory.messages``.
        """
        messages.append(message)
        self._wire_messages.append(message)

    def _read_as_agent(self, messages: list[Message]) -> list[Message]:
        """A new list of what the agent reads of *messages*; see :attr:`agent_view`."""
        return list(messages) if self.agent_view is None else self.agent_view(messages)

    def _attempt_turn(
        self,
        turn: int,
        system_prompt: str,
        messages: list[Message],
        start_time: float,
    ) -> TerminationDecision | tuple[TrialStatus | None, TerminationReason | None, bool]:
        """Run one turn with the bounded API-error retry budget.

        Returns the turn's ``(status_override, reason, stop)`` triple when the
        attempt produced a result the outer loop should consume. Returns a
        :class:`TerminationDecision` when the outer loop must stop — either
        because the episode wall-time budget is spent, or because a classified
        exception cannot be recovered by another attempt.

        The API-error retry budget resets to zero on every call, so a
        successful turn followed by an API-error turn gets a fresh budget.
        Only :attr:`TerminationReason.API_ERROR` triggers a retry at this
        layer: rate limits, API timeouts and trial-lost stay one-shot
        terminal. Empty completions, content-carrying max-tokens truncations
        and un-parseable tool_call arguments are resampled inside
        :meth:`_run_turn` under their own budgets
        (:attr:`LoopConfig.empty_retry_count`,
        :attr:`LoopConfig.output_length_retry_count` and
        :attr:`LoopConfig.parser_error_retry_count`), so the four retry
        classes stay orthogonal — the API-error retry replays a raised
        exception, the empty-completion retry resamples a returned
        empty-shape result, the output-length retry appends a ``role=user``
        feedback turn and resamples a returned truncated content-carrying
        result before falling through to accept-and-continue, and the
        parser-error retry appends a ``role=user`` feedback turn naming the
        failing tools and resamples before falling through to accept the
        ``{}``-coerced response.
        """
        api_error_attempts = 0
        while True:
            timeout_decision = self._check_episode_timeout(start_time)
            if timeout_decision is not None:
                return timeout_decision
            try:
                return self._run_turn(turn, system_prompt, messages)
            except Exception as exc:  # noqa: BLE001 — classified, then surfaced via status
                self.logger.error(
                    "Error during turn execution",
                    turn=turn,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                decision = self.classify_error(exc)
                if (
                    decision.reason is TerminationReason.API_ERROR
                    and api_error_attempts < self.config.api_error_retries
                ):
                    api_error_attempts += 1
                    self.logger.info(
                        "Retrying turn after classified API error",
                        turn=turn,
                        attempt=api_error_attempts,
                        max_attempts=self.config.api_error_retries + 1,
                        backoff_s=self.config.api_error_backoff_s,
                    )
                    self.retry_sleep(self.config.api_error_backoff_s)
                    continue
                return decision

    def _run_turn(
        self, turn: int, system_prompt: str, messages: list[Message]
    ) -> tuple[TrialStatus | None, TerminationReason | None, bool]:
        """Execute a single turn. Returns ``(status_override, reason, stop)``.

        ``stop`` requests the loop break with ``reason``. A non-``None``
        ``status_override`` promotes the trial status. Raised exceptions are
        classified by the caller.
        """
        summarize_decision = self._maybe_summarize(turn, system_prompt, messages)
        if summarize_decision is not None:
            self._append_both(messages, self._system_message(summarize_decision.system_message))
            return self._stop_on(summarize_decision)

        empty_attempts = 0
        output_length_attempts = 0
        reasoning_stall_attempts = 0
        parser_error_attempts = 0
        # Set by the reasoning-stall branch for the one resample that follows
        # it, and cleared as soon as that call is made: the suppression is a
        # way out of a stall, not a standing change to what the model sees.
        suppress_replay = False
        stalled_this_turn = False
        while True:
            # Cleared before the call, not after: a raise here is still the
            # one resample the suppression was for, and leaving the flag set
            # would silently suppress replay on the following turn instead.
            replay_reasoning = not suppress_replay
            suppress_replay = False
            try:
                result = self._generate(turn, system_prompt, replay_reasoning=replay_reasoning)
            except litellm.exceptions.ContextWindowExceededError:
                reactive_decision = self._maybe_reactive_summarize(turn, system_prompt, messages)
                if reactive_decision is not None:
                    self._append_both(
                        messages, self._system_message(reactive_decision.system_message)
                    )
                    return self._stop_on(reactive_decision)
                if not self._summarize_armed():
                    raise
                try:
                    result = self._generate(turn, system_prompt)
                except litellm.exceptions.ContextWindowExceededError as retry_exc:
                    self._append_both(
                        messages,
                        self._system_message(
                            "Context window exceeded on the post-summarize retry: "
                            f"{retry_exc}. Dialogue terminated."
                        ),
                    )
                    return (
                        TrialStatus.FAILED,
                        TerminationReason.CONTEXT_WINDOW_EXCEEDED,
                        True,
                    )
            result.tool_calls = self.funnel.assign_ids(result.tool_calls)
            self._capture_effective_prompt(result)
            self.metrics.record_generation(result)
            if result.parser_errors:
                self.metrics.record_parser_errors(result.parser_errors)
            self._log_generation(turn, result)

            if result.text or result.tool_calls:
                if (
                    result.parser_errors
                    and parser_error_attempts < self.config.parser_error_retry_count
                ):
                    parser_error_attempts += 1
                    self._append_both(
                        messages,
                        Message(
                            role=MessageRole.USER,
                            content=self._format_parser_error_feedback(result.parser_errors),
                            ts=_now(),
                        ),
                    )
                    self.logger.info(
                        "Resampling after tool_call argument parse errors",
                        turn=turn,
                        attempt=parser_error_attempts,
                        max_attempts=self.config.parser_error_retry_count + 1,
                        tool_names=[e.tool_name for e in result.parser_errors],
                    )
                    continue
                if (
                    result.finish_reason == "length"
                    and output_length_attempts < self.config.output_length_retry_count
                ):
                    output_length_attempts += 1
                    self._append_both(
                        messages,
                        Message(
                            role=MessageRole.USER,
                            content=(
                                "The previous response was truncated at max_tokens "
                                "(finish_reason: length). Please split the next "
                                "action into smaller pieces (fewer tool calls per "
                                "turn, shorter text) and try again."
                            ),
                            ts=_now(),
                        ),
                    )
                    self.logger.info(
                        "Resampling after truncated completion",
                        turn=turn,
                        attempt=output_length_attempts,
                        max_attempts=self.config.output_length_retry_count + 1,
                    )
                    continue
                break

            if _is_reasoning_only(result):
                # The same truncation that arrives with a token of text falls
                # through to ``break`` above and the trial carries on. Arriving
                # without one is the worse case, so it gets the same resample
                # the content-carrying path has rather than ending the trial.
                stalled_this_turn = True
                if reasoning_stall_attempts < self.config.reasoning_stall_retry_count:
                    reasoning_stall_attempts += 1
                    suppress_replay = True
                    self._append_both(
                        messages,
                        Message(
                            role=MessageRole.USER,
                            content=(
                                "The previous response contained reasoning but no action. "
                                "Keep deliberation short and reply with a tool call."
                            ),
                            ts=_now(),
                        ),
                    )
                    self.logger.info(
                        "Resampling after reasoning-only truncation",
                        turn=turn,
                        attempt=reasoning_stall_attempts,
                        max_attempts=self.config.reasoning_stall_retry_count + 1,
                        reasoning_tokens=(
                            result.usage.reasoning_tokens if result.usage is not None else 0
                        ),
                        finish_reason=result.finish_reason,
                        replaying_reasoning=False,
                    )
                    continue

                self._append_both(
                    messages,
                    self._system_message(
                        "Model returned reasoning but no text and no tool call; trial "
                        "terminated to keep the next request provider-legal."
                    ),
                )
                self._excluding_reason_evidence = _reasoning_stall_evidence(result)
                return (
                    TrialStatus.FAILED,
                    TerminationReason.REASONING_WITHOUT_ACTION,
                    True,
                )

            if empty_attempts >= self.config.empty_retry_count:
                self._append_both(
                    messages,
                    self._system_message(
                        "Model returned an empty completion (no text, no tool calls); "
                        "trial terminated to keep the next request provider-legal."
                    ),
                )
                self._excluding_reason_evidence = _EMPTY_COMPLETION_EVIDENCE
                return TrialStatus.FAILED, TerminationReason.EMPTY_COMPLETION, True

            empty_attempts += 1
            self.logger.info(
                "Resampling after provider-side empty completion",
                turn=turn,
                attempt=empty_attempts,
                max_attempts=self.config.empty_retry_count + 1,
            )

        # The turn produced an action. Fold it into the cross-turn count before
        # anything else reads it: a run of stalling turns is only interesting
        # while it is unbroken, and a productive turn breaks it.
        if stalled_this_turn:
            self._consecutive_stall_turns += 1
            self.logger.info(
                "Turn recovered from a reasoning-only stall",
                turn=turn,
                consecutive_stall_turns=self._consecutive_stall_turns,
            )
        else:
            self._consecutive_stall_turns = 0

        self._append_both(messages, self._assistant_message(result))
        if self.observer is not None:
            ended_at = messages[-1].ts or _now()
            self.observer.generation(
                index=len(messages) - 1,
                turn=turn,
                request=self._read_as_agent(messages[:-1]),
                result=result,
                started_at=ended_at - timedelta(seconds=max(0.0, result.latency_s or 0.0)),
                ended_at=ended_at,
            )
        # After the observer, so a turn the budget ends still emits its generation span
        # like every other ending of the turn.
        if self.simulation_budget is not None:
            reason = self.simulation_budget.participant(calls_environment=bool(result.tool_calls))
            if reason is not None:
                return self._stop_for_simulation_limit(messages, reason)

        # Checked after the turn is recorded, not at the point the counter
        # moved. This turn produced an action — ``record_generation`` has
        # already counted it — so returning before the append would leave the
        # bundle with a generation in its metrics and no message to match.
        limit = self.config.reasoning_stall_turn_limit
        if stalled_this_turn and limit and self._consecutive_stall_turns >= limit:
            self._append_both(
                messages,
                self._system_message(
                    f"Model stalled on reasoning in {self._consecutive_stall_turns} "
                    "consecutive turns; trial terminated."
                ),
            )
            self._excluding_reason_evidence = (
                f"{self._consecutive_stall_turns} consecutive turns each contained a "
                f"generation with no text and no tool calls that billed reasoning "
                f"tokens, against a limit of {limit}"
            )
            return (
                TrialStatus.FAILED,
                TerminationReason.REASONING_WITHOUT_ACTION,
                True,
            )

        decision = self.should_terminate(result, turn, messages)
        if decision is not None:
            self._append_both(messages, self._system_message(decision.system_message))
            return self._stop_on(decision)

        if result.tool_calls:
            batch_start = len(messages)
            try:
                results = self._execute_tool_calls(result, messages)
            except Exception:
                if self.simulation_budget is not None:
                    # The batch answered this turn's calls, so it is still the one
                    # environment step that closes the agent's step; left pending, an
                    # API-error retry of the turn would hit the budget's "participant
                    # replied before the pending environment batch" refusal and mask
                    # the raised error. Counted with the environment errors of the
                    # calls answered before the raise; a limit it reaches is enforced
                    # at the budget's next check rather than over the raised error.
                    self.simulation_budget.environment(
                        errors=sum(
                            message.tool_status is ToolExecutionStatus.ENVIRONMENT_ERROR
                            for message in messages[batch_start:]
                        )
                    )
                raise
            if self.simulation_budget is not None:
                errors = sum(
                    resolve_tool_status(tool_result) is ToolExecutionStatus.ENVIRONMENT_ERROR
                    for tool_result in results
                )
                reason = self.simulation_budget.environment(errors=errors)
                if reason is not None:
                    return self._stop_for_simulation_limit(messages, reason)
            return None, None, False

        return self._advance_user_turn(messages)

    def _summarize_armed(self) -> bool:
        return (
            self.config.max_context_tokens is not None
            and self.config.context_watermark is not None
            and self.config.summarize_policy is not None
        )

    def _maybe_summarize(
        self, turn: int, system_prompt: str, messages: list[Message]
    ) -> TerminationDecision | None:
        """Rewrite ``_wire_messages`` when the previous turn crossed the watermark.

        Returns a :class:`TerminationDecision` when summarize was fired and
        failed loud (empty recap, or the summarize call itself raised
        :class:`~litellm.exceptions.ContextWindowExceededError`). Returns
        ``None`` when summarize was not armed, when the watermark was not
        crossed, or when summarize succeeded and the loop should continue.
        """
        if not self._summarize_armed():
            return None
        last = self.metrics.last_prompt_tokens
        if last is None:
            return None
        watermark = self.config.context_watermark
        max_ctx = self.config.max_context_tokens
        if last + watermark < max_ctx:
            return None
        return self._perform_summarize(
            turn=turn,
            system_prompt=system_prompt,
            messages=messages,
            trigger=(
                f"pre-turn watermark (prev prompt_tokens={last}, watermark={watermark}, "
                f"max_context={max_ctx})"
            ),
        )

    def _maybe_reactive_summarize(
        self, turn: int, system_prompt: str, messages: list[Message]
    ) -> TerminationDecision | None:
        """Summarize in response to a raised
        :class:`~litellm.exceptions.ContextWindowExceededError`.

        Returns a :class:`TerminationDecision` on loud-fail, or ``None`` when
        the caller should retry ``_generate`` once inline. Returns ``None``
        with no side effect when summarize is not armed — the caller
        propagates the exception so the classifier handles it.
        """
        if not self._summarize_armed():
            return None
        return self._perform_summarize(
            turn=turn,
            system_prompt=system_prompt,
            messages=messages,
            trigger="reactive context_window_exceeded",
        )

    def _perform_summarize(
        self,
        *,
        turn: int,
        system_prompt: str,
        messages: list[Message],
        trigger: str,
    ) -> TerminationDecision | None:
        assert self.config.summarize_policy is not None  # gated by _summarize_armed
        policy = self.config.summarize_policy
        self.logger.info("Summarizing wire history", turn=turn, trigger=trigger)
        try:
            recap = policy.summarize(system_prompt, self._read_as_agent(messages))
        except litellm.exceptions.ContextWindowExceededError as exc:
            return TerminationDecision(
                reason=TerminationReason.CONTEXT_WINDOW_EXCEEDED,
                system_message=(
                    "Summarize call itself exceeded the context window "
                    f"({trigger}): {exc}. Dialogue terminated."
                ),
                status=TrialStatus.FAILED,
            )
        except SummarizerFailedError as exc:
            return TerminationDecision(
                reason=TerminationReason.CONTEXT_WINDOW_EXCEEDED,
                system_message=(
                    f"Summarize policy produced no recap ({trigger}): {exc}. Dialogue terminated."
                ),
                status=TrialStatus.FAILED,
            )
        self._wire_messages = [
            *_opening(self._wire_messages),
            Message(role=MessageRole.USER, content=recap, ts=_now()),
        ]
        marker = self._system_message(
            f"Context summarized before turn {turn} ({trigger}); wire history reset."
        )
        self._wire_messages.append(marker)
        messages.append(marker)
        return None

    def _generate(
        self, turn: int, system_prompt: str, *, replay_reasoning: bool = True
    ) -> GenerationResult:
        """One agent call. ``replay_reasoning=False`` sends the history with the
        deliberation that produced the actionless turn stripped off the wire.

        A resample that replays the reasoning which just produced an actionless
        turn is close to re-rolling the same dice, and measurably behaves like
        it. Only that one message is stripped: earlier assistant turns reasoned
        their way into actions, so their deliberation is not what is being
        re-rolled, and rewriting them would change every byte of the prompt
        after the first — on a route whose codec replays reasoning, that
        discards the provider's cached prefix and bills the whole context
        afresh. Only the wire copy is touched; the recorded messages the grader
        reads keep their reasoning, so nothing leaves the trajectory.
        """
        self.logger.debug("Requesting agent response", turn=turn)
        if self.request_limiter is not None:
            self.request_limiter.acquire()
        wire = self._wire_messages
        if not replay_reasoning:
            wire = _without_last_assistant_reasoning(wire)
        return self.llm_client.generate(
            system=system_prompt,
            messages=wire,
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
        if outcome.message is not None:
            self._append_both(messages, outcome.message)
            if self.simulation_budget is not None:
                reason = self.simulation_budget.participant(
                    calls_environment=bool(outcome.message.tool_calls)
                )
                if reason is not None:
                    return self._stop_for_simulation_limit(messages, reason)

        if outcome.termination is not None:
            self._append_both(messages, self._system_message(outcome.termination.system_message))
            return self._stop_on(outcome.termination)
        return None, None, False

    def _execute_tool_calls(
        self, result: GenerationResult, messages: list[Message]
    ) -> list[ToolResult]:
        return self.funnel.execute_all(
            result.tool_calls,
            lambda message: self._append_tool_message(messages, message),
            result.text,
        )

    def _stop_for_simulation_limit(
        self, messages: list[Message], reason: TerminationReason
    ) -> tuple[TrialStatus | None, TerminationReason | None, bool]:
        decision = TerminationDecision(
            reason=reason,
            system_message=f"Simulation ended at {reason.value}.",
        )
        self._append_both(messages, self._system_message(decision.system_message))
        return self._stop_on(decision)

    def _append_tool_message(self, messages: list[Message], message: Message) -> int:
        """Append a ``role: tool`` message to both views; its index in ``messages``."""
        self._append_both(messages, message)
        return len(messages) - 1

    def _check_episode_timeout(self, start_time: float) -> TerminationDecision | None:
        return episode_timeout_decision(start_time, self.config.episode_timeout_s, self.logger)

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
            openrouter_generation_id=result.openrouter_generation_id,
            ts=_now(),
        )

    @staticmethod
    def _system_message(content: str) -> Message:
        return Message(role=MessageRole.SYSTEM, content=content, ts=_now())

    @staticmethod
    def _format_parser_error_feedback(errors: tuple[ParserError, ...]) -> str:
        """Render the ``role=user`` feedback body for the parser-error retry.

        Lists every failing tool_call so a multi-error response does not
        silently drop one of the parse failures — the model would otherwise
        re-emit whichever one it did not see. Kept as a static method so a
        unit test can call it directly with a synthesised
        :class:`ParserError` tuple.
        """
        lines = [
            "The previous response had tool_call argument parse errors and could not be executed:"
        ]
        for e in errors:
            lines.append(f"- tool={e.tool_name!r}: {e.reason}. Raw arguments: {e.raw_arguments!r}")
        lines.append(
            "Please fix these issues and provide valid JSON arguments "
            "for each tool call, then try again."
        )
        return "\n".join(lines)


def _engine_loop_factory(context: AgentLoopContext) -> ToolCallingLoop:
    """Build the engine's built-in tool-calling loop.

    Registered as ``engine-loop`` in the ``tolokaforge.agent_loops``
    entry-point group.
    """
    return ToolCallingLoop(
        llm_client=context.llm_client,
        tool_executor=context.tool_executor,
        tool_schemas=context.tool_schemas,
        config=context.config,
        metrics=context.metrics,
        should_terminate=context.should_terminate,
        logger=context.logger,
        classify_error=context.classify_error,
        user_turn=context.user_turn,
        recorder=context.recorder,
        request_limiter=context.request_limiter,
        normalize_tool_arguments=context.normalize_tool_arguments,
        call_observation=context.call_observation,
        observer=context.observer,
        validation_schemas_by_tool=context.validation_schemas_by_tool,
        tool_output_max_chars_by_tool=context.tool_output_max_chars_by_tool,
        call_ids=context.call_ids,
        agent_view=context.agent_view,
        simulation_budget=context.simulation_budget,
    )
