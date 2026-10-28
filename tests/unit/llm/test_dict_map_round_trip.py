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
from typing import Any, Literal
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
from tolokaforge.core.llm.schema_sanitizer import StrictSchema
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


@pytest.mark.parametrize(
    "model", ["openai/gpt-6.1-sol", "google/gemini-3.8-flash", "gemini/gemini-4-argon"]
)
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

    def test_dict_map_behind_all_of_reverses(self) -> None:
        """``{"allOf": [{"$ref": …}], "description": …}`` is how Pydantic v1 /
        OpenAPI 3.0 / hand-written MCP schemas attach a description to a ref.
        The sanitiser pivots inside ``allOf``; the reverser must follow."""
        schema = {
            "$defs": {
                "Args": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "object", "additionalProperties": {"type": "string"}}
                    },
                }
            },
            "type": "object",
            "properties": {
                "arguments": {"allOf": [{"$ref": "#/$defs/Args"}], "description": "Args."}
            },
        }
        wire = StrictSchema().sanitize(
            [{"type": "function", "function": {"name": "t", "parameters": schema}}]
        )
        wire_args = wire[0]["function"]["parameters"]["properties"]["arguments"]
        assert wire_args["allOf"][0]["properties"]["path"]["type"] == "array"
        emitted = {"arguments": {"path": [{"key": "issueId", "value": "PROJ-1"}]}}
        assert reverse_dict_map_arrays(emitted, schema) == {
            "arguments": {"path": {"issueId": "PROJ-1"}}
        }

    def test_discriminated_union_picks_the_variant_that_declares_the_map(self) -> None:
        """``Union[Plain, WithMap]``: the first object branch fits any dict,
        but the value is a ``WithMap`` — its discriminator says so and the
        ``lines`` map lives only there."""

        class _Plain(BaseModel):
            kind: Literal["plain"]
            note: str = ""

        class _WithMap(BaseModel):
            kind: Literal["with_map"]
            lines: dict[str, _Line]

        class _Call(BaseModel):
            order: _Plain | _WithMap = Field(discriminator="kind")

        schema = TypeAdapter(_Call).json_schema()
        emitted = {"order": {"kind": "with_map", "lines": [{"key": "SKU-1", "qty": 2}]}}
        assert reverse_dict_map_arrays(emitted, schema) == {
            "order": {"kind": "with_map", "lines": {"SKU-1": {"qty": 2}}}
        }
        plain = {"order": {"kind": "plain", "note": "n"}}
        assert reverse_dict_map_arrays(plain, schema) == plain

    def test_undiscriminated_union_picks_the_branch_sharing_the_most_fields(self) -> None:
        class _Plain(BaseModel):
            note: str = ""

        class _WithMap(BaseModel):
            lines: dict[str, _Line]
            note: str = ""

        class _Call(BaseModel):
            order: _Plain | _WithMap

        schema = TypeAdapter(_Call).json_schema()
        emitted = {"order": {"note": "n", "lines": [{"key": "SKU-1", "qty": 2}]}}
        assert reverse_dict_map_arrays(emitted, schema) == {
            "order": {"note": "n", "lines": {"SKU-1": {"qty": 2}}}
        }

    def test_node_with_properties_and_typed_additional_properties_round_trips(self) -> None:
        """The sanitiser pivots *any* typed ``additionalProperties`` (dropping
        the ``properties``), so the reverser must treat the node as a map too
        — one definition of dict-map on both sides."""
        schema = {
            "type": "object",
            "properties": {
                "counts": {
                    "type": "object",
                    "properties": {"total": {"type": "integer"}},
                    "additionalProperties": {"type": "integer"},
                }
            },
        }
        wire = StrictSchema().sanitize(
            [{"type": "function", "function": {"name": "t", "parameters": schema}}]
        )
        counts_wire = wire[0]["function"]["parameters"]["properties"]["counts"]
        assert counts_wire["type"] == "array"
        emitted = {"counts": [{"key": "total", "value": 3}, {"key": "a", "value": 1}]}
        assert reverse_dict_map_arrays(emitted, schema) == {"counts": {"total": 3, "a": 1}}

    def test_flat_item_on_a_scalar_map_requires_the_value_to_fit(self) -> None:
        """``{"key": "AIza…", "value": "42"}`` on a ``Dict[str, int]`` is a
        two-entry map (an API-key parameter plus a ``value`` filter), not one
        pivoted item; ``{"key": "a", "value": 5}`` is the item."""

        class _Call(BaseModel):
            query: dict[str, int]

        schema = TypeAdapter(_Call).json_schema()
        native = {"query": {"key": "AIza", "value": "42"}}
        assert reverse_dict_map_arrays(native, schema) == native
        assert reverse_dict_map_arrays({"query": {"key": "a", "value": 5}}, schema) == {
            "query": {"a": 5}
        }

    def test_map_of_maps_native_inner_value_entry_is_not_unwrapped(self) -> None:
        """``{host: {"value": "token"}}`` on a ``Dict[str, Dict[str, str]]``
        is a genuine inner map — ``"token"`` cannot be the inner map — while
        the wire shape (inner map carried under ``value``) still reverses."""

        class _Call(BaseModel):
            headers_by_host: dict[str, dict[str, str]]

        schema = TypeAdapter(_Call).json_schema()
        native = {"headers_by_host": {"api.example.com": {"value": "token"}}}
        assert reverse_dict_map_arrays(native, schema) == native
        wire = {
            "headers_by_host": [
                {"key": "api.example.com", "value": [{"key": "Authorization", "value": "t"}]}
            ]
        }
        assert reverse_dict_map_arrays(wire, schema) == {
            "headers_by_host": {"api.example.com": {"Authorization": "t"}}
        }

    def test_stringified_nested_map_is_decoded_and_reversed(self) -> None:
        """``coerce_json_strings`` only sees the root; one level down the walk
        has the schema and decodes a stringified container itself. A
        ``string`` field that happens to hold JSON text is never decoded."""

        class _Args(BaseModel):
            path: dict[str, str]
            raw: str = ""

        class _Call(BaseModel):
            arguments: _Args

        schema = TypeAdapter(_Call).json_schema()
        emitted = {
            "arguments": {
                "path": json.dumps([{"key": "issueId", "value": "PROJ-1"}]),
                "raw": '{"not": "decoded"}',
            }
        }
        assert reverse_dict_map_arrays(emitted, schema) == {
            "arguments": {"path": {"issueId": "PROJ-1"}, "raw": '{"not": "decoded"}'}
        }
        stringified_object = {"arguments": json.dumps({"path": {"a": "b"}, "raw": "x"})}
        assert reverse_dict_map_arrays(stringified_object, schema) == {
            "arguments": {"path": {"a": "b"}, "raw": "x"}
        }


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
