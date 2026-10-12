"""The in-memory reference :class:`~tolokaforge.core.loop.AgentLoop`.

:class:`InMemoryAgentLoop` is the shortest loop that satisfies every obligation
:class:`~tolokaforge.core.loop.AgentLoop` declares. It runs no provider, reaches
no substrate and holds no trial state beyond one episode, so an implementer can
read the whole turn cycle in one screen and copy what is not optional: the
:class:`~tolokaforge.core.loop.ToolCallFunnel` every tool call travels through,
the sink feed, the two budgets, and the classifier's evidence carried onto the
outcome beside its reason.

It is also the suite's own control. Every defect in :class:`LoopDefects`
switches off exactly one obligation, which is what lets
``tests/canonical/test_agent_loop_contract.py`` show each conformance
assertion failing on the loop that violates the assertion it is written for. A
conformance kit nothing can fail is a kit that proves nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.loop import (
    AgentLoopContext,
    LoopOutcome,
    TerminationDecision,
    ToolCallFunnel,
)
from tolokaforge.core.models import (
    Message,
    MessageRole,
    TerminationReason,
    ToolCall,
    ToolExecutorIdentity,
    TrialStatus,
)
from tolokaforge.core.tool_message_format import TOOL_ERROR_MESSAGE_PREFIX
from tolokaforge.tools.registry import resolve_tool_output, resolve_tool_status

__all__ = [
    "InMemoryAgentLoop",
    "InMemoryAgentLoopCallLog",
    "LoopDefects",
    "in_memory_agent_loop_factory",
]


@dataclass(frozen=True)
class LoopDefects:
    """One switch per obligation, each defaulting to "honoured".

    Every field turns off a single contract obligation without breaking the
    loop, which is the point: each produces a plausible-looking trajectory and a
    wrong grade rather than a crash, and each is the vector one conformance
    assertion exists to catch.
    """

    raw_provider_call_ids: bool = False
    """Key calls by the provider's raw id instead of ``context.call_ids``.

    Also takes the turn off the funnel: the funnel refuses to execute a call it
    did not key, so a loop that mints its own ids is a loop that hand-rolls the
    whole tool-call path.
    """

    execute_in_reverse: bool = False
    """Execute a turn's calls in reverse declaration order.

    Same tool, same turn, different arguments: the two views then derive their
    episode-unique keys from disagreeing orders and each result is attributed to
    the other call's declaration — silently, because both name one tool.
    """

    plain_tool_error_text: bool = False
    """Word a failed call's ``role: tool`` message without the error prefix.

    The funnel always writes the prefix, so this too hand-rolls the tool-call
    path rather than routing through it.
    """

    skip_metrics: bool = False
    """Never feed the metrics sink, so the trial's cost reads as zero."""

    skip_should_terminate: bool = False
    """Never consult the termination policy, so stuck detection never fires."""

    skip_user_turn: bool = False
    """Never call the user turn, so a conversational trial runs agent-only."""

    ignore_max_turns: bool = False
    """Bound the episode at ten times ``config.max_turns`` instead."""

    ignore_episode_timeout: bool = False
    """Never measure the episode wall-time budget."""

    unearned_excluded_reason: TerminationReason | None = None
    """Report this reason on a clean finish, with no typed evidence behind it."""

    drop_excluding_reason_evidence: bool = False
    """Report the classifier's reason while discarding the evidence it came with.

    The reason is earned and the outcome still cannot show it, so the caller
    counts the trial rather than excluding it — the shape a loop that copies
    ``decision.reason`` alone produces.
    """


@dataclass
class InMemoryAgentLoopCallLog:
    """What the loop did, for orchestrator-level assertions."""

    turns: int = 0
    generations: list[GenerationResult] = field(default_factory=list)
    declared_call_ids: list[str] = field(default_factory=list)
    executed_call_ids: list[str] = field(default_factory=list)
    termination_checks: int = 0
    user_turns: int = 0


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class InMemoryAgentLoop:
    """Deterministic :class:`~tolokaforge.core.loop.AgentLoop` over a scripted client.

    Drives generate → append → terminate-check → act → optional user turn, and
    stops on the first of: a termination decision, the turn bound, or the
    episode wall-time budget. Holds no state between episodes except
    :attr:`call_log`.
    """

    context: AgentLoopContext
    defects: LoopDefects = field(default_factory=LoopDefects)
    call_log: InMemoryAgentLoopCallLog = field(default_factory=InMemoryAgentLoopCallLog)

    # The one path an honoured tool call takes: id assignment, execution,
    # recording, error wording, output cap, metrics, observer. One per episode,
    # because the assigner it draws from is the episode's.
    funnel: ToolCallFunnel = field(init=False)

    def __post_init__(self) -> None:
        self.funnel = ToolCallFunnel.from_context(self.context)

    @property
    def _hand_rolls_tool_calls(self) -> bool:
        """Whether a defect puts this episode's calls outside the funnel.

        The funnel discharges the id and error-prefix obligations by
        construction, so a defect that switches either one off cannot be
        expressed through it.
        """
        return self.defects.raw_provider_call_ids or self.defects.plain_tool_error_text

    def run(self, system_prompt: str, messages: list[Message], start_time: float) -> LoopOutcome:
        max_turns = self.context.config.max_turns
        if self.defects.ignore_max_turns:
            max_turns *= 10

        for turn in range(max_turns):
            if self._budget_spent(start_time):
                messages.append(
                    self._system_message(
                        f"Episode timeout reached ({self.context.config.episode_timeout_s}s)."
                    )
                )
                return LoopOutcome(
                    status=TrialStatus.TIMEOUT, termination_reason=TerminationReason.TIMEOUT
                )

            try:
                result = self.context.llm_client.generate(
                    system=system_prompt,
                    messages=messages,
                    tools=self.context.tool_schemas,
                    tool_choice="auto",
                    observation=self.context.call_observation,
                )
            except Exception as exc:  # noqa: BLE001 — classified into a terminal verdict
                decision = self.context.classify_error(exc)
                messages.append(self._system_message(decision.system_message))
                return self._outcome(decision, TrialStatus.ERROR)

            self.call_log.turns += 1
            self.call_log.generations.append(result)
            self._assign_call_ids(result)
            if not self.defects.skip_metrics:
                self.context.metrics.record_generation(result)

            messages.append(self._assistant_message(result))
            self.call_log.declared_call_ids.extend(call.id for call in result.tool_calls)

            decision = self._consult_termination(result, turn, messages)
            if decision is not None:
                messages.append(self._system_message(decision.system_message))
                return self._outcome(decision, TrialStatus.COMPLETED)

            if result.tool_calls:
                self._execute(result.tool_calls, messages, result.text)
                continue

            user_decision = self._advance_user_turn(messages)
            if user_decision is not None:
                messages.append(self._system_message(user_decision.system_message))
                return self._outcome(user_decision, TrialStatus.COMPLETED)

        messages.append(
            self._system_message(f"Maximum turns ({self.context.config.max_turns}) reached.")
        )
        return LoopOutcome(
            status=TrialStatus.COMPLETED,
            termination_reason=(
                self.defects.unearned_excluded_reason or TerminationReason.MAX_TURNS
            ),
        )

    def _outcome(self, decision: TerminationDecision, default: TrialStatus) -> LoopOutcome:
        """The loop's verdict for a termination *decision*, evidence included.

        A decision that named a denominator-excluding reason carries the typed
        observation behind it, and the outcome carries both or neither: a
        reason copied without its evidence is downgraded by the caller.
        """
        return LoopOutcome(
            status=decision.status or default,
            termination_reason=decision.reason,
            excluding_reason_evidence=(
                None
                if self.defects.drop_excluding_reason_evidence
                else decision.excluding_reason_evidence
            ),
        )

    def _budget_spent(self, start_time: float) -> bool:
        if self.defects.ignore_episode_timeout:
            return False
        return time.time() - start_time > self.context.config.episode_timeout_s

    def _assign_call_ids(self, result: GenerationResult) -> None:
        """Re-key every parsed call through the episode's assigner, before anything reads it.

        Ahead of the assistant message so the declaration, the executor and the
        recorder all carry one key per call, and a provider that repeats a raw
        id within the episode still leaves each call joinable.
        """
        if self.defects.raw_provider_call_ids:
            return
        result.tool_calls = self.funnel.assign_ids(result.tool_calls)

    def _consult_termination(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None:
        if self.defects.skip_should_terminate:
            return None
        self.call_log.termination_checks += 1
        return self.context.should_terminate(result, turn, messages)

    def _execute(self, calls: list[ToolCall], messages: list[Message], assistant_text: str) -> None:
        ordered = list(reversed(calls)) if self.defects.execute_in_reverse else list(calls)

        def append_tool_message(message: Message) -> int:
            messages.append(message)
            return len(messages) - 1

        for call in ordered:
            self.call_log.executed_call_ids.append(call.id)
            if self._hand_rolls_tool_calls:
                self._execute_by_hand(call, messages)
            else:
                self.funnel.execute(call, append_tool_message, assistant_text)

    def _execute_by_hand(self, call: ToolCall, messages: list[Message]) -> None:
        """The funnel's work, written out, for the defects that bypass it.

        Everything :meth:`~tolokaforge.core.loop.ToolCallFunnel.execute` also
        does, minus the output cap, the observer notification, the argument
        recovery and the per-tool validation schema — which is the cost of
        leaving the funnel, and the reason only a defect does.
        """
        started = time.time()
        tool_result = self.context.tool_executor.execute(call.name, call.arguments, call_id=call.id)
        latency = time.time() - started
        self.context.metrics.record_tool_call()

        if self.context.recorder is not None:
            self.context.recorder.record(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments or {},
                executor=ToolExecutorIdentity.AGENT,
                status=resolve_tool_status(tool_result),
                output=resolve_tool_output(tool_result),
                latency_seconds=latency,
            )

        messages.append(
            Message(
                role=MessageRole.TOOL,
                content=self._tool_message_content(tool_result),
                tool_call_id=call.id,
                ts=_now(),
            )
        )

    def _tool_message_content(self, tool_result: Any) -> str:
        text = resolve_tool_output(tool_result)
        if tool_result.success:
            return tool_result.output
        if self.defects.plain_tool_error_text:
            return text
        return f"{TOOL_ERROR_MESSAGE_PREFIX}{text}"

    def _advance_user_turn(self, messages: list[Message]) -> TerminationDecision | None:
        if self.context.user_turn is None or self.defects.skip_user_turn:
            return None
        self.call_log.user_turns += 1
        outcome = self.context.user_turn(messages)
        if outcome.termination is not None:
            return outcome.termination
        if outcome.message is not None:
            messages.append(outcome.message)
        return None

    @staticmethod
    def _assistant_message(result: GenerationResult) -> Message:
        return Message(
            role=MessageRole.ASSISTANT,
            content=result.text,
            tool_calls=result.tool_calls or None,
            reasoning=result.reasoning,
            ts=_now(),
        )

    @staticmethod
    def _system_message(content: str) -> Message:
        return Message(role=MessageRole.SYSTEM, content=content, ts=_now())


def in_memory_agent_loop_factory(context: AgentLoopContext) -> InMemoryAgentLoop:
    """An :data:`~tolokaforge.core.loop.AgentLoopFactory` over :class:`InMemoryAgentLoop`."""
    return InMemoryAgentLoop(context=context)
