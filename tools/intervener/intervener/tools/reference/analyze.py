"""``AnalyzeTool`` — LLM-drafted brief of what's happening in the last N turns.

Agentic. Uses Anthropic when both ``ANTHROPIC_API_KEY`` and the
``anthropic`` package are present; otherwise falls back to a deterministic
heuristic summary. Same detection pattern as
:mod:`intervener.pipeline.drafter`; shared via
:mod:`intervener.tools._llm`.
"""

from __future__ import annotations

from intervener.tools._llm import llm_available
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
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = """You summarise a live agent trial in three or four short sentences \
for an operator watching over it. Focus on:
- What the agent has been trying to do.
- Whether it is making progress, stuck in a loop, or hitting an obstacle.
- One concrete suggestion for the operator, or "no action needed" if it is fine.
Be terse. No preamble. No section headers."""


class AnalyzeTool(InteractiveTool):
    name = "analyze"
    description = "LLM-drafted brief of the last N turns (default 5)"

    def __init__(self, model: str = _DEFAULT_MODEL) -> None:
        self._model = model

    def run(self, args: str, context: ToolContext) -> ToolResult:
        n = _parse_n(args, default=_DEFAULT_TURNS)
        window = _last_n_turns(context.recent_events, n)
        if not window:
            return ToolResult(
                output="(no events observed yet — analyze needs a running trial)",
                data={"turns_analyzed": 0},
            )

        transcript = _format_window(window)
        if llm_available():
            summary = _call_llm(transcript, model=self._model)
            source = "llm"
        else:
            summary = _heuristic(window)
            source = "heuristic"

        return ToolResult(
            output=summary,
            data={
                "turns_analyzed": _count_turn_boundaries(window),
                "events_seen": len(window),
                "source": source,
            },
        )


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
    """Slice back through events, collecting the last N turn boundaries + their content."""
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


def _call_llm(transcript: str, model: str) -> str:
    from anthropic import Anthropic

    client = Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=400,
        system=_SYSTEM,
        messages=[{"role": "user", "content": transcript}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return text.strip() or "(LLM returned empty response)"


def _heuristic(window: list[TrialEvent]) -> str:
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
    parts.append(
        "(set ANTHROPIC_API_KEY and install the anthropic package for an LLM-drafted brief)"
    )
    return " ".join(parts)
