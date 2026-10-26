"""A ``{"$ref": "…"}`` object in a tool result never reaches a Gemini
``function_response.response``.

The Gemini API reads such an object anywhere inside ``response`` as a reference
to one of the response's multimodal parts (by ``display_name``) and refuses the
request with HTTP 400 ``INVALID_ARGUMENT`` when no part carries that name.
litellm's Gemini translation — and so any OpenAI-format gateway built on it —
parses a JSON tool message into ``response`` verbatim, so a tool that returns a
JSON Schema fragment (``"$ref": "#/$defs/X"``) fails the next model call.

The Gemini presets wire ``content_policy: gemini`` (``GeminiContent``):
``LLMClient._convert_messages`` sends such a result as
``{"content": "<the original text>"}``. This module locks:

1. the pure wrap rule (``wrap_part_reference_tool_result``) — what is wrapped,
   what passes through, and that the original text survives verbatim;
2. the wire: a Gemini client wraps, an OpenAI-preset client does not, on both
   the plain-text and the content-blocks path;
3. the end-to-end guard through litellm's own converter: the Gemini request
   built from the converted message holds no ``$ref`` key at any depth, and
   the model still receives the original text.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from litellm.litellm_core_utils.prompt_templates import factory
from tolokaforge_models.policies.gemini import GeminiContent

from tolokaforge.core.llm import LLMClient
from tolokaforge.core.llm.content_policy import (
    OpenAIContent,
    contains_part_reference,
    wrap_part_reference_tool_result,
)
from tolokaforge.core.llm.presets import resolve_policy_names
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.unit


_SCHEMA_RESULT: dict[str, Any] = {
    "results": [
        {
            "id": "jira.attachments.add",
            "request_schema": {
                "properties": {"multipart_file": {"$ref": "#/$defs/MultipartFileBinding"}},
                "$defs": {"MultipartFileBinding": {"type": "object"}},
            },
        }
    ],
    "count": 1,
}
_SCHEMA_TEXT = json.dumps(_SCHEMA_RESULT)


def _ref_keys(value: object) -> list[str]:
    """Every ``$ref`` key's value found at any depth, as strings."""
    found: list[str] = []
    stack: list[object] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "$ref" in node:
                found.append(str(node["$ref"]))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


# ---------------------------------------------------------------------------
# 1. The wrap rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        _SCHEMA_TEXT,
        json.dumps({"$ref": "#/$defs/X"}),
        json.dumps([{"a": {"$ref": "image_0.png"}}]),
        json.dumps({"deep": [[{"$ref": "x"}]]}),
        # The key spelled with a \u escape — JSON text Gemini reads the same way.
        '{"\\u0024ref": "#/$defs/X"}',
        "  \n" + _SCHEMA_TEXT,
    ],
    ids=["schema", "root", "list_root", "nested_lists", "u_escaped_key", "leading_space"],
)
def test_a_part_reference_at_any_depth_is_wrapped_verbatim(text: str) -> None:
    wrapped = wrap_part_reference_tool_result(text)
    assert wrapped != text
    outer = json.loads(wrapped)
    assert outer == {"content": text}, "the original text survives verbatim inside the wrapper"
    assert not _ref_keys(outer), "the wrapper itself carries no $ref key"


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"results": [{"id": "jira.issues.get"}], "count": 1}),
        json.dumps({"$ref": ""}),
        json.dumps({"$ref": 12}),
        json.dumps({"$ref": None}),
        json.dumps({"note": 'see "$ref" in the schema'}),
        "Error: $ref not resolved",
        '{"not json": $ref}',
        "{}",
        "",
        "plain text with no reference",
    ],
    ids=[
        "no_ref",
        "empty_ref",
        "int_ref",
        "null_ref",
        "ref_inside_string",
        "non_json_text",
        "invalid_json",
        "empty_object",
        "empty",
        "plain",
    ],
)
def test_everything_else_passes_through_unchanged(text: str) -> None:
    assert wrap_part_reference_tool_result(text) is text


def test_contains_part_reference_matches_the_gemini_shape_only() -> None:
    assert contains_part_reference({"a": [{"b": {"$ref": "x"}}]})
    assert not contains_part_reference({"$ref": ""})
    assert not contains_part_reference({"$ref": ["x"]})
    assert not contains_part_reference("$ref")
    assert not contains_part_reference(["$ref"])


# ---------------------------------------------------------------------------
# 2. The wire
# ---------------------------------------------------------------------------


def _tool_turn(content: str, blocks: list[dict[str, Any]] | None = None) -> list[Message]:
    return [
        Message(role=MessageRole.USER, content="search"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="call_1", name="api_search", arguments={"query": "att"})],
        ),
        Message(
            role=MessageRole.TOOL,
            content=content,
            tool_call_id="call_1",
            content_blocks=blocks,
        ),
    ]


def _tool_content(converted: list[dict[str, Any]]) -> Any:
    return next(m for m in converted if m["role"] == "tool")["content"]


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("gemini/gemini-3.8-flash", "openai"),
        ("gemini/gemini-4-argon", "openai"),
        ("google/gemini-2.5-flash", "openrouter"),
        ("google/gemini-3.1-pro-preview", "openrouter"),
    ],
)
def test_every_gemini_preset_wraps_a_part_reference_on_the_wire(model: str, provider: str) -> None:
    client = LLMClient(ModelConfig(provider=provider, name=model))
    assert isinstance(client.capabilities.content_policy, GeminiContent)
    assert resolve_policy_names(client.capabilities)["content_policy"] == "gemini"

    converted = client._convert_messages(system=None, messages=_tool_turn(_SCHEMA_TEXT))
    assert json.loads(_tool_content(converted)) == {"content": _SCHEMA_TEXT}


def test_gemini_leaves_an_ordinary_json_result_structured() -> None:
    client = LLMClient(ModelConfig(provider="openai", name="gemini/gemini-4-argon"))
    plain = json.dumps({"results": [{"id": "jira.issues.get"}], "count": 1})
    converted = client._convert_messages(system=None, messages=_tool_turn(plain))
    assert _tool_content(converted) == plain


def test_gemini_wraps_on_the_content_blocks_path_too() -> None:
    client = LLMClient(ModelConfig(provider="openai", name="gemini/gemini-3.8-flash"))
    assert not client.supports_tool_image_blocks()
    converted = client._convert_messages(
        system=None,
        messages=_tool_turn("", blocks=[{"type": "text", "text": _SCHEMA_TEXT}]),
    )
    assert json.loads(_tool_content(converted)) == {"content": _SCHEMA_TEXT}


def test_an_openai_preset_sends_the_result_unchanged() -> None:
    client = LLMClient(ModelConfig(provider="openai", name="openai/gpt-5.5"))
    assert isinstance(client.capabilities.content_policy, OpenAIContent)
    assert client.capabilities.content_policy.wraps_part_reference_tool_results is False

    converted = client._convert_messages(system=None, messages=_tool_turn(_SCHEMA_TEXT))
    assert _tool_content(converted) == _SCHEMA_TEXT


def test_a_policy_without_the_attribute_is_read_as_not_wrapping() -> None:
    """An out-of-tree content policy that predates the attribute keeps working."""

    class LegacyContent:
        format = "openai"
        supports_images = False

    client = LLMClient(ModelConfig(provider="openai", name="openai/gpt-5.5"))
    object.__setattr__(client.capabilities, "content_policy", LegacyContent())
    converted = client._convert_messages(system=None, messages=_tool_turn(_SCHEMA_TEXT))
    assert _tool_content(converted) == _SCHEMA_TEXT


# ---------------------------------------------------------------------------
# 3. Through litellm's own Gemini converter
# ---------------------------------------------------------------------------


def _gemini_function_response(converted: list[dict[str, Any]]) -> dict[str, Any]:
    """What litellm's OpenAI → Gemini translation makes of the converted turn."""
    tool_msg = next(m for m in converted if m["role"] == "tool")
    call_msg = next(m for m in converted if m["role"] == "assistant")
    part = factory.convert_to_gemini_tool_call_result(tool_msg, call_msg)
    if isinstance(part, list):
        part = part[0]
    return part["function_response"]


def test_the_gemini_request_holds_no_ref_key_and_the_model_reads_the_same_text() -> None:
    client = LLMClient(ModelConfig(provider="openai", name="gemini/gemini-3.8-flash"))
    converted = client._convert_messages(system=None, messages=_tool_turn(_SCHEMA_TEXT))

    function_response = _gemini_function_response(converted)
    assert function_response["name"] == "api_search"
    assert not _ref_keys(
        function_response["response"]
    ), "a $ref key in function_response.response is what the Gemini API refuses"
    assert function_response["response"]["content"] == _SCHEMA_TEXT


def test_without_the_policy_the_converter_places_the_ref_into_the_response() -> None:
    """The control: an OpenAI-preset client lets the converter put the ``$ref``
    object into ``response`` — the shape the Gemini API rejects."""
    client = LLMClient(ModelConfig(provider="openai", name="openai/gpt-5.5"))
    converted = client._convert_messages(system=None, messages=_tool_turn(_SCHEMA_TEXT))
    assert _ref_keys(_gemini_function_response(converted)["response"]) == [
        "#/$defs/MultipartFileBinding"
    ]
