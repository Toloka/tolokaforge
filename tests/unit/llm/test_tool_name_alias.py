"""``strip_task_tool_prefix``: the model sees short tool names, everything else the pack's.

Pins the wire contract of :mod:`tolokaforge.core.llm.tool_name_alias`: the
request's tools and the replayed tool calls lose the ``<task category>_``
prefix, the model's tool calls regain it, and names outside the prefix pass
through in both directions.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.tool_name_alias import ToolNameAliases, ToolNameAliasingClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.unit

PREFIX = "ots_bank_hr_d365_"
LONG = "ots_bank_hr_d365_hr_service_api_hr_service_api_create_document_generation_request"
SHORT = "hr_service_api_hr_service_api_create_document_generation_request"
CONNECTOR = "connectors_typesense_search_policy"


def _schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": "d", "parameters": {"type": "object"}},
    }


class _RecordingClient:
    """Captures what the loop's client was asked and answers with ``calls``."""

    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls = calls
        self.seen: dict[str, Any] = {}

    def generate(self, system, messages, tools, tool_choice="auto", observation=None):
        self.seen = {"system": system, "messages": messages, "tools": tools}
        return GenerationResult(text="", tool_calls=list(self.calls))


def test_long_names_fit_after_stripping() -> None:
    aliases = ToolNameAliases([LONG, CONNECTOR], PREFIX)
    assert aliases.outbound(LONG) == SHORT
    assert len(aliases.outbound(LONG)) == 64
    assert aliases.inbound(SHORT) == LONG


def test_a_name_without_the_prefix_passes_both_ways() -> None:
    aliases = ToolNameAliases([LONG, CONNECTOR], PREFIX)
    assert aliases.outbound(CONNECTOR) == CONNECTOR
    assert aliases.inbound(CONNECTOR) == CONNECTOR


def test_an_unknown_or_full_name_from_the_model_passes_unchanged() -> None:
    aliases = ToolNameAliases([LONG], PREFIX)
    assert aliases.inbound("made_up_tool") == "made_up_tool"
    assert aliases.inbound(LONG) == LONG


def test_a_collision_is_refused() -> None:
    with pytest.raises(ValueError, match="could not be told apart"):
        ToolNameAliases([f"{PREFIX}get_case", "get_case"], PREFIX)


def test_an_empty_alias_is_refused() -> None:
    with pytest.raises(ValueError, match="empty name"):
        ToolNameAliases(["ots_bank_hr_d365_"], PREFIX)


def test_an_empty_prefix_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ToolNameAliases([LONG], "")


def test_the_client_aliases_the_wire_and_restores_the_result() -> None:
    inner = _RecordingClient([ToolCall(id="c2", name=SHORT, arguments={"x": 1})])
    tools = [_schema(LONG), _schema(CONNECTOR)]
    history = [
        Message(role=MessageRole.USER, content="hi"),
        Message(
            role=MessageRole.ASSISTANT,
            tool_calls=[ToolCall(id="c1", name=LONG, arguments={})],
        ),
        Message(role=MessageRole.TOOL, content="{}", tool_call_id="c1"),
    ]
    client = ToolNameAliasingClient(inner, ToolNameAliases.for_tools(tools, PREFIX))

    result = client.generate(system="s", messages=history, tools=tools)

    assert [t["function"]["name"] for t in inner.seen["tools"]] == [SHORT, CONNECTOR]
    assert inner.seen["messages"][1].tool_calls[0].name == SHORT
    assert [call.name for call in result.tool_calls] == [LONG]
    assert result.tool_calls[0].arguments == {"x": 1}
    # The loop's own records are never rewritten.
    assert tools[0]["function"]["name"] == LONG
    assert history[1].tool_calls[0].name == LONG


def test_the_option_is_off_by_default() -> None:
    assert (
        ModelConfig(provider="openrouter", name="meta/muse-wonder").strip_task_tool_prefix is False
    )
