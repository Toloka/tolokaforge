"""The scripted substrate one conformance episode runs against.

Every dependency an :class:`~tolokaforge.core.loop.AgentLoopContext` names is
either a deterministic script with a call log, or the engine's own production
object. The split is deliberate, and it is the reason the suite can observe the
defects it is written against:

* **Scripted, with a call log** — the generate seam, the tool executor, the
  metrics sink, the termination policy and the user turn. Each returns a fixed
  sequence and records what it was asked for, so an obligation phrased as "the
  loop calls X once per turn, before Y" becomes an assertion over two lists.
* **Real, never faked** — :class:`~tolokaforge.core.tool_call_ids.EpisodeUniqueCallIds`
  and :class:`~tolokaforge.core.runner.TrialToolCallRecorder`. The id-join
  obligation is a property of the *assigner's* derivation meeting the grading
  path's re-derivation; a fake assigner that hands back what it was given makes
  every loop look conformant, including one that never assigns at all.

The fixtures carry failure knobs rather than requiring a real substrate to reach
a failure branch: :attr:`RecordingToolExecutor.fail_call_indices` fails a chosen
call, :attr:`ScriptedLLMClient.raise_at_call` raises a chosen exception instead
of generating, and :attr:`ScriptedUserTurn` can terminate instead of replying.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tolokaforge.core.llm.client import GenerationResult, ParserError, Usage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.loop import (
    AgentLoopContext,
    LoopConfig,
    TerminationDecision,
    UserTurnResult,
    classify_loop_error,
)
from tolokaforge.core.models import Message, MessageRole, TerminationReason, ToolCall, TrialStatus
from tolokaforge.core.run_display_events import LLMCallObservation
from tolokaforge.core.runner import TrialToolCallRecorder
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.tools.registry import ToolResult

__all__ = [
    "EpisodeHarness",
    "GenerationCall",
    "RecordingMetricsSink",
    "RecordingTerminationPolicy",
    "RecordingToolExecutor",
    "ScriptedLLMClient",
    "ScriptedUserTurn",
    "TerminationCall",
    "ToolExecution",
    "UserTurnCall",
    "assistant_turn",
    "stop_when_no_tool_calls",
    "tool_call",
    "tool_output_for",
]


def tool_call(name: str, arguments: dict[str, Any], call_id: str) -> ToolCall:
    """A provider-shaped tool call. ``call_id`` is the *raw* provider id.

    A conformance script hands out raw ids freely, including ids that repeat
    within one episode — the ``<tool>:<index within the turn>`` shape some
    providers emit. Turning those into episode-unique keys is the loop's job,
    through ``context.call_ids``.
    """
    return ToolCall(id=call_id, name=name, arguments=arguments)


def assistant_turn(
    text: str = "",
    tool_calls: Sequence[ToolCall] = (),
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    cost_usd: float | None = 0.001,
    finish_reason: str | None = None,
    parser_errors: Sequence[ParserError] = (),
) -> GenerationResult:
    """One scripted model turn, priced so a sink that drops it reads as zero."""
    result = GenerationResult(
        text=text,
        tool_calls=list(tool_calls),
        usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        cost_usd=cost_usd,
        finish_reason=finish_reason,
    )
    result.parser_errors = tuple(parser_errors)
    return result


def tool_output_for(tool_name: str, arguments: dict[str, Any]) -> str:
    """The output :class:`RecordingToolExecutor` returns for one call.

    A pure function of the call's own name and arguments, so a result that
    reaches the wrong call is visible in the text rather than only in an id.
    Two calls to one tool with different arguments produce different outputs —
    which is what makes same-tool-twice attribution observable at all.
    """
    return f"{tool_name} -> {json.dumps(arguments, sort_keys=True)}"


@dataclass(frozen=True)
class GenerationCall:
    """One ``generate`` the loop asked for."""

    system: str
    messages: tuple[Message, ...]
    tools: tuple[dict[str, Any], ...]
    tool_choice: str


class ScriptedLLMClient:
    """A :class:`~tolokaforge.core.loop.LoopLLMClient` over a fixed sequence.

    Serves ``script`` in order. Past the end it serves ``exhausted`` — a
    tool-call-free turn by default — so a loop under test for a turn or time
    bound runs against something instead of an exception whose traceback would
    mask the bound it was meant to hit.

    Failure knob: ``raise_at_call`` maps a zero-based call index to the
    exception raised instead of generating.
    """

    def __init__(
        self,
        script: Sequence[GenerationResult],
        *,
        exhausted: Callable[[], GenerationResult] | None = None,
        raise_at_call: dict[int, Exception] | None = None,
    ) -> None:
        self._script = list(script)
        self._exhausted = exhausted or (lambda: assistant_turn(text="nothing further to do"))
        self._raise_at_call = dict(raise_at_call or {})
        self.calls: list[GenerationCall] = []

    @property
    def call_count(self) -> int:
        """How many times the loop asked for a generation, raises included."""
        return len(self.calls)

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        index = len(self.calls)
        self.calls.append(
            GenerationCall(
                system=system,
                messages=tuple(messages),
                tools=tuple(tools),
                tool_choice=tool_choice,
            )
        )
        if index in self._raise_at_call:
            raise self._raise_at_call[index]
        if index < len(self._script):
            return self._script[index]
        return self._exhausted()


@dataclass(frozen=True)
class ToolExecution:
    """One ``execute`` the loop asked for, in execution order."""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str


class RecordingToolExecutor:
    """A :class:`~tolokaforge.tools.registry.ToolExecuting` that logs and can fail.

    Returns :func:`tool_output_for` on success. Failure knob:
    ``fail_call_indices`` names the zero-based execution positions that come
    back as a failed :class:`~tolokaforge.tools.registry.ToolResult` carrying
    ``error_text``.
    """

    def __init__(
        self,
        *,
        fail_call_indices: Sequence[int] = (),
        error_text: str = "the tool refused the call",
    ) -> None:
        self._fail_call_indices = set(fail_call_indices)
        self._error_text = error_text
        self.executions: list[ToolExecution] = []

    @property
    def error_text(self) -> str:
        return self._error_text

    def failed_call_ids(self) -> tuple[str, ...]:
        """The ids of the calls this executor was told to fail, in order."""
        return tuple(
            execution.call_id
            for index, execution in enumerate(self.executions)
            if index in self._fail_call_indices
        )

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        call_id: str,
        validation_schema: dict[str, Any] | None = None,
    ) -> ToolResult:
        index = len(self.executions)
        self.executions.append(
            ToolExecution(tool_name=tool_name, arguments=dict(arguments or {}), call_id=call_id)
        )
        if index in self._fail_call_indices:
            return ToolResult(success=False, output="", error=self._error_text)
        return ToolResult(success=True, output=tool_output_for(tool_name, arguments or {}))


class RecordingMetricsSink:
    """A :class:`~tolokaforge.core.loop.MetricsSink` that keeps every call.

    ``cost_usd`` is summed the way the run's budget cap sums it, so a loop that
    never feeds the sink leaves the total at zero — the shape that makes
    ``compute.max_budget_usd`` unreachable.
    """

    def __init__(self) -> None:
        self.generations: list[GenerationResult] = []
        self.tool_calls = 0
        self.truncations: list[int] = []
        self.parser_errors: list[tuple[ParserError, ...]] = []
        self._last_prompt_tokens: int | None = None

    @property
    def cost_usd(self) -> float:
        return sum(result.cost_usd or 0.0 for result in self.generations)

    @property
    def last_prompt_tokens(self) -> int | None:
        return self._last_prompt_tokens

    def record_generation(self, result: GenerationResult) -> None:
        self.generations.append(result)
        self._last_prompt_tokens = result.usage.prompt_tokens

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_tool_output_truncated(self, omitted_chars: int) -> None:
        self.truncations.append(omitted_chars)

    def record_parser_errors(self, errors: tuple[ParserError, ...]) -> None:
        self.parser_errors.append(errors)


@dataclass(frozen=True)
class TerminationCall:
    """One ``should_terminate`` invocation, with what was visible at the time.

    ``executions_before`` is the executor's call count at the moment the policy
    ran. The obligation is "after the assistant message is appended and before
    tools execute", and both halves are read off this record: ``last_role`` says
    the message landed first, ``executions_before`` says the tools had not.
    """

    turn: int
    message_count: int
    last_role: MessageRole | None
    last_declared_call_ids: tuple[str, ...]
    result_call_ids: tuple[str, ...]
    executions_before: int


class RecordingTerminationPolicy:
    """A :class:`~tolokaforge.core.loop.TerminationPolicy` with a call log.

    ``decide`` is the wrapped policy — ``None`` by default, which continues the
    loop. The log is written before ``decide`` runs, so a decision that stops
    the loop still leaves its own invocation observable.
    """

    def __init__(
        self,
        executor: RecordingToolExecutor,
        decide: (
            Callable[[GenerationResult, int, list[Message]], TerminationDecision | None] | None
        ) = None,
    ) -> None:
        self._executor = executor
        self._decide = decide
        self.calls: list[TerminationCall] = []

    def __call__(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None:
        last = messages[-1] if messages else None
        self.calls.append(
            TerminationCall(
                turn=turn,
                message_count=len(messages),
                last_role=last.role if last is not None else None,
                last_declared_call_ids=(
                    tuple(call.id for call in (last.tool_calls or ())) if last is not None else ()
                ),
                result_call_ids=tuple(call.id for call in result.tool_calls),
                executions_before=len(self._executor.executions),
            )
        )
        if self._decide is None:
            return None
        return self._decide(result, turn, messages)


def stop_when_no_tool_calls(
    result: GenerationResult, turn: int, messages: list[Message]
) -> TerminationDecision | None:
    """Termination policy that ends the episode on the first tool-call-free turn.

    Stands in for whatever real stuck / submit detection a trial wires, so a
    conformance episode ends on its own script rather than on ``max_turns``.
    """
    if result.tool_calls:
        return None
    return TerminationDecision(
        reason=TerminationReason.AGENT_DONE,
        system_message="Scripted episode reached its final turn.",
        status=TrialStatus.COMPLETED,
    )


@dataclass(frozen=True)
class UserTurnCall:
    """One ``user_turn`` invocation."""

    message_count: int
    last_role: MessageRole | None


class ScriptedUserTurn:
    """A :class:`~tolokaforge.core.loop.UserTurn` over a fixed reply sequence.

    Serves ``replies`` in order; past the end it terminates the episode, so a
    loop that calls it more often than the script allows still halts. Failure
    knob: a scripted entry may itself be a
    :class:`~tolokaforge.core.loop.TerminationDecision`.
    """

    def __init__(self, replies: Sequence[str | TerminationDecision] = ()) -> None:
        self._replies = list(replies)
        self.calls: list[UserTurnCall] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, messages: list[Message]) -> UserTurnResult:
        index = len(self.calls)
        last = messages[-1] if messages else None
        self.calls.append(
            UserTurnCall(
                message_count=len(messages),
                last_role=last.role if last is not None else None,
            )
        )
        if index >= len(self._replies):
            return UserTurnResult(
                termination=TerminationDecision(
                    reason=TerminationReason.USER_STOP,
                    system_message="Scripted user turn exhausted.",
                    status=TrialStatus.COMPLETED,
                )
            )
        reply = self._replies[index]
        if isinstance(reply, TerminationDecision):
            return UserTurnResult(termination=reply)
        return UserTurnResult(message=Message(role=MessageRole.USER, content=reply))


@dataclass
class EpisodeHarness:
    """One episode's dependencies, assembled and observable.

    ``context()`` produces the :class:`~tolokaforge.core.loop.AgentLoopContext`
    a factory under test is called with; every field on it is one of the
    attributes here, so an assertion reads the same object the loop was handed.
    """

    llm: ScriptedLLMClient
    executor: RecordingToolExecutor
    metrics: RecordingMetricsSink
    should_terminate: RecordingTerminationPolicy
    call_ids: EpisodeUniqueCallIds
    recorder: TrialToolCallRecorder
    config: LoopConfig
    logger: StructuredLogger
    user_turn: ScriptedUserTurn | None = None
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        script: Sequence[GenerationResult],
        *,
        config: LoopConfig | None = None,
        fail_call_indices: Sequence[int] = (),
        user_turn: ScriptedUserTurn | None = None,
        decide: (
            Callable[[GenerationResult, int, list[Message]], TerminationDecision | None] | None
        ) = stop_when_no_tool_calls,
        raise_at_call: dict[int, Exception] | None = None,
        tool_schemas: Sequence[dict[str, Any]] = (),
    ) -> EpisodeHarness:
        executor = RecordingToolExecutor(fail_call_indices=fail_call_indices)
        return cls(
            llm=ScriptedLLMClient(script, raise_at_call=raise_at_call),
            executor=executor,
            metrics=RecordingMetricsSink(),
            should_terminate=RecordingTerminationPolicy(executor, decide),
            call_ids=EpisodeUniqueCallIds(),
            recorder=TrialToolCallRecorder(),
            config=config or LoopConfig(max_turns=8, episode_timeout_s=600),
            logger=StructuredLogger("agent-loop-conformance"),
            user_turn=user_turn,
            tool_schemas=list(tool_schemas),
        )

    def context(self) -> AgentLoopContext:
        return AgentLoopContext(
            llm_client=self.llm,
            tool_executor=self.executor,
            tool_schemas=self.tool_schemas,
            config=self.config,
            metrics=self.metrics,
            should_terminate=self.should_terminate,
            logger=self.logger,
            classify_error=lambda exc: classify_loop_error(exc, ()),
            call_ids=self.call_ids,
            user_turn=self.user_turn,
            recorder=self.recorder,
        )

    @staticmethod
    def declared_calls(messages: Sequence[Message]) -> tuple[ToolCall, ...]:
        """Every tool call the assistant messages declare, in declaration order."""
        return tuple(
            call
            for message in messages
            if message.role is MessageRole.ASSISTANT
            for call in (message.tool_calls or ())
        )

    @staticmethod
    def tool_messages_by_call_id(messages: Sequence[Message]) -> dict[str, Message]:
        """The ``role: tool`` message answering each call, keyed by its id."""
        return {
            message.tool_call_id: message
            for message in messages
            if message.role is MessageRole.TOOL and message.tool_call_id
        }
