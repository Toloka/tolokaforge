"""``AnalyzeTool`` — LLM-drafted brief of what's happening in the last N turns.

The tool has **no knowledge of any specific LLM stack**. It uses whatever
:data:`~intervener.tools.base.LLMCallable` the caller supplied via
:attr:`ToolContext.llm_call`. That contract is ``(system, user) → text``
— narrow enough that a caller can wrap tolokaforge's ``LLMClient``, an
in-house HTTP client, or a test stub with a two-line adapter.

Falls back to a deterministic heuristic when ``llm_call is None`` or the
call raises. The heuristic path still produces a useful brief.
"""

from __future__ import annotations

from intervener.tools.base import InteractiveTool, ToolContext, ToolResult
from tolokaforge.session import (
    AssistantMessage,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TurnStarted,
)

__all__ = ["AnalyzeTool"]

_DEFAULT_TURNS = 5

_SYSTEM = """You summarise a live agent trial in three or four short sentences \
for an operator watching over it. Focus on:
- What the agent has been trying to do.
- Whether it is making progress, stuck in a loop, or hitting an obstacle.
- One concrete suggestion for the operator, or "no action needed" if it is fine.
Be terse. No preamble. No section headers."""


class AnalyzeTool(InteractiveTool):
    name = "analyze"
    description = "LLM-drafted brief of the last N turns (default 5)"

    def run(self, args: str, context: ToolContext) -> ToolResult:
        n = _parse_n(args, default=_DEFAULT_TURNS)
        window = _last_n_turns(context.recent_events, n)
        if not window:
            return ToolResult(
                output="(no events observed yet — analyze needs a running trial)",
                data={"turns_analyzed": 0},
            )

        transcript = _format_window(window)
        summary, source = self._draft(transcript, context)

        return ToolResult(
            output=summary,
            data={
                "turns_analyzed": _count_turn_boundaries(window),
                "events_seen": len(window),
                "source": source,
            },
        )

    def _draft(self, transcript: str, context: ToolContext) -> tuple[str, str]:
        if context.llm_call is None:
            return _heuristic_when_no_llm(context.recent_events), "heuristic"
        try:
            text = context.llm_call(_SYSTEM, transcript)
        except Exception as exc:
            return (
                _heuristic_when_llm_fails(context.recent_events, exc),
                "heuristic",
            )
        text = (text or "").strip()
        if not text:
            return _heuristic_when_llm_fails(context.recent_events, "empty"), "heuristic"
        return text, "llm"


# ── parsing helpers ─────────────────────────────────────────────────────


def _parse_n(args: str, *, default: int) -> int:
    stripped = args.strip()
    if not stripped:
        return default
    try:
        n = int(stripped)
    except ValueError:
        return default
    return max(1, n)


def _last_n_turns(events: list[TrialEvent], n: int) -> list[TrialEvent]:
    if not events:
        return []
    turn_boundaries: list[int] = []
    for i, event in enumerate(events):
        if isinstance(event, TurnStarted):
            turn_boundaries.append(i)
    if len(turn_boundaries) <= n:
        return list(events)
    cutoff = turn_boundaries[-n]
    return list(events[cutoff:])


def _count_turn_boundaries(window: list[TrialEvent]) -> int:
    return sum(1 for e in window if isinstance(e, TurnStarted))


def _format_window(events: list[TrialEvent]) -> str:
    lines = ["## Trial events (chronological):"]
    for event in events:
        line = _event_line(event)
        if line is not None:
            lines.append(line)
    return "\n".join(lines)


def _event_line(event: TrialEvent) -> str | None:
    if isinstance(event, TurnStarted):
        return f"-- turn {event.turn_index} (seq {event.seq}) --"
    if isinstance(event, AssistantMessage):
        preview = event.content_preview.replace("\n", " ")[:300]
        return f"assistant: {preview}"
    if isinstance(event, ToolCallEmitted):
        return f"tool_call: {event.tool_name}({event.arguments_preview[:150]})"
    if isinstance(event, ToolResultObserved):
        preview = event.truncated_preview.replace("\n", " ")[:150]
        return f"tool_result: {event.tool_name} → {preview}"
    return None


# ── heuristic fallback ──────────────────────────────────────────────────


def _heuristic_when_no_llm(events: list[TrialEvent]) -> str:
    return _heuristic_brief(events) + (
        " (no LLM callable supplied — caller can pass one via ToolContext.llm_call "
        "to get an LLM-drafted brief)"
    )


def _heuristic_when_llm_fails(events: list[TrialEvent], reason: object) -> str:
    return _heuristic_brief(events) + (f" (LLM call failed: {reason} — falling back to heuristic)")


def _heuristic_brief(window: list[TrialEvent]) -> str:
    tool_calls = [e for e in window if isinstance(e, ToolCallEmitted)]
    tool_names = [tc.tool_name for tc in tool_calls]
    unique_tools = sorted(set(tool_names))
    turn_count = _count_turn_boundaries(window)
    last_assistant = next(
        (e.content_preview for e in reversed(window) if isinstance(e, AssistantMessage)),
        None,
    )

    parts = [
        f"Heuristic brief ({turn_count} turns, {len(tool_calls)} tool calls: "
        f"{', '.join(unique_tools) if unique_tools else 'none'})."
    ]
    if tool_names and len(set(tool_names[-3:])) == 1 and len(tool_names) >= 3:
        parts.append(
            f"The agent has called '{tool_names[-1]}' repeatedly — likely stuck in a loop."
        )
    if last_assistant:
        short = last_assistant.strip().replace("\n", " ")
        if len(short) > 160:
            short = short[:157] + "…"
        parts.append(f"Last assistant note: {short}")
    return " ".join(parts)
