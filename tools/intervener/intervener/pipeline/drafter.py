"""LLM message drafter — single-call pipeline stage.

Given a trial context (the most recent N events plus a running conversation
digest), asks an LLM to produce a :class:`InterventionSuggestion` with structured
output. When the ``ANTHROPIC_API_KEY`` env var is unset, falls back to a
deterministic heuristic drafter so the demo still runs end-to-end and its
shape is inspectable without live provider access.

Kept in one file for M2 — split when retrieval and a separate calibrated
urgency head land.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from intervener.schema import InterventionSuggestion
from tolokaforge.session import (
    AssistantMessage,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
)

__all__ = ["draft_suggestion"]

_DEFAULT_MODEL = "claude-opus-4-7"
_SYSTEM = """You are an LLM intervener embedded in an eval harness.
You watch a running agent trial and, when the agent is stuck or about to waste budget,
propose a single next message the human operator should inject to unstick it.

Return JSON only, matching this schema exactly:
{
  "situation": "<one sentence>",
  "urgency": "<none|low|medium|high|critical>",
  "urgency_score": <float in [0,1]>,
  "suggested_message": "<message to inject>",
  "alternative_suggestions": [<up to 2 alternatives>],
  "rationale": "<short explanation>"
}
Never wrap the JSON in backticks. Never add fields.
"""


def draft_suggestion(
    trial_id: str,
    at_seq: int,
    recent_events: list[TrialEvent],
    model: str = _DEFAULT_MODEL,
) -> InterventionSuggestion:
    """Produce a :class:`InterventionSuggestion` from a window of recent events.

    Uses Anthropic Messages API when both ``ANTHROPIC_API_KEY`` is set AND the
    ``anthropic`` package is importable; otherwise falls back to the heuristic
    drafter so the pipeline is exercisable in test and demo runs without a
    live provider.
    """
    context = _format_context(recent_events)
    payload = _call_llm(context, model=model) if _llm_available() else _heuristic(recent_events)
    payload["trial_id"] = trial_id
    payload["at_seq"] = at_seq
    return InterventionSuggestion.model_validate(payload)


def _llm_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _call_llm(context: str, model: str) -> dict[str, Any]:
    from anthropic import Anthropic

    client = Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=800,
        system=_SYSTEM,
        messages=[{"role": "user", "content": context}],
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"LLM drafter returned no JSON object: {text[:200]!r}")
    return json.loads(match.group(0))


def _format_context(events: list[TrialEvent]) -> str:
    lines: list[str] = ["## Recent trial events (chronological):"]
    for e in events[-12:]:
        lines.append(_event_line(e))
    lines.append("")
    lines.append("## Task: propose an intervention if the agent is stuck or wasting budget.")
    return "\n".join(lines)


def _event_line(event: TrialEvent) -> str:
    if isinstance(event, AssistantMessage):
        return f"[{event.seq}] assistant: {event.content_preview}"
    if isinstance(event, ToolCallEmitted):
        return f"[{event.seq}] tool_call: {event.tool_name}({event.arguments_preview})"
    if isinstance(event, ToolResultObserved):
        return f"[{event.seq}] tool_result: {event.tool_name} -> {event.truncated_preview}"
    return f"[{event.seq}] {event.kind}"


def _heuristic(events: list[TrialEvent]) -> dict[str, Any]:
    """Deterministic fallback used when no LLM key is available.

    Detects the most common failure signature — repeated tool calls to the
    same tool — and produces a plausible suggestion. Never claims high
    urgency in fallback mode.
    """
    tool_names = [e.tool_name for e in events if isinstance(e, ToolCallEmitted)]
    if len(tool_names) >= 3 and len(set(tool_names[-3:])) == 1:
        looping_tool = tool_names[-1]
        return {
            "situation": f"agent is repeatedly calling {looping_tool!r} without progress",
            "urgency": "medium",
            "urgency_score": 0.6,
            "suggested_message": (
                f"You've called {looping_tool!r} three times without a useful result. "
                "Consider a different approach — either change the arguments significantly "
                "or use a different tool."
            ),
            "alternative_suggestions": [
                f"Try passing different arguments to {looping_tool!r}",
                "Ask a clarifying question about what state you expect",
            ],
            "rationale": "heuristic: 3 consecutive calls to the same tool",
        }
    return {
        "situation": "agent progressing normally; no intervention needed",
        "urgency": "none",
        "urgency_score": 0.05,
        "suggested_message": "(no intervention)",
        "alternative_suggestions": [],
        "rationale": "heuristic: no stuck-pattern detected in the recent window",
    }
