""":class:`StrictSchema` contract — RE2-safe rewrites only, never over-strip.

Exercises the :class:`ToolSchemaSanitizer` Protocol (not private helpers) using
a Pydantic-generated JSON Schema for a ``Decimal`` field. Pydantic emits

``anyOf: [{type: number}, {type: string, pattern: "^(?!^[-+.]*$)…$"}]``

for every ``Decimal``; the negative look-ahead ``(?!…)`` is rejected by
OpenAI's RE2 validator and causes GPT-5 / GPT-5.5 to 500 on every tool call
whose tool list carries such a schema (see
[`plans/eval_output_new_diagnosis.md`](../../../plans/eval_output_new_diagnosis.md)
Part 1).

Contract guarantees asserted here:

1. The Pydantic Decimal ``anyOf`` idiom collapses to ``{type: "number"}``
   with the original ``description`` preserved.
2. No RE2-incompatible substring (lookarounds / backreferences) survives.
3. ``format: date-time`` and other valid formats are **preserved** — they
   are accepted by every current function-calling provider and removing
   them throws away a useful hint. (Position-aware contract from
   :file:`test_schema_sanitizer_position_aware.py`.)
4. RE2-safe ``pattern`` values **survive** — only lookaround / backref
   patterns trip the conditional strip.
5. Scalar fields unchanged (regression guard — no over-stripping).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.llm.schema_sanitizer import (
    SchemaCapability,
    StrictSchema,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test fixtures — Pydantic-generated (break automatically on upstream changes)
# ---------------------------------------------------------------------------


class _Amount(BaseModel):
    amount: Decimal = Field(description="A monetary amount")
    occurred_at: datetime = Field(description="When it happened")
    note: str | None = None


class _CountOnly(BaseModel):
    """Regression guard: a trivial scalar model with no stripped features."""

    count: int = Field(description="A simple count")


def _build_tool(model: type[BaseModel], name: str, desc: str) -> dict[str, Any]:
    schema = TypeAdapter(model).json_schema()
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": schema,
        },
    }


# ---------------------------------------------------------------------------
# Recursive-walker helpers (operate on the Protocol's return type: list[dict])
# ---------------------------------------------------------------------------


def _walk(node: Any):
    """Yield every (parent, key, value) triple in nested dicts/lists."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield node, k, v
            yield from _walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _find_keys(obj: Any, key: str) -> list[Any]:
    """Return every value mapped to ``key`` anywhere in the structure."""
    return [v for _parent, k, v in _walk(obj) if k == key]


_RE2_INCOMPAT = re.compile(r"\(\?!|\(\?=|\(\?<!|\(\?<=|\\[1-9]")


def _assert_no_re2_incompat(obj: Any) -> None:
    serialised = json.dumps(obj)
    match = _RE2_INCOMPAT.search(serialised)
    assert match is None, (
        f"RE2-incompatible pattern leaked past StrictSchema: {match.group()!r} "
        f"in {serialised[max(0, match.start() - 40) : match.end() + 40]!r}"
    )


# ---------------------------------------------------------------------------
# Sanity check on the upstream Pydantic shape — breaks loudly if Pydantic
# changes its Decimal encoding so we rewrite the sanitizer, not the test.
# ---------------------------------------------------------------------------


def test_pydantic_decimal_still_emits_poisoned_pattern() -> None:
    """Guard: if Pydantic stops emitting the look-ahead regex, revisit fix."""
    schema = TypeAdapter(_Amount).json_schema()
    amount_schema = schema["properties"]["amount"]
    assert "anyOf" in amount_schema
    string_branch = next(b for b in amount_schema["anyOf"] if b.get("type") == "string")
    assert "pattern" in string_branch
    assert "(?!" in string_branch["pattern"]


# ---------------------------------------------------------------------------
# Contract assertions against the Protocol surface
# ---------------------------------------------------------------------------


class TestStrictSchemaScrubsRE2Incompatible:
    """Every assertion here must hold for the :class:`StrictSchema` output."""

    @pytest.fixture
    def sanitised_tool(self) -> dict[str, Any]:
        tool = _build_tool(_Amount, name="charge", desc="Charge a card")
        result = StrictSchema().sanitize([tool])
        assert len(result) == 1
        return result[0]

    def test_no_re2_incompatible_pattern_survives(self, sanitised_tool: dict[str, Any]) -> None:
        """The Decimal look-ahead pattern must not survive sanitisation —
        but RE2-safe patterns elsewhere are fine and tested separately
        (:file:`test_schema_sanitizer_position_aware.py`).
        """
        for _parent, key, value in _walk(sanitised_tool):
            if key != "pattern":
                continue
            assert isinstance(value, str)
            assert not _RE2_INCOMPAT.search(value), (
                "RE2-incompatible pattern survived sanitisation: "
                f"pattern={value!r} — GPT-5's RE2 validator rejects this."
            )

    def test_no_re2_incompatible_substrings(self, sanitised_tool: dict[str, Any]) -> None:
        _assert_no_re2_incompat(sanitised_tool)

    def test_decimal_anyof_collapsed_to_number(self, sanitised_tool: dict[str, Any]) -> None:
        params = sanitised_tool["function"]["parameters"]
        amount = params["properties"]["amount"]
        assert "anyOf" not in amount, (
            "Pydantic Decimal anyOf{number, string+pattern} must collapse to "
            f"plain number. Got: {amount!r}"
        )
        assert amount.get("type") == "number", f"Expected amount.type='number', got {amount!r}"

    def test_decimal_description_preserved(self, sanitised_tool: dict[str, Any]) -> None:
        params = sanitised_tool["function"]["parameters"]
        amount = params["properties"]["amount"]
        assert amount.get("description") == "A monetary amount"

    def test_datetime_format_preserved(self, sanitised_tool: dict[str, Any]) -> None:
        """``format: date-time`` is accepted by every current provider —
        sanitiser must preserve it so the model knows the expected shape."""
        params = sanitised_tool["function"]["parameters"]
        occurred_at = params["properties"]["occurred_at"]
        assert occurred_at.get("type") == "string"
        assert occurred_at.get("format") == "date-time", (
            "`format: date-time` is informational metadata accepted by all "
            f"providers; sanitiser must preserve it. Got {occurred_at!r}"
        )

    def test_parameters_structure_preserved(self, sanitised_tool: dict[str, Any]) -> None:
        params = sanitised_tool["function"]["parameters"]
        assert params.get("type") == "object"
        assert "properties" in params
        # Only the three declared fields on ``_Amount``
        assert set(params["properties"].keys()) == {"amount", "occurred_at", "note"}


class TestStrictSchemaDoesNotOverstrip:
    """Regression guard — simple scalar tools must come through unchanged
    in substance."""

    def test_plain_int_param_unchanged(self) -> None:
        tool = _build_tool(_CountOnly, name="tally", desc="Tally items")
        result = StrictSchema().sanitize([tool])
        params = result[0]["function"]["parameters"]
        count = params["properties"]["count"]
        assert count["type"] == "integer"
        assert count.get("description") == "A simple count"
        # Integer schemas never carry pattern/format — confirm no drift.
        assert "pattern" not in count
        assert "format" not in count


class TestStrictSchemaResolvesRefs:
    """Pydantic emits nested-model schemas as ``$ref: #/$defs/<Model>``.
    StrictSchema must resolve the reference so that the dict-map → array
    pivot can carry the value-schema fields onto the synthetic items
    object. Otherwise the model sees ``items: {properties: {key}}`` and
    no idea what other fields to fill — observed live as GPT-5.5 / Grok-4
    packing ``qty`` and ``price`` into the ``key`` string itself
    (``"SKU-A|qty=10|price=9.99"``).
    """

    def test_dict_map_with_ref_value_inlines_value_fields(self) -> None:
        class _LineItem(BaseModel):
            sku: str = Field(description="SKU.")
            qty: int = Field(ge=0, description="Quantity.")
            price: Decimal = Field(description="Unit price.")

        class _Order(BaseModel):
            order_id: str = Field(description="Order ID.")
            lines: dict[str, _LineItem] = Field(description="Line map.")

        tool = _build_tool(_Order, name="add_lines", desc="Add lines")
        result = StrictSchema().sanitize([tool])
        params = result[0]["function"]["parameters"]
        lines_schema = params["properties"]["lines"]

        # The dict-map → array conversion must produce a populated items schema.
        assert lines_schema["type"] == "array"
        items = lines_schema["items"]
        assert items["type"] == "object"

        items_props = items["properties"]
        prop_names = set(items_props.keys())
        assert prop_names == {"key", "sku", "qty", "price"}, (
            f"Items schema must inline value fields from $ref-resolved schema; "
            f"got properties: {prop_names}. Without this, GPT-5 / Grok pack "
            f"value fields into the 'key' string."
        )

        # Required fields must include the inlined required fields plus key.
        required = set(items["required"])
        assert required == {"key", "sku", "qty", "price"}, (
            f"Items required must include both 'key' and the inlined value's "
            f"required fields; got {required}"
        )

        # Decimal collapse still applied through the resolved value schema.
        assert items_props["price"]["type"] == "number"
        assert "pattern" not in items_props["price"]

    def test_top_level_defs_block_is_removed_from_output(self) -> None:
        """After inlining, ``$defs`` is stale — leave it out of the wire
        schema so providers that reject unknown root keys don't trip."""

        class _Item(BaseModel):
            name: str

        class _Order(BaseModel):
            items: dict[str, _Item]

        tool = _build_tool(_Order, name="t", desc="d")
        result = StrictSchema().sanitize([tool])
        params = result[0]["function"]["parameters"]
        assert (
            "$defs" not in params
        ), f"$defs must be removed after inlining; got params keys: {list(params)}"

    def test_optional_dict_map_with_ref_resolves(self) -> None:
        """``Optional[dict[str, T]]`` (the tau-mfg ``upsert_lines`` shape) wraps the
        ref in ``anyOf`` — refs must resolve through anyOf branches too."""

        class _Line(BaseModel):
            qty: int

        class _Mod(BaseModel):
            order_id: str
            upsert_lines: dict[str, _Line] | None = None

        tool = _build_tool(_Mod, name="modify", desc="d")
        result = StrictSchema().sanitize([tool])
        params = result[0]["function"]["parameters"]
        upsert = params["properties"]["upsert_lines"]
        # The anyOf branch becomes the array shape; resolve into it.
        if "anyOf" in upsert:
            array_branch = next(b for b in upsert["anyOf"] if b.get("type") == "array")
        else:
            array_branch = upsert
        items_props = set(array_branch["items"]["properties"].keys())
        assert items_props == {"key", "qty"}, (
            f"Optional[Dict[str, T]] must also inline the $ref value schema; "
            f"got items.properties: {items_props}"
        )


class TestStrictSchemaCapabilitySet:
    """:meth:`supported_capabilities` advertises which JSON-Schema features
    pass through ``sanitize()`` unchanged.

    After the position-aware refactor only two capabilities are *removed* —
    typed dict-maps (rewritten to array-of-objects) and the Pydantic
    Decimal numeric/string union (collapsed to plain ``number``).
    ``REGEX_PATTERN`` and ``DATE_TIME_FORMAT`` are now preserved: safe
    patterns survive, ``format: date-time`` is informational and accepted
    by every current provider.
    """

    def test_includes_regex_pattern(self) -> None:
        caps = StrictSchema().supported_capabilities()
        assert SchemaCapability.REGEX_PATTERN in caps

    def test_includes_date_time_format(self) -> None:
        caps = StrictSchema().supported_capabilities()
        assert SchemaCapability.DATE_TIME_FORMAT in caps

    def test_excludes_anyof_numeric_string(self) -> None:
        caps = StrictSchema().supported_capabilities()
        assert SchemaCapability.ANYOF_NUMERIC_STRING not in caps

    def test_still_excludes_dict_map_typed(self) -> None:
        caps = StrictSchema().supported_capabilities()
        assert SchemaCapability.DICT_MAP_TYPED not in caps
