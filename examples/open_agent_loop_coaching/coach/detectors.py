"""Detectors — decide *when* the coach should help.

All detectors follow the same Protocol: `observe(event) -> None` on every
event drained from the session bus, and `should_fire(events) -> TriggerReason | None`
polled after the observation to check whether an intervention should be
issued.

Reference detectors:

* :class:`RuleDetector` — pure event-pattern matching. No LLM cost.
  Detects same-tool loops, empty-result loops, consecutive tool errors.
* :class:`LLMDetector` — invokes an LLM to judge whether the agent looks
  stuck. Reuses `intervener.tools.reference.AnalyzeTool` shape.
* :class:`AlwaysDetector` — fires on every prompt seam (dumb baseline).
* :class:`NeverDetector` — fires never (sanity check for the plumbing).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from intervener.tools.base import LLMCallable

from coach.config import DetectorSpec
from tolokaforge.session import (
    AssistantMessage,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
)

__all__ = [
    "AlwaysDetector",
    "Detector",
    "LLMDetector",
    "NeverDetector",
    "RuleDetector",
    "TriggerReason",
    "build_detector",
]


@dataclass(frozen=True)
class TriggerReason:
    """Explanation of why the detector fired — recorded in the coach report."""

    detector: str
    reason: str
    at_seq: int


class Detector(Protocol):
    """Called by the coach controller on every event; returns a trigger
    reason if the coach should intervene now, or `None` to observe only.
    """

    def check(self, event: TrialEvent, history: Sequence[TrialEvent]) -> TriggerReason | None: ...


class RuleDetector:
    """Fires when event patterns look stuck. No LLM.

    Params:
    * ``same_tool_repeat_threshold`` (default 3) — fire when the last N
      tool calls all invoke the same tool.
    * ``same_args_repeat_threshold`` (default 2) — additionally require
      identical `arguments_preview` across the repeats (stricter loop).
    * ``empty_result_threshold`` (default 3) — fire when the last N tool
      results all look empty (`"[]"`, `""`, `"null"`, `"{}"`).
    * ``consecutive_errors_threshold`` (default 3) — fire when the last
      N tool results contain "error" or "not found".
    """

    _EMPTY_MARKERS = frozenset({"[]", "", "null", "{}", "None"})

    def __init__(
        self,
        same_tool_repeat_threshold: int = 3,
        same_args_repeat_threshold: int = 2,
        empty_result_threshold: int = 3,
        consecutive_errors_threshold: int = 3,
    ) -> None:
        self._same_tool = same_tool_repeat_threshold
        self._same_args = same_args_repeat_threshold
        self._empty = empty_result_threshold
        self._errors = consecutive_errors_threshold

    def check(self, event: TrialEvent, history: Sequence[TrialEvent]) -> TriggerReason | None:
        if isinstance(event, ToolCallEmitted):
            return self._check_tool_call_pattern(history, event)
        if isinstance(event, ToolResultObserved):
            return self._check_result_pattern(history, event)
        return None

    def _check_tool_call_pattern(
        self, history: Sequence[TrialEvent], event: ToolCallEmitted
    ) -> TriggerReason | None:
        recent_calls = [e for e in history if isinstance(e, ToolCallEmitted)]
        if len(recent_calls) < self._same_tool:
            return None
        window = recent_calls[-self._same_tool :]
        names = {c.tool_name for c in window}
        if len(names) != 1:
            return None
        arg_previews = {c.arguments_preview for c in window[-self._same_args :]}
        strict_loop = len(arg_previews) == 1
        return TriggerReason(
            detector="rule",
            reason=(
                f"same tool '{event.tool_name}' called {self._same_tool}× in a row"
                + (" with identical args" if strict_loop else "")
            ),
            at_seq=event.seq,
        )

    def _check_result_pattern(
        self, history: Sequence[TrialEvent], event: ToolResultObserved
    ) -> TriggerReason | None:
        recent_results = [e for e in history if isinstance(e, ToolResultObserved)]
        if len(recent_results) < self._empty:
            return None
        window = recent_results[-self._empty :]

        empty_hits = sum(1 for r in window if _looks_empty(r.truncated_preview))
        if empty_hits == self._empty:
            return TriggerReason(
                detector="rule",
                reason=f"last {self._empty} tool results all returned empty payloads",
                at_seq=event.seq,
            )

        error_hits = sum(1 for r in window if _looks_error(r.truncated_preview))
        if error_hits >= self._errors:
            return TriggerReason(
                detector="rule",
                reason=f"last {self._errors} tool results contained errors",
                at_seq=event.seq,
            )
        return None


def _looks_empty(preview: str) -> bool:
    stripped = preview.strip()
    if not stripped:
        return True
    if stripped in RuleDetector._EMPTY_MARKERS:
        return True
    # a wrapper like "[]"; sometimes previews add quotes or truncation dots
    if stripped.replace(" ", "") in ("[]", "{}", "null"):
        return True
    return False


def _looks_error(preview: str) -> bool:
    lowered = preview.lower()
    return "error" in lowered or "not found" in lowered or "failed" in lowered


class LLMDetector:
    """Fires when an LLM judges the agent to be stuck.

    Params:
    * ``window_events`` (default 20) — how many recent events to include
      in the prompt.
    * ``fire_only_on_assistant`` (default True) — only invoke the LLM
      after `AssistantMessage` events, to keep call frequency bounded.
    """

    _SYSTEM = (
        "You are a coach watching an agent execute a trial. Given the "
        "recent event log, decide whether the agent is stuck (looping, "
        "misreading tool output, or unable to make progress). Reply with "
        'exactly one word: "STUCK" or "OK". No preamble.'
    )

    def __init__(
        self,
        llm_call: LLMCallable | None = None,
        window_events: int = 20,
        fire_only_on_assistant: bool = True,
    ) -> None:
        self._llm_call = llm_call
        self._window = window_events
        self._gate_on_assistant = fire_only_on_assistant

    def check(self, event: TrialEvent, history: Sequence[TrialEvent]) -> TriggerReason | None:
        if self._llm_call is None:
            return None
        if self._gate_on_assistant and not isinstance(event, AssistantMessage):
            return None
        window = list(history)[-self._window :]
        transcript = _format_events_for_llm(window)
        try:
            verdict = self._llm_call(self._SYSTEM, transcript).strip().upper()
        except Exception:
            # LLM failure ≠ stuck; observe silently. Fault stays in the
            # coach_report's LLM-call log via the cost tracker wrapper.
            return None
        # Robust parse: LLMs sometimes wrap with punctuation.
        if verdict.startswith("STUCK"):
            return TriggerReason(detector="llm", reason=f"LLM verdict: {verdict}", at_seq=event.seq)
        return None


class AlwaysDetector:
    """Fires on every `AssistantMessage`. Baseline for measuring the raw
    cost of over-eager coaching."""

    def check(self, event: TrialEvent, history: Sequence[TrialEvent]) -> TriggerReason | None:
        if isinstance(event, AssistantMessage):
            return TriggerReason(detector="always", reason="always-fire baseline", at_seq=event.seq)
        return None


class NeverDetector:
    """Never fires. Sanity check: an open-mode run with this detector
    should behave identically to the sealed baseline."""

    def check(self, event: TrialEvent, history: Sequence[TrialEvent]) -> TriggerReason | None:
        return None


def _format_events_for_llm(events: Sequence[TrialEvent]) -> str:
    lines = ["## Recent trial events:"]
    for e in events:
        if isinstance(e, AssistantMessage):
            lines.append(f"assistant: {e.content_preview[:200].replace(chr(10), ' ')}")
        elif isinstance(e, ToolCallEmitted):
            lines.append(f"tool_call: {e.tool_name}({e.arguments_preview[:120]})")
        elif isinstance(e, ToolResultObserved):
            lines.append(
                f"tool_result: {e.tool_name} → {e.truncated_preview[:120].replace(chr(10), ' ')}"
            )
    lines.append("\nIs the agent stuck? Reply STUCK or OK.")
    return "\n".join(lines)


def build_detector(spec: DetectorSpec, llm_call: LLMCallable | None = None) -> Detector:
    """Factory: `DetectorSpec` → concrete detector instance.

    `llm_call` is only used by `type: "llm"` detectors; other types
    ignore it. Keeping it as a top-level arg means the driver wires the
    LLMCallable once and passes it through, matching the intervener
    package's decoupling contract.
    """
    if spec.type == "rule":
        return RuleDetector(**spec.params)
    if spec.type == "llm":
        params = dict(spec.params)
        params.setdefault("llm_call", llm_call)
        return LLMDetector(**params)
    if spec.type == "always":
        return AlwaysDetector(**spec.params)
    if spec.type == "never":
        return NeverDetector(**spec.params)
    raise ValueError(f"unknown detector type: {spec.type!r}")
