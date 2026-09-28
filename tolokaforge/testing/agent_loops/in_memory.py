"""The in-memory reference :class:`~tolokaforge.core.loop.AgentLoop`.

:class:`InMemoryAgentLoop` is the shortest loop that satisfies every obligation
:class:`~tolokaforge.core.loop.AgentLoop` declares. It runs no provider, reaches
no substrate and holds no trial state beyond one episode, so an implementer can
read the whole turn cycle in one screen and copy the four things that are not
optional: the id assignment, the error prefix, the sink feed, and the two
budgets.

It is also the suite's own control. Every defect in :class:`LoopDefects`
switches off exactly one obligation, which is what lets
``tests/canonical/test_agent_loop_contract.py`` show each conformance
assertion failing on the loop that violates the assertion it is written for. A
conformance kit nothing can fail is a kit that proves nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.loop import (
    AgentLoopContext,
    LoopOutcome,
    TerminationDecision,
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
    """Key calls by the provider's raw id instead of ``context.call_ids``."""

    execute_in_reverse: bool = False
    """Execute a turn's calls in reverse declaration order.

    Same tool, same turn, different arguments: the two views then derive their
    episode-unique keys from disagreeing orders and each result is attributed to
    the other call's declaration — silently, because both name one tool.
    """

    plain_tool_error_text: bool = False
    """Word a failed call's ``role: tool`` message without the error prefix."""

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
    return datetime.now(tz=timezone.utc)


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
                return LoopOutcome(
                    status=decision.status or TrialStatus.ERROR,
                    termination_reason=decision.reason,
                )

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
                return LoopOutcome(
                    status=decision.status or TrialStatus.COMPLETED,
                    termination_reason=decision.reason,
                )

            if result.tool_calls:
                self._execute(result.tool_calls, messages)
                continue

            user_decision = self._advance_user_turn(messages)
            if user_decision is not None:
                messages.append(self._system_message(user_decision.system_message))
                return LoopOutcome(
                    status=user_decision.status or TrialStatus.COMPLETED,
                    termination_reason=user_decision.reason,
                )

        messages.append(
            self._system_message(f"Maximum turns ({self.context.config.max_turns}) reached.")
        )
        return LoopOutcome(
            status=TrialStatus.COMPLETED,
            termination_reason=(
                self.defects.unearned_excluded_reason or TerminationReason.MAX_TURNS
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
        result.tool_calls = [
            call.model_copy(update={"id": self.context.call_ids.assign(call.id)})
            for call in result.tool_calls
        ]

    def _consult_termination(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None:
        if self.defects.skip_should_terminate:
            return None
        self.call_log.termination_checks += 1
        return self.context.should_terminate(result, turn, messages)

    def _execute(self, calls: list[ToolCall], messages: list[Message]) -> None:
        ordered = list(reversed(calls)) if self.defects.execute_in_reverse else list(calls)
        for call in ordered:
            started = time.time()
            tool_result = self.context.tool_executor.execute(
                call.name, call.arguments, call_id=call.id
            )
            latency = time.time() - started
            self.call_log.executed_call_ids.append(call.id)
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
