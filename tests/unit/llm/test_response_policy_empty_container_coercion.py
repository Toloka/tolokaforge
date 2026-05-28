"""Schema-aware empty-container coercion contract.

Bug 4 from the post-PR-#88 production diagnosis: open-weights models (Qwen
most prominently) occasionally emit ``''`` (empty string) for tool-call
parameters declared as ``type: array`` or ``type: object``. The receiving
tool then rejects the call with ``Input should be a valid list`` /
``valid dictionary`` — 20 ``equipment: ''`` errors / 455 trials on
qwen36_plus tau_manufacturing.

The fix is **schema-aware**: the response policy looks up the declared
parameter type from the post-sanitised tool list and coerces ``''`` →
``[]`` for ``array`` params, ``''`` → ``{}`` for ``object`` params.

The post-PR-#88 plan recommendation explicitly required this be schema-aware
(blind ``''`` → ``[]`` is unsafe — empty strings are valid for string
params); see [`plans/eval_tau_manufacturing_v2_post_pr88_analysis.md`](
../../../plans/eval_tau_manufacturing_v2_post_pr88_analysis.md) §
Recommendation 2.

Contract pinned here:

1. ``''`` for an ``array``-typed param → ``[]``.
2. ``''`` for an ``object``-typed param → ``{}``.
3. ``''`` for any other declared type (including unknown / unspecified) →
   left untouched (a string param's empty string is meaningful).
4. Coercion happens **before** :class:`JsonCoerceResponse`'s JSON decoding
   (so that the empty string never gets fed to ``json.loads``).
5. The same coercion applies in :class:`ArrayDictMapResponse` since it
   composes the JSON-coerce step.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.response_policy import (
    ArrayDictMapResponse,
    JsonCoerceResponse,
    StandardResponse,
)

pytestmark = pytest.mark.unit


class TestEmptyContainerCoercion:
    """Schema-aware coercion fires only when the param's declared type
    is ``array`` or ``object``."""

    def test_empty_string_for_array_param_becomes_empty_list(self) -> None:
        policy = JsonCoerceResponse()
        out = policy.parse_arguments(
            {"equipment": ""},
            param_types={"equipment": "array"},
        )
        assert out == {"equipment": []}

    def test_empty_string_for_object_param_becomes_empty_dict(self) -> None:
        policy = JsonCoerceResponse()
        out = policy.parse_arguments(
            {"updates": ""},
            param_types={"updates": "object"},
        )
        assert out == {"updates": {}}

    def test_empty_string_for_string_param_unchanged(self) -> None:
        """Empty strings are valid string values; never coerce."""
        policy = JsonCoerceResponse()
        out = policy.parse_arguments(
            {"note": ""},
            param_types={"note": "string"},
        )
        assert out == {"note": ""}

    def test_empty_string_with_no_param_types_unchanged(self) -> None:
        """Without schema info we cannot safely coerce; pass through."""
        policy = JsonCoerceResponse()
        out = policy.parse_arguments({"equipment": ""})
        assert out == {"equipment": ""}

    def test_non_empty_string_for_array_param_still_decoded(self) -> None:
        """Pre-existing JSON-decode behaviour wins when the value is
        actually a JSON-encoded array."""
        policy = JsonCoerceResponse()
        out = policy.parse_arguments(
            {"items": '["A", "B"]'},
            param_types={"items": "array"},
        )
        assert out == {"items": ["A", "B"]}

    def test_unknown_param_in_param_types_unchanged(self) -> None:
        """Extra keys in ``param_types`` are ignored — defensive
        round-tripping when the model emits an unknown arg."""
        policy = JsonCoerceResponse()
        out = policy.parse_arguments(
            {"equipment": ""},
            param_types={"other": "array"},
        )
        assert out == {"equipment": ""}

    def test_array_dict_map_response_also_coerces_empty(self) -> None:
        """The same coercion path applies on the GPT-5/Grok strict pipeline."""
        policy = ArrayDictMapResponse()
        out = policy.parse_arguments(
            {"equipment": "", "lines": [{"key": "SKU-A", "qty": 1}]},
            param_types={"equipment": "array", "lines": "array"},
        )
        # equipment was empty string → []
        # lines was a list of {key, …} → pivoted to dict
        assert out == {"equipment": [], "lines": {"SKU-A": {"qty": 1}}}

    def test_standard_response_unchanged_when_param_types_passed(self) -> None:
        """``StandardResponse`` is the no-op policy — schema info is
        accepted (Protocol compliance) but coerces nothing."""
        policy = StandardResponse()
        out = policy.parse_arguments(
            {"equipment": ""},
            param_types={"equipment": "array"},
        )
        assert out == {"equipment": ""}


class TestEmptyContainerCoercionMixedShapes:
    """Realistic multi-arg call with mixed shape mismatches."""

    def test_qwen_create_order_call_recovered(self) -> None:
        """Reconstructs the qwen ``tau_manufacturing_create_order`` failure
        observed in production: ``equipment: ''`` for a list param, alongside
        well-formed dict / scalar args."""
        policy = JsonCoerceResponse()
        out = policy.parse_arguments(
            {
                "equipment": "",
                "lines": {
                    "SKU-A": {"requested_quantity": 200, "allocated_quantity": 0},
                },
                "produced_sku_id": "SKU-X",
                "produced_quantity": 100,
                "status": "pending",
                "supplier_id": "SUP-1",
            },
            param_types={
                "equipment": "array",
                "lines": "object",
                "produced_sku_id": "string",
                "produced_quantity": "number",
                "status": "string",
                "supplier_id": "string",
            },
        )
        assert out == {
            "equipment": [],
            "lines": {"SKU-A": {"requested_quantity": 200, "allocated_quantity": 0}},
            "produced_sku_id": "SKU-X",
            "produced_quantity": 100,
            "status": "pending",
            "supplier_id": "SUP-1",
        }
