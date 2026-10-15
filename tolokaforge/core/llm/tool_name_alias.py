"""Shorten the tool names a provider would refuse, and map the model's calls back.

Some providers cap a tool name's length (Meta's API and Cohere refuse a name
over 64 characters), while packs namespace every tool as
``<task category>_<service>_<tool>``. With ``ModelConfig.tool_name_max_length``
set, only the names over the limit are shortened — first by their
``<task category>_`` prefix, then, if still too long, by further leading
``_``-segments until they fit — and the agent's loop talks to its model
through :class:`ToolNameAliasingClient`, which offers the short names on the
wire — as the tools' function names, in replayed tool calls, and in tool-result
text such as an executor's "Invalid arguments for tool '<name>'" — and maps
every tool call the model returns back to the pack's name.

Everything downstream of the loop therefore reads the pack's own names: the
tool executor, the recorded trajectory, the trace checks and the grader.
"""

from __future__ import annotations

from typing import Any, Protocol

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.models.trajectory import Message, MessageRole, ToolCall
from tolokaforge.core.run_display_events import LLMCallObservation


def tool_name_aliases(
    tool_schemas: list[dict[str, Any]], category: str | None, limit: int | None
) -> dict[str, str]:
    """``{pack name: model name}`` for every tool whose name exceeds ``limit``.

    Empty when ``limit`` is ``None`` or every name fits. Refuses a result that
    offers two tools under one name, since the model's calls to it could not be
    told apart.
    """
    if limit is None:
        return {}
    prefix = f"{category}_" if category else None
    names = [schema["function"]["name"] for schema in tool_schemas]
    aliases: dict[str, str] = {}
    for name in names:
        short = name
        if len(short) > limit and prefix and short.startswith(prefix):
            short = short[len(prefix) :]
        while len(short) > limit and "_" in short:
            short = short.split("_", 1)[1]
        if short != name:
            aliases[name] = short
    offered = [aliases.get(name, name) for name in names]
    if len(set(offered)) != len(offered):
        clashes = sorted({n for n in offered if offered.count(n) > 1})
        raise ValueError(f"shortening tool names to {limit} characters offers {clashes} twice")
    return aliases


class _AgentClient(Protocol):
    """The generate call the wrapper makes, by keyword, on the agent's client."""

    def generate(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str,
        observation: LLMCallObservation | None,
    ) -> GenerationResult: ...


class ToolNameAliasingClient:
    """A :class:`~tolokaforge.core.loop.LoopLLMClient` offering ``aliases`` on the wire."""

    def __init__(self, inner: _AgentClient, aliases: dict[str, str]) -> None:
        self._inner = inner
        self._outbound = aliases
        self._inbound = {short: name for name, short in aliases.items()}
        # Longest first, so a name that contains another is replaced whole.
        self._in_text = sorted(aliases, key=len, reverse=True)

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        result = self._inner.generate(
            system=system,
            messages=[self._wire(message) for message in messages],
            tools=[_renamed_schema(schema, self._outbound) for schema in tools],
            tool_choice=tool_choice,
            observation=observation,
        )
        result.tool_calls = _renamed(result.tool_calls, self._inbound)
        return result

    def _wire(self, message: Message) -> Message:
        update: dict[str, Any] = {}
        if message.tool_calls:
            update["tool_calls"] = _renamed(message.tool_calls, self._outbound)
        if message.role is MessageRole.TOOL and any(n in message.content for n in self._in_text):
            content = message.content
            for name in self._in_text:
                content = content.replace(name, self._outbound[name])
            update["content"] = content
        return message.model_copy(update=update) if update else message


def _renamed(calls: list[ToolCall], names: dict[str, str]) -> list[ToolCall]:
    return [
        call.model_copy(update={"name": names[call.name]}) if call.name in names else call
        for call in calls
    ]


def _renamed_schema(schema: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    function = schema["function"]
    if function["name"] not in names:
        return schema
    return {**schema, "function": {**function, "name": names[function["name"]]}}
