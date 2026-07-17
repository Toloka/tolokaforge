"""Interveners — decide *what* the coach does when its detector fires.

All interveners return an :class:`intervener.session_ready.TrialIntervention`
(or ``None`` to abstain) given a trigger reason and a session binding.

Reference interveners:

* :class:`HintIntervener` — submits a canned InjectMessage. No LLM cost.
* :class:`LLMSuggestIntervener` — asks an LLM to draft a specific
  suggestion given the recent events.
* :class:`KillIntervener` — after N triggers, terminates the trial with
  a `Kill`. Requires the coach to have `admin` role.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from intervener.binding import SessionBinding
from intervener.tools.base import LLMCallable

from coach.config import IntervenerSpec
from coach.detectors import TriggerReason
from tolokaforge.session import (
    AssistantMessage,
    InjectMessage,
    Kill,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TrialIntervention,
)

__all__ = [
    "HintIntervener",
    "Intervener",
    "KillIntervener",
    "LLMSuggestIntervener",
    "build_intervener",
]


class Intervener(Protocol):
    def build(
        self,
        trigger: TriggerReason,
        binding: SessionBinding,
        history: Sequence[TrialEvent],
    ) -> TrialIntervention | None: ...


class HintIntervener:
    """Submits a fixed template as an `InjectMessage`.

    Params:
    * ``hint`` (default given below) — the message content.
    """

    _DEFAULT_HINT = (
        "You appear to be stuck ({reason}). Reconsider your approach — "
        "re-read the initial state, broaden or replace your queries, or "
        "consult any provided documentation before your next tool call."
    )

    def __init__(self, hint: str | None = None) -> None:
        self._hint_template = hint or self._DEFAULT_HINT

    def build(
        self,
        trigger: TriggerReason,
        binding: SessionBinding,
        history: Sequence[TrialEvent],
    ) -> TrialIntervention | None:
        content = self._hint_template.format(reason=trigger.reason)
        return InjectMessage(
            trial_id=binding.trial_id,
            attach_to_seq=trigger.at_seq,
            participant_id=binding.participant_id,
            timestamp=datetime.now(UTC),
            content=content,
        )


class LLMSuggestIntervener:
    """Asks an LLM to draft a specific hint given the recent events + reason.

    Params:
    * ``window_events`` (default 20) — history to include in the prompt.
    * ``max_words`` (default 60) — cap on suggestion length.
    """

    _SYSTEM = (
        "You are coaching an agent that appears stuck. Given the recent "
        "trial events and the reason the coach flagged this moment, write "
        "ONE actionable next-step suggestion for the agent in {max_words} "
        "words or fewer. Be concrete — reference specific tool arguments "
        "or file paths when possible. No preamble. No apologies."
    )

    def __init__(
        self,
        llm_call: LLMCallable | None = None,
        window_events: int = 20,
        max_words: int = 60,
    ) -> None:
        self._llm_call = llm_call
        self._window = window_events
        self._max_words = max_words

    def build(
        self,
        trigger: TriggerReason,
        binding: SessionBinding,
        history: Sequence[TrialEvent],
    ) -> TrialIntervention | None:
        if self._llm_call is None:
            return None
        transcript = _format_for_prompt(history[-self._window :], trigger)
        system = self._SYSTEM.format(max_words=self._max_words)
        try:
            suggestion = self._llm_call(system, transcript).strip()
        except Exception:
            return None
        if not suggestion:
            return None
        return InjectMessage(
            trial_id=binding.trial_id,
            attach_to_seq=trigger.at_seq,
            participant_id=binding.participant_id,
            timestamp=datetime.now(UTC),
            content=suggestion,
        )


class KillIntervener:
    """After N total triggers, submits `Kill`. Requires `role: "admin"`
    for the coach — participants below admin can't supersede a running
    trial. Useful as a hard-safety upper bound.

    Params:
    * ``after_n`` (default 3) — number of prior triggers before firing.
    """

    def __init__(self, after_n: int = 3) -> None:
        self._threshold = after_n
        self._count = 0

    def build(
        self,
        trigger: TriggerReason,
        binding: SessionBinding,
        history: Sequence[TrialEvent],
    ) -> TrialIntervention | None:
        self._count += 1
        if self._count < self._threshold:
            return None
        return Kill(
            trial_id=binding.trial_id,
            attach_to_seq=trigger.at_seq,
            participant_id=binding.participant_id,
            timestamp=datetime.now(UTC),
            reason=f"coach killed after {self._count} stuck detections",
        )


def _format_for_prompt(events: Sequence[TrialEvent], trigger: TriggerReason) -> str:
    lines = [f"## Coach trigger: {trigger.reason}", "", "## Recent events:"]
    for e in events:
        if isinstance(e, AssistantMessage):
            lines.append(f"assistant: {e.content_preview[:200].replace(chr(10), ' ')}")
        elif isinstance(e, ToolCallEmitted):
            lines.append(f"tool_call: {e.tool_name}({e.arguments_preview[:150]})")
        elif isinstance(e, ToolResultObserved):
            preview = e.truncated_preview[:150].replace(chr(10), " ")
            lines.append(f"tool_result: {e.tool_name} → {preview}")
    lines.append(
        "\nDraft one concrete next-step suggestion for the agent. "
        "Reference specific tool arguments or paths."
    )
    return "\n".join(lines)


def build_intervener(spec: IntervenerSpec, llm_call: LLMCallable | None = None) -> Intervener:
    """Factory: `IntervenerSpec` → concrete intervener."""
    if spec.type == "hint":
        return HintIntervener(**spec.params)
    if spec.type == "llm_suggest":
        params = dict(spec.params)
        params.setdefault("llm_call", llm_call)
        return LLMSuggestIntervener(**params)
    if spec.type == "kill":
        return KillIntervener(**spec.params)
    raise ValueError(f"unknown intervener type: {spec.type!r}")
