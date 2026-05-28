"""Unit tests for :class:`JsonCoerceResponse` and the JSON-string fallback
in :class:`ArrayDictMapResponse`.

Bug 3 from the ``tau_manufacturing_v2`` post-fix diagnosis: open-weights models
(Qwen most prominently) occasionally emit container-typed tool-call arguments
as JSON-encoded *strings* instead of native arrays / objects. Without a
recovery pass, every such call hits Pydantic ``Input should be a valid
dictionary`` / ``valid list`` validation — 805 / 250 modify_order failures on
``tau_manufacturing_v2``.

Contract pinned here:

1. A string starting with ``[`` or ``{`` that ``json.loads`` cleanly is
   replaced with its parsed value.
2. A string that *looks* like JSON but fails to parse is left untouched
   (we never raise on user data).
3. Strings that don't look like JSON containers (no leading ``[``/``{``)
   are left untouched — the model meant them as strings.
4. Native (non-string) values pass through unchanged.
5. :class:`ArrayDictMapResponse` composes both behaviours: JSON-decode
   first, *then* the existing array→dict pivot for the
   :class:`StrictSchema`-rewritten dict-map shape.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.response_policy import (
    ArrayDictMapResponse,
    JsonCoerceResponse,
)

pytestmark = pytest.mark.unit


class TestJsonCoerceResponse:
    def test_stringified_dict_is_decoded(self) -> None:
        policy = JsonCoerceResponse()
        out = policy.parse_arguments({"upsert_lines": '{"SKU-A": {"qty": 1, "price": 9.99}}'})
        assert out == {
            "upsert_lines": {"SKU-A": {"qty": 1, "price": 9.99}},
        }

    def test_stringified_list_is_decoded(self) -> None:
        policy = JsonCoerceResponse()
        out = policy.parse_arguments({"remove_lines": '["SKU-A", "SKU-B"]'})
        assert out == {"remove_lines": ["SKU-A", "SKU-B"]}

    def test_native_dict_unchanged(self) -> None:
        policy = JsonCoerceResponse()
        args = {"lines": {"SKU-A": {"qty": 1}}}
        assert policy.parse_arguments(args) == args

    def test_native_list_unchanged(self) -> None:
        policy = JsonCoerceResponse()
        args = {"remove_lines": ["SKU-A"]}
        assert policy.parse_arguments(args) == args

    def test_plain_string_unchanged(self) -> None:
        """A real string parameter (e.g. ``order_id``) must not be reinterpreted."""
        policy = JsonCoerceResponse()
        args = {"order_id": "ORD-123", "note": "Customer asked for rush."}
        assert policy.parse_arguments(args) == args

    def test_invalid_json_string_unchanged(self) -> None:
        """A string that *looks* like JSON but doesn't parse is preserved verbatim
        — we surface the validation failure downstream rather than crash here."""
        policy = JsonCoerceResponse()
        args = {"payload": '{"unbalanced": '}
        assert policy.parse_arguments(args) == args

    def test_empty_string_unchanged(self) -> None:
        """Empty strings are passed through; the receiving tool decides whether
        ``""`` is acceptable for its declared type."""
        policy = JsonCoerceResponse()
        args = {"equipment": ""}
        assert policy.parse_arguments(args) == args

    def test_string_containing_only_whitespace_unchanged(self) -> None:
        policy = JsonCoerceResponse()
        args = {"x": "   "}
        assert policy.parse_arguments(args) == args

    def test_numbers_and_booleans_passed_through(self) -> None:
        policy = JsonCoerceResponse()
        args = {"qty": 42, "active": True, "ratio": 0.5}
        assert policy.parse_arguments(args) == args

    def test_quoted_json_scalar_not_promoted(self) -> None:
        """``'42'`` parses to 42 via json.loads but isn't a *container* —
        we never promote scalars (that would mangle string IDs that happen
        to be all-numeric)."""
        policy = JsonCoerceResponse()
        args = {"order_id": "42"}
        assert policy.parse_arguments(args) == args

    def test_none_values_preserved(self) -> None:
        policy = JsonCoerceResponse()
        args = {"optional": None}
        assert policy.parse_arguments(args) == args

    def test_multiple_stringified_values(self) -> None:
        policy = JsonCoerceResponse()
        out = policy.parse_arguments(
            {
                "lines": '{"SKU-A": {"qty": 1}}',
                "remove_lines": '["SKU-B"]',
                "order_id": "ORD-1",
                "qty_total": 5,
            }
        )
        assert out == {
            "lines": {"SKU-A": {"qty": 1}},
            "remove_lines": ["SKU-B"],
            "order_id": "ORD-1",
            "qty_total": 5,
        }


class TestArrayDictMapResponseAlsoCoercesJsonStrings:
    """``ArrayDictMapResponse`` (used by GPT-5 / Grok with StrictSchema) must
    *also* JSON-decode stringified containers — otherwise a stringified array
    of ``{key, …}`` objects bypasses the array→dict pivot and the tool sees a
    string instead of a dict."""

    def test_stringified_array_with_keys_is_decoded_then_pivoted(self) -> None:
        """The full pipeline: ``str → list[{key,…}] → dict``."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments(
            {
                "lines": '[{"key": "SKU-A", "qty": 1}, {"key": "SKU-B", "qty": 2}]',
            }
        )
        assert out == {"lines": {"SKU-A": {"qty": 1}, "SKU-B": {"qty": 2}}}

    def test_stringified_dict_passes_through_pivot(self) -> None:
        """A stringified dict (no ``key`` field) is decoded but the pivot
        leaves it as-is — the receiving tool expected a dict and gets a
        dict."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments(
            {
                "lines": '{"SKU-A": {"qty": 1}}',
            }
        )
        assert out == {"lines": {"SKU-A": {"qty": 1}}}

    def test_native_array_with_keys_still_pivoted(self) -> None:
        """Existing behaviour preserved."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments(
            {
                "lines": [{"key": "SKU-A", "qty": 1}],
            }
        )
        assert out == {"lines": {"SKU-A": {"qty": 1}}}


class TestArrayDictMapResponseEmptyArrayPivot:
    """An empty array for a dict-map-typed parameter must pivot to an empty
    dict — the schema declared it as ``type: array`` on the wire (per
    :class:`StrictSchema`'s dict-map → array conversion), but the
    receiving tool's Pydantic validator still expects ``Dict[str, T]``.
    Without this pivot the tool rejects the empty list with
    ``"Input should be a valid dictionary"``.

    The marker for "was originally a dict-map" comes from
    :func:`_resolve_declared_type` in ``client.py`` — it returns
    ``"dict_map"`` (not ``"array"``) for the
    ``{type: array, items.properties.key}`` shape that ``StrictSchema``
    emits.

    Production motivation: ``output/new_collected/tau_manufacturing /
    gemini_35_flash / task=9 rep=4`` showed Gemini 3.5 Flash emitting
    ``lines: []`` for ``tau_manufacturing_create_order``. The
    "Input should be a valid dictionary" error masks the underlying
    tool-level constraint (an order needs at least one line); with the
    pivot the tool surfaces the actual semantic constraint instead.

    Affects every preset using ``response_policy: array_dict_map`` —
    GPT-5 family, xAI Grok, Gemini family.
    """

    def test_empty_array_pivots_when_param_typed_dict_map(self) -> None:
        """Empty list with ``dict_map`` marker → empty dict."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments(
            {"lines": []},
            param_types={"lines": "dict_map"},
        )
        assert out == {"lines": {}}

    def test_empty_array_pivot_doesnt_disturb_siblings(self) -> None:
        """Empty-array pivot is local to the dict-map param; other args
        are unchanged."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments(
            {
                "lines": [],
                "produced_sku_id": "SKU-X",
                "produced_quantity": 0,
            },
            param_types={"lines": "dict_map", "produced_sku_id": "string"},
        )
        assert out == {
            "lines": {},
            "produced_sku_id": "SKU-X",
            "produced_quantity": 0,
        }

    def test_stringified_empty_array_pivots_after_json_decode(self) -> None:
        """``"[]"`` → decoded to ``[]`` → pivoted to ``{}``."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments(
            {"lines": "[]"},
            param_types={"lines": "dict_map"},
        )
        assert out == {"lines": {}}

    def test_non_empty_array_still_pivots(self) -> None:
        """Existing non-empty pivot path is unaffected (works without
        ``param_types`` because the items carry the ``key`` field)."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments({"lines": [{"key": "SKU-A", "qty": 1}]})
        assert out == {"lines": {"SKU-A": {"qty": 1}}}

    def test_empty_real_array_param_left_alone(self) -> None:
        """Legitimate empty list for an ``array``-typed param (NOT a
        dict-map; ``equipment: list[str]``) stays as an empty list. The
        receiving tool expects ``[]``, not ``{}``."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments(
            {"equipment": []},
            param_types={"equipment": "array"},
        )
        assert out == {"equipment": []}

    def test_empty_array_without_param_types_left_alone(self) -> None:
        """Conservative: when no schema info is available, leave an
        ambiguous empty list alone rather than guessing wrong. The
        pre-fix tool error is annoying but doesn't silently corrupt
        data; the schema-aware pivot above is the path that callers
        with schema info should take."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments({"lines": []})
        assert out == {"lines": []}
