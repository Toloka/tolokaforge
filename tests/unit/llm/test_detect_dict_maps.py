"""Unit tests for :func:`detect_dict_maps`.

The helper drives :class:`~tolokaforge.core.llm.prompt_policy.DictMapHints`'s
enrichment, so any property shape it misses is silently un-hinted on the wire.

Key Pydantic shapes the production tool schemas use:

- ``Dict[str, T]``                       → top-level ``additionalProperties``
- ``Optional[Dict[str, T]]``             → ``anyOf[{additionalProperties:T}, null]``
- ``Optional[Dict[str, T]]`` with ``$ref`` value → same as above but the inner
  schema uses ``$ref`` to a ``$defs`` block instead of inline ``properties``

The first shape was the only one the original Stage 2 implementation handled.
The diagnosis on ``tau_manufacturing_v2`` traced 805 dict-map failures /
250 trials to ``modify_order.upsert_lines`` (the Optional case) silently
losing its hint. This test pins the recursion contract.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.prompt_policy import detect_dict_maps

pytestmark = pytest.mark.unit


def _tool(name: str, properties: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": properties},
        },
    }


_VALUE_SCHEMA = {
    "type": "object",
    "properties": {
        "qty": {"type": "number"},
        "sku": {"type": "string"},
    },
    "required": ["qty"],
}


class TestDetectDictMaps:
    def test_top_level_dict_map_is_detected(self) -> None:
        """Baseline: a non-Optional ``Dict[str, T]`` lands at the top of the
        property and the helper must surface it."""
        tools = [
            _tool(
                "create_order",
                {
                    "lines": {
                        "type": "object",
                        "additionalProperties": _VALUE_SCHEMA,
                        "description": "Map of sku → line.",
                    },
                },
            )
        ]
        result = detect_dict_maps(tools)
        names = [(d.tool_name, d.param_name) for d in result]
        assert names == [("create_order", "lines")]

    def test_optional_dict_map_via_anyof_null_is_detected(self) -> None:
        """Pydantic emits ``Optional[Dict[str, T]]`` as
        ``anyOf=[{additionalProperties:T, type:object}, {type:null}]``.
        The helper must recurse into ``anyOf`` branches and surface the
        inner dict-map. This is the single highest-impact bug from the
        ``tau_manufacturing_v2`` diagnosis (805 / 250 modify_order failures)."""
        tools = [
            _tool(
                "modify_order",
                {
                    "upsert_lines": {
                        "anyOf": [
                            {
                                "additionalProperties": _VALUE_SCHEMA,
                                "type": "object",
                            },
                            {"type": "null"},
                        ],
                        "default": None,
                        "description": "Optional map of sku → line.",
                    },
                },
            )
        ]
        result = detect_dict_maps(tools)
        names = [(d.tool_name, d.param_name) for d in result]
        assert (
            "modify_order",
            "upsert_lines",
        ) in names, f"Optional dict-map must be detected; got {names}"

    def test_anyof_branch_without_null_is_detected(self) -> None:
        """``anyOf`` without a null branch (e.g. ``Union[Dict[str, T], list[T]]``)
        still surfaces the dict-map branch — recursion is unconditional."""
        tools = [
            _tool(
                "flexible",
                {
                    "payload": {
                        "anyOf": [
                            {"additionalProperties": _VALUE_SCHEMA, "type": "object"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                },
            )
        ]
        result = detect_dict_maps(tools)
        names = [(d.tool_name, d.param_name) for d in result]
        assert ("flexible", "payload") in names

    def test_optional_dict_map_carries_value_schema(self) -> None:
        """The detected entry's ``value_schema`` must be the inner one,
        not ``None`` and not the outer ``anyOf`` wrapper. The hint
        generator depends on this to emit field-level guidance."""
        tools = [
            _tool(
                "modify_order",
                {
                    "upsert_lines": {
                        "anyOf": [
                            {"additionalProperties": _VALUE_SCHEMA, "type": "object"},
                            {"type": "null"},
                        ],
                    },
                },
            )
        ]
        result = detect_dict_maps(tools)
        assert len(result) == 1
        dm = result[0]
        assert dm.value_schema == _VALUE_SCHEMA, (
            "The Optional[Dict[str, T]] case must surface the INNER value schema, "
            f"not the outer anyOf wrapper. Got: {dm.value_schema!r}"
        )
        assert dict(dm.value_fields) == {"qty": "number", "sku": "string"}

    def test_no_dict_map_returns_empty(self) -> None:
        """No false positives on plain object / array / scalar shapes."""
        tools = [
            _tool(
                "plain",
                {
                    "name": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "obj": {"type": "object", "properties": {"k": {"type": "string"}}},
                    "maybe_str": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            )
        ]
        assert detect_dict_maps(tools) == []

    def test_boolean_additional_properties_recursed_through_anyof(self) -> None:
        """``Optional[dict[str, Any]]`` (Pydantic emits ``additionalProperties: True``
        nested under ``anyOf``) should still be detected, with no value schema."""
        tools = [
            _tool(
                "free_form",
                {
                    "metadata": {
                        "anyOf": [
                            {"additionalProperties": True, "type": "object"},
                            {"type": "null"},
                        ],
                        "description": "Free-form metadata bag.",
                    },
                },
            )
        ]
        result = detect_dict_maps(tools)
        assert len(result) == 1
        dm = result[0]
        assert dm.value_schema is None
        assert dm.param_description == "Free-form metadata bag."
