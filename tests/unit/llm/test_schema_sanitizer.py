"""Unit tests for :class:`ToolSchemaSanitizer` Protocol conformance.

Stage 0 only verifies that :class:`PassthroughSchema` and :class:`StrictSchema`
satisfy the Protocol surface and advertise a reasonable capability set. Stage 1
layers the behavioural assertions (RE2 guard, pattern stripping, etc.).
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm.schema_sanitizer import (
    PassthroughSchema,
    SchemaCapability,
    StrictSchema,
    ToolSchemaSanitizer,
)

pytestmark = pytest.mark.unit


def _simple_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "ping",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    }


class TestPassthroughSchema:
    def test_implements_protocol(self) -> None:
        sanitizer: ToolSchemaSanitizer = PassthroughSchema()
        assert isinstance(sanitizer, ToolSchemaSanitizer)

    def test_sanitize_returns_input(self) -> None:
        sanitizer = PassthroughSchema()
        tools = [_simple_tool()]
        assert sanitizer.sanitize(tools) is tools

    def test_supports_every_capability(self) -> None:
        sanitizer = PassthroughSchema()
        assert sanitizer.supported_capabilities() == frozenset(SchemaCapability)


class TestStrictSchema:
    def test_implements_protocol(self) -> None:
        sanitizer: ToolSchemaSanitizer = StrictSchema()
        assert isinstance(sanitizer, ToolSchemaSanitizer)

    def test_sanitize_preserves_metadata_keys(self) -> None:
        """Position-aware sanitiser preserves JSON-Schema metadata keys —
        ``title``, ``examples``, ``minProperties`` carry useful signal and
        are accepted by every current function-calling provider. Only the
        parameters-root ``description`` (Pydantic class-docstring artefact)
        is removed.
        """
        sanitizer = StrictSchema()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "parameters": {
                        "title": "PingInput",
                        "examples": [{"message": "hello"}],
                        "minProperties": 1,
                        "description": "Pydantic class docstring noise.",
                        "type": "object",
                        "required": ["message"],
                        "properties": {"message": {"type": "string"}},
                    },
                },
            }
        ]
        result = sanitizer.sanitize(tools)
        params = result[0]["function"]["parameters"]
        assert (
            params.get("title") == "PingInput"
        ), f"title carries useful signal; preserve it. Got {params!r}"
        assert params.get("examples") == [{"message": "hello"}]
        assert params.get("minProperties") == 1
        # Only parameters-root ``description`` is intentionally stripped.
        assert "description" not in params
        assert "properties" in params

    def test_capability_exclusion_set(self) -> None:
        """``StrictSchema`` rewrites only typed dict-maps and the Pydantic
        Decimal numeric/string union; everything else passes through. Safe
        regex patterns and ``format: date-time`` survive — see
        ``test_schema_sanitizer_strict.py`` for full behavioural coverage.
        """
        caps = StrictSchema().supported_capabilities()
        assert SchemaCapability.DICT_MAP_TYPED not in caps
        assert SchemaCapability.ANYOF_NUMERIC_STRING not in caps
        assert SchemaCapability.REGEX_PATTERN in caps
        assert SchemaCapability.DATE_TIME_FORMAT in caps
