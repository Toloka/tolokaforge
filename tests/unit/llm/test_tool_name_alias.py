"""``tool_name_max_length``: only names over the limit are shortened, on the wire only.

Pins :mod:`tolokaforge.core.llm.tool_name_alias`: a long name loses its
``<task category>_`` prefix, then further leading segments only if it still
does not fit; names that fit are never touched; the model's tool calls are
mapped back to the pack's names.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm import GenerationResult
from tolokaforge.core.llm.tool_name_alias import ToolNameAliasingClient, tool_name_aliases
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.unit

CATEGORY = "ots_bank_hr_d365"
LONG = "ots_bank_hr_d365_hr_service_api_hr_service_api_create_document_generation_request"
SHORT = "hr_service_api_hr_service_api_create_document_generation_request"
FITS = "ots_bank_hr_d365_d365_api_d365_api_create_case"
CONNECTOR = "connectors_typesense_search_policy"


def _schemas(*names: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": n, "parameters": {}}} for n in names]


def test_only_a_name_over_the_limit_loses_the_category_prefix() -> None:
    aliases = tool_name_aliases(_schemas(LONG, FITS, CONNECTOR), CATEGORY, 64)
    assert aliases == {LONG: SHORT}
    assert len(SHORT) == 64


def test_a_name_still_too_long_loses_further_segments_until_it_fits() -> None:
    assert tool_name_aliases(_schemas(LONG), CATEGORY, 50) == {
        LONG: "hr_service_api_create_document_generation_request"
    }


def test_no_limit_or_no_long_name_means_no_aliases() -> None:
    assert tool_name_aliases(_schemas(LONG), CATEGORY, None) == {}
    assert tool_name_aliases(_schemas(FITS, CONNECTOR), CATEGORY, 64) == {}


def test_a_name_offered_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        tool_name_aliases(_schemas(LONG, SHORT), CATEGORY, 64)


class _RecordingClient:
    def __init__(self, calls: list[ToolCall]) -> None:
        self.calls = calls
        self.seen: dict[str, Any] = {}

    def generate(self, *, system, messages, tools, tool_choice, observation):
        self.seen = {"messages": messages, "tools": tools}
        return GenerationResult(text="", tool_calls=list(self.calls))


def test_the_client_aliases_the_wire_and_restores_the_result() -> None:
    inner = _RecordingClient(
        [
            ToolCall(id="c2", name=SHORT, arguments={"x": 1}),
            ToolCall(id="c3", name=FITS, arguments={}),
        ]
    )
    tools = _schemas(LONG, FITS)
    history = [
        Message(role=MessageRole.USER, content="hi"),
        Message(
            role=MessageRole.ASSISTANT, tool_calls=[ToolCall(id="c1", name=LONG, arguments={})]
        ),
        Message(role=MessageRole.TOOL, content="{}", tool_call_id="c1"),
    ]
    client = ToolNameAliasingClient(inner, tool_name_aliases(tools, CATEGORY, 64))

    result = client.generate(system="s", messages=history, tools=tools)

    assert [t["function"]["name"] for t in inner.seen["tools"]] == [SHORT, FITS]
    assert inner.seen["tools"][1] is tools[1]
    assert inner.seen["messages"][1].tool_calls[0].name == SHORT
    assert [call.name for call in result.tool_calls] == [LONG, FITS]
    assert result.tool_calls[0].arguments == {"x": 1}
    # The loop's own records are never rewritten.
    assert tools[0]["function"]["name"] == LONG
    assert history[1].tool_calls[0].name == LONG


def test_the_limit_is_off_by_default() -> None:
    assert ModelConfig(provider="openrouter", name="meta/muse-wonder").tool_name_max_length is None
