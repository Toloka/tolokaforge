"""Offer the agent its tools under shorter names, and map its calls back.

Some providers cap a tool name's length (Meta's API and Cohere refuse a name
over 64 characters), while packs namespace every tool as
``<task category>_<service>_<tool>`` and so routinely exceed it. With
``ModelConfig.strip_task_tool_prefix`` set, the agent's loop talks to its
model through :class:`ToolNameAliasingClient`: the ``<task category>_``
prefix is stripped from each tool name on the wire — in the request's
``tools`` and in the tool calls replayed from history — and every tool call
the model returns is mapped back to the pack's name before the loop sees it.

Everything downstream of the loop therefore reads the pack's own names: the
tool executor, the recorded trajectory, the trace checks and the grader. Only
the model's view of the names changes, which is why the option is per model
and off by default.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.models.trajectory import Message
from tolokaforge.core.run_display_events import LLMCallObservation


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


class ToolNameAliases:
    """The bijection between a trial's tool names and the names the model sees.

    A name that does not carry ``prefix`` is offered unchanged. Construction
    refuses a prefix whose removal would leave two tools under one name, or a
    tool with no name at all — either would make a model's call ambiguous.
    """

    def __init__(self, names: Iterable[str], prefix: str) -> None:
        if not prefix:
            raise ValueError("a tool-name prefix to strip must be non-empty")
        self.prefix = prefix
        self._outbound: dict[str, str] = {}
        self._inbound: dict[str, str] = {}
        for name in names:
            alias = name[len(prefix) :] if name.startswith(prefix) else name
            if not alias:
                raise ValueError(f"stripping {prefix!r} from tool {name!r} leaves an empty name")
            taken = self._inbound.get(alias)
            if taken is not None and taken != name:
                raise ValueError(
                    f"stripping {prefix!r} offers tools {taken!r} and {name!r} under one "
                    f"name {alias!r}; the model's calls to it could not be told apart"
                )
            self._outbound[name] = alias
            self._inbound[alias] = name

    @classmethod
    def for_tools(cls, tool_schemas: list[dict[str, Any]], prefix: str) -> ToolNameAliases:
        return cls((_schema_name(schema) for schema in tool_schemas), prefix)

    def outbound(self, name: str) -> str:
        """The name the model is offered for the pack's tool ``name``."""
        return self._outbound.get(name, name)

    def inbound(self, name: str) -> str:
        """The pack's name for a tool the model called ``name``.

        A name the model invented, or a pack name it repeated in full, passes
        through unchanged, so the executor reports it exactly as it would
        without aliasing.
        """
        return self._inbound.get(name, name)

    def schemas(self, tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copies of ``tool_schemas`` carrying the model-facing names."""
        return [
            _with_schema_name(schema, self.outbound(_schema_name(schema)))
            for schema in tool_schemas
        ]

    def messages(self, messages: list[Message]) -> list[Message]:
        """``messages`` with each replayed tool call under its model-facing name."""
        return [self._message(message, self.outbound) for message in messages]

    def result(self, result: GenerationResult) -> GenerationResult:
        """``result`` with each tool call under the pack's name, in place."""
        result.tool_calls = [
            call.model_copy(update={"name": self.inbound(call.name)}) for call in result.tool_calls
        ]
        return result

    @staticmethod
    def _message(message: Message, rename: Any) -> Message:
        if not message.tool_calls:
            return message
        calls = [call.model_copy(update={"name": rename(call.name)}) for call in message.tool_calls]
        return message.model_copy(update={"tool_calls": calls})


class ToolNameAliasingClient:
    """A :class:`~tolokaforge.core.loop.LoopLLMClient` that runs ``inner`` under
    :class:`ToolNameAliases`."""

    def __init__(self, inner: _AgentClient, aliases: ToolNameAliases) -> None:
        self._inner = inner
        self._aliases = aliases

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
            messages=self._aliases.messages(messages),
            tools=self._aliases.schemas(tools),
            tool_choice=tool_choice,
            observation=observation,
        )
        return self._aliases.result(result)


def _schema_name(schema: dict[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, dict):
        return str(function["name"])
    return str(schema["name"])


def _with_schema_name(schema: dict[str, Any], name: str) -> dict[str, Any]:
    function = schema.get("function")
    if isinstance(function, dict):
        return {**schema, "function": {**function, "name": name}}
    return {**schema, "name": name}
