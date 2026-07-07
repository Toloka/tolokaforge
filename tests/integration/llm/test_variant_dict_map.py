"""Structural VARIANTS of the dict-map capability probe (observe stage).

``test_dict_map_tool_call`` is a LEGACY test we do not own, so instead of
editing it we add this parallel variant suite: the SAME
``DICT_MAP_TOOL_CALL`` capability stressed at different structural shapes
(nested inside an object, a wider map, scalar-valued) so intermittent
shape-dependent mis-shapes surface that the single canonical probe
misses. Empirical motivation - the MiniMax-M3 ``{"item": [...]}`` artifact
on dict-map fields was worse when the map was NESTED, so
``nested_in_object`` is the highest-value variant.

Self-contained (its own schema + prompt + structural check per variant),
living in the canonical package with a ``test_variant_`` filename prefix.
Gated by the cert AND ``TF_RUN_VARIANTS`` so a normal CI run skips it; the
auto-integration observe workflow runs it (sets the flag + injects the
all-required candidate cert) into a separate junit dir. The ``ShapeVariant``
helper is kept local here; if a second variant file needs it, lift it into a
shared module then, not before.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


class _LineItem(BaseModel):
    sku: str = Field(description="Stock keeping unit.")
    qty: int = Field(description="Quantity ordered.", ge=0)
    price: Decimal = Field(description="Unit price in USD.")


class _AddOrderLines(BaseModel):
    order_id: str = Field(description="Order identifier.")
    lines: dict[str, _LineItem] = Field(description="Map of line-id -> line.")


class _Order(BaseModel):
    order_id: str = Field(description="Order identifier.")
    lines: dict[str, _LineItem] = Field(description="Map of line-id -> line.")


class _SubmitOrder(BaseModel):
    order: _Order = Field(description="An order whose `lines` is a dict-map of line items.")


class _SetQuantities(BaseModel):
    order_id: str = Field(description="Order identifier.")
    quantities: dict[str, int] = Field(description="Map of sku -> quantity (scalar values).")


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": TypeAdapter(model).json_schema(),
        },
    }


_ADD_LINES_TOOL = _tool("add_order_lines", "Add line items to an order.", _AddOrderLines)
_SUBMIT_ORDER_TOOL = _tool("submit_order", "Submit a full order with its line items.", _SubmitOrder)
_SET_QTY_TOOL = _tool("set_quantities", "Set per-SKU quantities on an order.", _SetQuantities)

_SYS_BUILDER = (
    "You are an order-management assistant. Prefer calling tools over replying in "
    "prose. Issue a single tool call with every entry included in the map parameter."
)


def _assert_structured_map(lines: Any, min_entries: int = 1) -> None:
    assert isinstance(
        lines, dict
    ), f"map arguments must be a native dict, got {type(lines).__name__}: {lines!r}"
    assert (
        len(lines) >= min_entries
    ), f"expected >= {min_entries} entries, got {len(lines)}: {lines!r}"
    structured = [v for v in lines.values() if isinstance(v, dict) and "qty" in v and "price" in v]
    assert structured, f"no dict-shaped entries with qty/price - map likely stringified: {lines!r}"


def _check_nested_in_object(args: dict[str, Any]) -> None:
    order = args.get("order")
    assert isinstance(
        order, dict
    ), f"`order` must be a native dict, got {type(order).__name__}: {order!r}"
    _assert_structured_map(order.get("lines"))


def _check_wide_map(args: dict[str, Any]) -> None:
    _assert_structured_map(args.get("lines"), min_entries=5)


def _check_scalar_values(args: dict[str, Any]) -> None:
    quantities = args.get("quantities")
    assert isinstance(
        quantities, dict
    ), f"`quantities` must be a native dict, got {type(quantities).__name__}: {quantities!r}"
    assert quantities, f"`quantities` map must be non-empty: {quantities!r}"
    assert all(
        isinstance(v, int) and not isinstance(v, bool) for v in quantities.values()
    ), f"every value must be a native integer (map not stringified): {quantities!r}"


@dataclass(frozen=True)
class ShapeVariant:
    """One structural variation of the dict-map capability probe."""

    variant_id: str
    tool: dict[str, Any]
    user: str
    check: Callable[[dict[str, Any]], None]


_VARIANTS: list[ShapeVariant] = [
    ShapeVariant(
        variant_id="nested_in_object",
        tool=_SUBMIT_ORDER_TOOL,
        user=(
            "Submit order PO-42 with two lines: SKU-A at qty 10 price $9.99, and "
            "SKU-B at qty 5 price $19.50. Put the lines in the order's `lines` map."
        ),
        check=_check_nested_in_object,
    ),
    ShapeVariant(
        variant_id="wide_map",
        tool=_ADD_LINES_TOOL,
        user=(
            "Order PO-99: add five order lines - SKU-A qty 1 price $1.00, SKU-B qty 2 "
            "price $2.00, SKU-C qty 3 price $3.00, SKU-D qty 4 price $4.00, and SKU-E "
            "qty 5 price $5.00."
        ),
        check=_check_wide_map,
    ),
    ShapeVariant(
        variant_id="scalar_values",
        tool=_SET_QTY_TOOL,
        user=(
            "On order PO-7, set quantities: SKU-A to 10, SKU-B to 5, SKU-C to 2. "
            "Use the `quantities` map."
        ),
        check=_check_scalar_values,
    ),
]


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
@pytest.mark.parametrize("variant", _VARIANTS, ids=lambda v: f"dict_map__{v.variant_id}")
def test_variant_dict_map(
    cert: ModelCertificate,
    variant: ShapeVariant,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Stress dict-map with one structural variation.

    Same contract as the canonical probe: a non-empty tool call whose arguments are
    a native dict, then a variant-specific check that the map round-tripped as a
    native dict rather than a stringified blob or array.
    """
    if not os.getenv("TF_RUN_VARIANTS"):
        pytest.skip("shape-variant suite runs only in the observe stage (set TF_RUN_VARIANTS)")
    skip_unless_capability_declared(cert, Capability.DICT_MAP_TOOL_CALL)

    client = live_client(cert)
    result = client.generate(
        system=_SYS_BUILDER,
        messages=[Message(role=MessageRole.USER, content=variant.user)],
        tools=[variant.tool],
        tool_choice="auto",
    )

    assert (
        result.tool_calls
    ), f"{cert.model_id}/{variant.variant_id}: expected at least one tool call ({result!r})"
    args = result.tool_calls[0].arguments
    if isinstance(args, str):
        args = json.loads(args)
    assert isinstance(args, dict), (
        f"{cert.model_id}/{variant.variant_id}: arguments must parse as dict, "
        f"got {type(args).__name__}: {args!r}"
    )
    variant.check(args)
