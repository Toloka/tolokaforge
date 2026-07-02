"""Samples sandbox tools (manufacturing-derived, native stubs).

Faithful to COMPLEX wire shapes (dict-map ``lots`` and ``upsert_lines``, the
CAPA discriminated union) so a model reveals the same serialisation habits a
complex multi-step domain would surface. Stubs only (echo); no policy,
no business logic. No ``from __future__ import annotations`` (FastMCP must
resolve the model types as real objects).
"""

from typing import Annotated, Any, Literal

from pydantic import Field

from models import Capa, LotAlloc, OrderLine

from tolokaforge.core.tools_interface import DomainToolRegistry


def register(registry: DomainToolRegistry) -> None:
    @registry.tool("Read information about an entity (order, lot, sku).")
    def get_entity_info(
        data: dict,
        entity_id: Annotated[str, Field(description="Entity id.", examples=["ORD-001"])],
    ) -> dict:
        return {"ok": True, "entity_id": entity_id, "found": False}

    @registry.tool(
        "Create an allocation. `lots` is a non-empty map lot_id -> {allocated_quantity}."
    )
    def create_allocation(
        data: dict,
        order_id: Annotated[str, Field(description="Order id.", examples=["ORD-001"])],
        sku_id: Annotated[str, Field(description="SKU id.", examples=["SKU-1"])],
        requested_quantity: Annotated[int, Field(ge=0, description="Requested quantity.")],
        lots: Annotated[
            dict[str, LotAlloc],
            Field(
                description="Map lot_id -> {allocated_quantity}.",
                examples=[{"LOT-EXAMPLE": {"allocated_quantity": 10}}],
                min_length=1,
            ),
        ],
    ) -> dict:
        keys = list(lots.keys()) if isinstance(lots, dict) else []
        return {"ok": True, "order_id": order_id, "sku_id": sku_id, "lot_ids": keys}

    @registry.tool(
        "Update an order's lines. `upsert_lines` is a map sku_id -> {requested_quantity, allocated_quantity}."
    )
    def modify_order(
        data: dict,
        order_id: Annotated[str, Field(description="Order id.", examples=["ORD-001"])],
        upsert_lines: Annotated[
            dict[str, OrderLine],
            Field(
                description="Map sku_id -> line.",
                examples=[{"SKU-1": {"requested_quantity": 10, "allocated_quantity": 10}}],
            ),
        ],
    ) -> dict:
        keys = list(upsert_lines.keys()) if isinstance(upsert_lines, dict) else []
        return {"ok": True, "order_id": order_id, "line_skus": keys}

    @registry.tool(
        "Open a CAPA against a lot or an order. `capa` is a discriminated union on `target`."
    )
    def create_capa(
        data: dict,
        capa: Annotated[Capa, Field(description="The CAPA to open (lot | order).")],
    ) -> dict:
        capa_repr = capa.model_dump() if hasattr(capa, "model_dump") else capa
        return {"ok": True, "capa": capa_repr}

    @registry.tool(
        "Update a lot after material is consumed: set its remaining quantity and "
        "status (released | consumed | on_hold)."
    )
    def modify_lot(
        data: dict,
        lot_id: Annotated[str, Field(description="Lot id.", examples=["LOT-01"])],
        lot_remaining_quantity: Annotated[int, Field(ge=0, description="Remaining quantity after consumption.")],
        status: Annotated[Literal["released", "consumed", "on_hold"], Field(description="New lot status.")],
    ) -> dict:
        return {"ok": True, "lot_id": lot_id, "remaining": lot_remaining_quantity, "status": str(status)}

    @registry.tool(
        "Recompute and write a SKU's inventory counters: in_stock, reserved, and "
        "available_to_promise."
    )
    def modify_inventory(
        data: dict,
        sku_id: Annotated[str, Field(description="SKU id.", examples=["SKU-1"])],
        in_stock: Annotated[int, Field(ge=0, description="On-hand quantity.")],
        reserved: Annotated[int, Field(ge=0, description="Reserved quantity.")],
        available_to_promise: Annotated[int, Field(ge=0, description="ATP = max(in_stock - hold - reserved, 0).")],
    ) -> dict:
        return {"ok": True, "sku_id": sku_id, "in_stock": in_stock,
                "reserved": reserved, "available_to_promise": available_to_promise}
