"""Canonical tests for model_policies schema transformations.

Snapshot baselines for StrictSchema and DictMapHints applied to
real tau_manufacturing tool schemas.
"""

from __future__ import annotations

import copy

import pytest

from tolokaforge.core.llm import DictMapHints, StrictSchema

pytestmark = pytest.mark.canonical

# ---------------------------------------------------------------------------
# Shared fixture: exact copy of CREATE_ORDER_TOOL from tau_manufacturing
# ---------------------------------------------------------------------------

CREATE_ORDER_TOOL = {
    "type": "function",
    "function": {
        "name": "tau_manufacturing_create_order",
        "description": "Create a new production order.",
        "parameters": {
            "additionalProperties": False,
            "description": "Input model for create_order.",
            "properties": {
                "status": {
                    "enum": [
                        "pending",
                        "in_progress",
                        "on_hold",
                        "completed",
                        "closed",
                    ],
                    "title": "OrderStatus",
                    "type": "string",
                    "description": "Status of the order.",
                    "examples": ["pending"],
                },
                "lines": {
                    "additionalProperties": {
                        "additionalProperties": False,
                        "description": "Order line keyed by sku_id.",
                        "properties": {
                            "requested_quantity": {
                                "description": "Requested quantity.",
                                "minimum": 0,
                                "title": "Requested Quantity",
                                "type": "number",
                            },
                            "allocated_quantity": {
                                "description": "Allocated quantity.",
                                "minimum": 0,
                                "title": "Allocated Quantity",
                                "type": "number",
                            },
                        },
                        "required": ["requested_quantity", "allocated_quantity"],
                        "title": "OrderLine",
                        "type": "object",
                    },
                    "description": "Map of sku_id -> line.",
                    "examples": [
                        {
                            "SKU-3A9E4": {
                                "requested_quantity": 200,
                                "allocated_quantity": 0,
                            }
                        }
                    ],
                    "title": "Lines",
                    "type": "object",
                },
                "produced_sku_id": {
                    "description": "SKU ID to produce.",
                    "examples": ["SKU-7F2C1"],
                    "title": "Produced Sku Id",
                    "type": "string",
                },
                "produced_quantity": {
                    "description": "Quantity to produce.",
                    "examples": [200],
                    "minimum": 0,
                    "title": "Produced Quantity",
                    "type": "number",
                },
            },
            "required": ["status", "lines", "produced_sku_id", "produced_quantity"],
            "title": "CreateOrderInput",
            "type": "object",
        },
    },
}


class TestStrictSchemaCanon:
    """Canonical snapshot for StrictSchema.transform() on tau_manufacturing."""

    def test_strict_schema_transform_tau_manufacturing(self, canon_snapshot) -> None:
        """StrictSchema.transform() output for CREATE_ORDER_TOOL.

        Captures current (buggy) behaviour as a baseline.
        After the fix, update snapshots with --update-canon.
        """
        strict = StrictSchema()
        result = strict.sanitize([copy.deepcopy(CREATE_ORDER_TOOL)])

        snap = canon_snapshot("schema_policy_strict_tau")
        snap.assert_match(result[0], "strict_transform.json")


class TestDictMapHintsCanon:
    """Canonical snapshot for DictMapHints.build_hints() on tau_manufacturing."""

    def test_dict_map_hints_tau_manufacturing(self, canon_snapshot) -> None:
        """DictMapHints.build_hints() output for CREATE_ORDER_TOOL.

        Captures the hint text generated for the lines parameter.
        """
        hints = DictMapHints().build_hints([copy.deepcopy(CREATE_ORDER_TOOL)])

        snap = canon_snapshot("schema_policy_dict_map_hints_tau")
        snap.assert_match({"hints": hints}, "dict_map_hints.json")
