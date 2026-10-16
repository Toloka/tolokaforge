"""Dict-map round trip — every preset that pivots ``Dict[str, T]`` to an array
on the wire gets the native dict back, values included, at every depth.

Two field-reported defects (IT domain, HTTP ``path`` / ``query`` maps):

1. ``StrictSchema`` dropped the *values* of scalar dict-maps: ``Dict[str,
   str]`` went out as ``[{key}]`` because ``carry_scalar_dict_map_value``
   defaulted to ``False`` and only the ``gemini_recursive`` lineage overrode
   it. ``openai/gpt-6.1-sol`` (``strict``) and ``google/gemini-3.8-flash``
   (generic ``gemini``) received ``path`` / ``query`` with keys only, and
   every call failed.
2. ``ArrayDictMapResponse`` reversed the pivot at the **root only**. A map
   nested inside an object parameter (``arguments.path``) reached the tool
   as a list and Pydantic answered ``Input should be a valid dictionary``.

Both are closed in the base: the carriage is on by default and the reverse
pivot is driven by the tool's original schema (``param_schema``), so it fires
wherever the schema declares a dict-map and nowhere else.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, Field, JsonValue, TypeAdapter

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.client import (
    LLMClient,
    _param_schemas_by_tool,
    _policy_accepts_param_schema,
    _root_param_types_by_tool,
)
from tolokaforge.core.llm.response_policy import (
    ArrayDictMapResponse,
    reverse_dict_map_arrays,
)
from tolokaforge.core.llm.schema_sanitizer import StrictSchema, is_free_form_schema
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The IT-domain shape: an HTTP call whose path / query are string maps nested
# inside an object parameter, plus the other dict-map idioms a domain carries.
# ---------------------------------------------------------------------------


class _Native(BaseModel):
    method: str = "GET"
    path: dict[str, str] = Field(default_factory=dict, description="Path params.")
    query: dict[str, JsonValue] = Field(default_factory=dict, description="Query params.")


class _Header(BaseModel):
    """A list item that happens to carry a field named ``key``."""

    key: str
    value: str


class _Line(BaseModel):
    qty: int
    attrs: dict[str, int] = Field(default_factory=dict)


class _CallApi(BaseModel):
    arguments: _Native
    query: dict[str, str] | None = None
    headers: list[_Header] = Field(default_factory=list)
    lines: dict[str, _Line] = Field(default_factory=dict)
    body: dict[str, JsonValue] = Field(default_factory=dict)


def _tool(model: type[BaseModel] = _CallApi, name: str = "call_api") -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Call an HTTP API.",
            "parameters": TypeAdapter(model).json_schema(),
        },
    }


_NATIVE_ARGS: dict[str, Any] = {
    "arguments": {
        "method": "GET",
        # ``key`` as a map KEY (an API-key query parameter is common).
        "path": {"issueId": "PROJ-1", "key": "api"},
        "query": {"fields": "summary", "max": 5, "nested": {"a": [1]}},
    },
    "query": {"page": "2", "key": "secret"},
    "headers": [{"key": "Accept", "value": "application/json"}],
    "lines": {"SKU-A": {"qty": 1, "attrs": {"w": 2}}},
    "body": {"x": 1, "y": {"z": "w"}},
}


def _emit_from_wire(schema: dict[str, Any], native: Any) -> Any:
    """What a model that follows the sanitised wire schema emits for *native*."""
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            if branch.get("type") != "null":
                return _emit_from_wire(branch, native)
    declared = schema.get("type")
    if declared == "array" and isinstance(native, dict):
        # A pivoted dict-map: one ``{key, …}`` item per entry.
        props = schema["items"]["properties"]
        items = []
        for k, v in native.items():
            item: dict[str, Any] = {"key": k}
            if set(props) == {"key", "value"}:
                item["value"] = _emit_from_wire(props["value"], v)
            else:
                for pk, ps in props.items():
                    if pk != "key" and pk in v:
                        item[pk] = _emit_from_wire(ps, v[pk])
            items.append(item)
        return items
    if declared == "array" and isinstance(native, list):
        return [_emit_from_wire(schema["items"], x) for x in native]
    if declared == "object" and "properties" in schema and isinstance(native, dict):
        props = schema["properties"]
        return {k: _emit_from_wire(props[k], v) for k, v in native.items() if k in props}
    return native


_PIVOTING_MODELS = [
    "openai/gpt-6.1-sol",
    "openai/gpt-5.5",
    "google/gemini-3.8-flash",
    "google/gemini-3.7-flash",
    "google/gemini-3.1-pro-preview",
    "x-ai/grok-4.6",
    "x-ai/grok-5",
    "azure_ai/cohere-command-a-plus",
    "azure_ai/cohere-command-a-plus-05-2026",
]


@pytest.mark.parametrize("model", _PIVOTING_MODELS)
def test_every_pivoting_preset_round_trips_dict_maps_at_every_depth(model: str) -> None:
    """Sanitise → emit per the wire schema → parse: the tool sees the native
    arguments, values included, root and nested alike."""
    caps = build_capabilities(model, "openrouter")
    tool = _tool()
    sanitised = caps.schema_sanitizer.sanitize([tool])
    wire = sanitised[0]["function"]["parameters"]

    path_wire = wire["properties"]["arguments"]["properties"]["path"]
    assert path_wire["type"] == "array", f"{model}: nested Dict[str, str] must pivot on the wire"
    assert set(path_wire["items"]["properties"]) == {"key", "value"}, (
        f"{model}: scalar map items must carry the value, got "
        f"{list(path_wire['items']['properties'])}"
    )

    emitted = json.loads(json.dumps(_emit_from_wire(wire, _NATIVE_ARGS)))
    assert isinstance(emitted["arguments"]["path"], list), "precondition: emitted as array"

    recovered = caps.response_policy.parse_arguments(
        emitted,
        param_types=_root_param_types_by_tool(sanitised).get("call_api"),
        param_schema=tool["function"]["parameters"],
    )
    assert recovered == _NATIVE_ARGS


@pytest.mark.parametrize("model", ["openai/gpt-6.1-sol", "google/gemini-3.8-flash"])
def test_native_dict_emission_passes_through_and_value_wrapper_unwraps(model: str) -> None:
    """A model that ignores the wire shape and emits the native dict is left
    alone; the ``{k: {"value": v}}`` wrapper some models emit is unwrapped."""
    caps = build_capabilities(model, "openrouter")
    tool = _tool()
    emitted = json.loads(json.dumps(_NATIVE_ARGS))
    emitted["query"] = {"page": {"value": "2"}, "key": "secret"}
    recovered = caps.response_policy.parse_arguments(
        emitted,
        param_types=_root_param_types_by_tool(caps.schema_sanitizer.sanitize([tool])).get(
            "call_api"
        ),
        param_schema=tool["function"]["parameters"],
    )
    assert recovered == _NATIVE_ARGS


# ---------------------------------------------------------------------------
# Sanitiser: scalar carriage by default, untyped maps left free-form.
# ---------------------------------------------------------------------------


class TestStrictSchemaScalarDictMapCarriage:
    def test_scalar_map_carries_value_by_default(self) -> None:
        class _Call(BaseModel):
            path: dict[str, str] = Field(description="Path params.")

        wire = StrictSchema().sanitize([_tool(_Call)])[0]["function"]["parameters"]
        path = wire["properties"]["path"]
        assert path["type"] == "array"
        items = path["items"]
        assert items["properties"]["key"]["type"] == "string"
        assert items["properties"]["value"]["type"] == "string"
        assert set(items["required"]) == {"key", "value"}
        assert "'value' field" in path["description"], path["description"]

    def test_int_map_value_keeps_its_scalar_schema(self) -> None:
        class _Call(BaseModel):
            counts: dict[str, int] = Field(ge=0)

        wire = StrictSchema().sanitize([_tool(_Call)])[0]["function"]["parameters"]
        value = wire["properties"]["counts"]["items"]["properties"]["value"]
        assert value["type"] == "integer"
        assert value["description"] == "The map value."

    def test_untyped_map_becomes_free_form_object_not_array(self) -> None:
        """``Dict[str, JsonValue]`` compiles to ``additionalProperties: {}``
        (an empty ``$ref`` target). There is nothing to lift onto an items
        object, so the map stays an object with the ``true`` marker."""

        class _Call(BaseModel):
            query: dict[str, JsonValue] = Field(description="Query params.")

        wire = StrictSchema().sanitize([_tool(_Call)])[0]["function"]["parameters"]
        query = wire["properties"]["query"]
        assert query["type"] == "object"
        assert query["additionalProperties"] is True
        assert "items" not in query
        assert "$defs" not in wire

    def test_object_valued_map_still_lifts_properties_without_value(self) -> None:
        class _Call(BaseModel):
            lines: dict[str, _Line]

        wire = StrictSchema().sanitize([_tool(_Call)])[0]["function"]["parameters"]
        items = wire["properties"]["lines"]["items"]["properties"]
        assert set(items) == {"key", "qty", "attrs"}
        # The nested scalar map inside the value type carries its value too.
        assert set(items["attrs"]["items"]["properties"]) == {"key", "value"}

    @pytest.mark.parametrize(
        ("schema", "free"),
        [
            ({}, True),
            ({"title": "JsonValue", "description": "any"}, True),
            ({"type": "string"}, False),
            ({"$ref": "#/$defs/X"}, False),
            ({"anyOf": [{"type": "string"}]}, False),
            ({"additionalProperties": True}, False),
            ("not a dict", False),
        ],
    )
    def test_is_free_form_schema(self, schema: Any, free: bool) -> None:
        assert is_free_form_schema(schema) is free


# ---------------------------------------------------------------------------
# Reverse pivot: schema-driven, recursive, and only where the schema says so.
# ---------------------------------------------------------------------------


class TestReverseDictMapArrays:
    def test_list_of_items_with_key_field_is_not_a_dict_map(self) -> None:
        """``list[Header]`` where ``Header`` has a ``key`` field: the schema
        says array, so the shape heuristic that would pivot it never runs."""
        schema = TypeAdapter(_CallApi).json_schema()
        emitted = {"headers": [{"key": "Accept", "value": "application/json"}]}
        assert reverse_dict_map_arrays(emitted, schema) == emitted

    def test_value_type_with_its_own_value_field_is_not_unwrapped(self) -> None:
        class _Setting(BaseModel):
            value: str

        class _Call(BaseModel):
            settings: dict[str, _Setting]

        schema = TypeAdapter(_Call).json_schema()
        emitted = {"settings": [{"key": "theme", "value": "dark"}]}
        assert reverse_dict_map_arrays(emitted, schema) == {
            "settings": {"theme": {"value": "dark"}}
        }

    def test_scalar_map_single_flat_item_pivots(self) -> None:
        class _Call(BaseModel):
            path: dict[str, str]

        schema = TypeAdapter(_Call).json_schema()
        assert reverse_dict_map_arrays({"path": {"key": "id", "value": "42"}}, schema) == {
            "path": {"id": "42"}
        }

    def test_scalar_map_native_dict_keyed_by_key_is_a_map(self) -> None:
        """``{"key": "abc"}`` is a one-entry map (an API-key parameter), not a
        flat item: a flat scalar item is exactly ``{key, value}``."""

        class _Call(BaseModel):
            query: dict[str, str]

        schema = TypeAdapter(_Call).json_schema()
        assert reverse_dict_map_arrays({"query": {"key": "abc"}}, schema) == {
            "query": {"key": "abc"}
        }

    def test_empty_array_for_a_dict_map_becomes_empty_dict(self) -> None:
        class _Call(BaseModel):
            path: dict[str, str]
            tags: list[str]

        schema = TypeAdapter(_Call).json_schema()
        assert reverse_dict_map_arrays({"path": [], "tags": []}, schema) == {"path": {}, "tags": []}

    def test_dict_map_inside_array_items_reverses(self) -> None:
        class _Step(BaseModel):
            env: dict[str, str]

        class _Call(BaseModel):
            steps: list[_Step]

        schema = TypeAdapter(_Call).json_schema()
        emitted = {"steps": [{"env": [{"key": "A", "value": "1"}]}]}
        assert reverse_dict_map_arrays(emitted, schema) == {"steps": [{"env": {"A": "1"}}]}

    def test_union_with_dict_map_branch_picks_the_map_for_key_items(self) -> None:
        class _Call(BaseModel):
            target: str | dict[str, str]

        schema = TypeAdapter(_Call).json_schema()
        assert reverse_dict_map_arrays({"target": [{"key": "a", "value": "b"}]}, schema) == {
            "target": {"a": "b"}
        }
        assert reverse_dict_map_arrays({"target": "plain"}, schema) == {"target": "plain"}

    def test_recursive_schema_does_not_hang(self) -> None:
        class _Node(BaseModel):
            children: dict[str, _Node] = Field(default_factory=dict)
            label: str = ""

        schema = TypeAdapter(_Node).json_schema()
        emitted = {
            "label": "root",
            "children": [
                {"key": "a", "label": "A", "children": [{"key": "b", "label": "B", "children": []}]}
            ],
        }
        assert reverse_dict_map_arrays(emitted, schema) == {
            "label": "root",
            "children": {"a": {"label": "A", "children": {"b": {"label": "B", "children": {}}}}},
        }

    def test_undeclared_fields_and_non_dict_arguments_pass_through(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        assert reverse_dict_map_arrays({"a": "x", "extra": [{"key": "k"}]}, schema) == {
            "a": "x",
            "extra": [{"key": "k"}],
        }
        assert reverse_dict_map_arrays(["not", "a", "dict"], schema) == ["not", "a", "dict"]  # type: ignore[arg-type]

    def test_free_form_map_is_untouched(self) -> None:
        schema = {
            "type": "object",
            "properties": {"body": {"type": "object", "additionalProperties": {}}},
        }
        emitted = {"body": {"k": [{"key": "looks-pivoted"}]}}
        assert reverse_dict_map_arrays(emitted, schema) == emitted


class TestArrayDictMapResponseWithoutSchemaKeepsTheRootHeuristic:
    """A caller on the two-argument contract gets the historical behaviour."""

    def test_root_list_pivots_and_nested_list_does_not(self) -> None:
        policy = ArrayDictMapResponse()
        emitted = {
            "lines": [{"key": "SKU-A", "qty": 1}],
            "arguments": {"path": [{"key": "id", "value": "42"}]},
        }
        assert policy.parse_arguments(emitted, param_types={"lines": "dict_map"}) == {
            "lines": {"SKU-A": {"qty": 1}},
            "arguments": {"path": [{"key": "id", "value": "42"}]},
        }


# ---------------------------------------------------------------------------
# Client wiring: the original schema reaches the policy; old policies survive.
# ---------------------------------------------------------------------------


def _tool_call_response(name: str, arguments: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    message.content = ""
    message.thinking_blocks = None
    message.reasoning_content = None
    tc = MagicMock()
    tc.id = "call_1"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    message.tool_calls = [tc]
    choice.message = message
    response.choices = [choice]
    response.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


class TestLLMClientThreadsParamSchemaIntoResponsePolicy:
    @pytest.mark.parametrize("model", ["openai/gpt-6.1-sol", "google/gemini-3.8-flash"])
    def test_nested_path_map_reaches_the_tool_as_a_dict(self, model: str) -> None:
        client = LLMClient(ModelConfig(provider="openrouter", name=model))
        emitted = {
            "arguments": {
                "method": "GET",
                "path": [{"key": "issueId", "value": "PROJ-1"}],
                "query": {"fields": "summary"},
            },
            "query": [{"key": "page", "value": "2"}],
        }
        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            mock_completion.return_value = _tool_call_response("call_api", emitted)
            result = client.generate(
                system=None,
                messages=[Message(role=MessageRole.USER, content="get the issue")],
                tools=[_tool()],
            )
        assert result.tool_calls[0].arguments == {
            "arguments": {
                "method": "GET",
                "path": {"issueId": "PROJ-1"},
                "query": {"fields": "summary"},
            },
            "query": {"page": "2"},
        }

    def test_a_policy_on_the_two_argument_contract_is_still_called(self) -> None:
        """An older ``tolokaforge-models`` wheel or an out-of-tree policy that
        does not know ``param_schema`` must not break the call."""

        class _OldStyle:
            seen: list[dict[str, Any]] = []

            def parse_arguments(
                self, arguments: dict[str, Any], *, param_types: Any = None
            ) -> dict[str, Any]:
                self.seen.append({"param_types": param_types})
                return arguments

        assert _policy_accepts_param_schema(_OldStyle()) is False
        assert _policy_accepts_param_schema(ArrayDictMapResponse()) is True

        client = LLMClient(ModelConfig(provider="openrouter", name="openai/gpt-6.1-sol"))
        object.__setattr__(client.capabilities, "response_policy", _OldStyle())
        emitted = {"query": [{"key": "page", "value": "2"}]}
        with patch("tolokaforge.core.llm.client.completion") as mock_completion:
            mock_completion.return_value = _tool_call_response("call_api", emitted)
            result = client.generate(
                system=None,
                messages=[Message(role=MessageRole.USER, content="go")],
                tools=[_tool()],
            )
        assert result.tool_calls[0].arguments == emitted
        assert _OldStyle.seen and _OldStyle.seen[0]["param_types"]["query"] == "dict_map"

    def test_param_schemas_by_tool_reads_the_original_parameters(self) -> None:
        tool = _tool()
        out = _param_schemas_by_tool([tool, {"type": "function"}, "junk"])  # type: ignore[list-item]
        assert out == {"call_api": tool["function"]["parameters"]}
        assert "$defs" in out["call_api"], "the original, not the sanitised, schema"
        assert _param_schemas_by_tool(None) == {}
