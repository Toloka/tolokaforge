"""``ContextTool`` — print the general trial context.

Non-agentic. Formats ``task_metadata`` (if the caller supplied any) plus
event counters (turns observed, tool calls, assistant messages, last
assistant preview) into a small text block. Works with an empty context;
degrades cleanly when fields are missing.
"""

from __future__ import annotations

from intervener.tools.base import InteractiveTool, ToolContext, ToolResult
from tolokaforge.session import (
    AssistantMessage,
    ToolCallEmitted,
    ToolResultObserved,
    TurnStarted,
)

__all__ = ["ContextTool"]


class ContextTool(InteractiveTool):
    name = "context"
    description = "print the trial's task metadata + event counters"

    def run(self, args: str, context: ToolContext) -> ToolResult:
        del args  # this tool takes no arguments today
        lines: list[str] = []
        data: dict = {}

        if context.binding is not None:
            lines.append(f"trial_id:      {context.binding.trial_id}")
            data["trial_id"] = context.binding.trial_id

        meta = context.task_metadata or {}
        if meta:
            name = meta.get("name") or meta.get("task_id")
            if name:
                lines.append(f"task:          {name}")
                data["task"] = name
            description = meta.get("description")
            if description:
                short = description.strip().replace("\n", " ")
                if len(short) > 200:
                    short = short[:197] + "…"
                lines.append(f"description:   {short}")
                data["description"] = description
        else:
            lines.append("task metadata: (none supplied — caller did not populate it)")

        turns = 0
        tool_calls = 0
        tool_results = 0
        assistants = 0
        last_assistant: str | None = None
        for event in context.recent_events:
            if isinstance(event, TurnStarted):
                turns += 1
            elif isinstance(event, ToolCallEmitted):
                tool_calls += 1
            elif isinstance(event, ToolResultObserved):
                tool_results += 1
            elif isinstance(event, AssistantMessage):
                assistants += 1
                last_assistant = event.content_preview

        lines.append(
            f"observed:      {turns} turns · {tool_calls} tool calls · "
            f"{tool_results} tool results · {assistants} assistant messages"
        )
        data["counters"] = {
            "turns": turns,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "assistant_messages": assistants,
        }
        if last_assistant:
            preview = last_assistant.strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:197] + "…"
            lines.append(f"last message:  {preview}")
            data["last_assistant_preview"] = preview

        return ToolResult(output="\n".join(lines), data=data)
