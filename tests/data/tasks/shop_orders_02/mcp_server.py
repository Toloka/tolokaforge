"""Self-contained MCP server for shop_orders_02 canonical tests.

Mirrors tasks/tool_use/shop_orders_02/ but all models and tool implementations
are inlined so tests stay within tests/data/ with no dependency on the
production tasks/ directory.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tolokaforge.core.tools_interface import ToolError, create_server

mcp, registry, TOOLS = create_server(__file__, "shop-orders-test")

# ---------------------------------------------------------------------------
# Pydantic models (mirrors tasks/tool_use/shop_orders_02/models.py)
# ---------------------------------------------------------------------------

_cfg = ConfigDict(extra="ignore", frozen=True)


class Product(BaseModel):
    model_config = _cfg
    id: str
    name: str
    price: float = Field(ge=0)
    stock: int = Field(ge=0)
    category: str


class Customer(BaseModel):
    model_config = _cfg
    id: str
    name: str
    email: str
    balance: float = Field(ge=0)


class OrderItem(BaseModel):
    model_config = _cfg
    product_id: str
    quantity: int = Field(ge=1)
    unit_price: float = Field(ge=0)


class Order(BaseModel):
    model_config = _cfg
    id: str
    customer_id: str
    status: Literal["pending", "paid", "cancelled"]
    items: list[OrderItem]
    total: float = Field(ge=0)


def _find_product(data: dict, pid: str) -> Product | None:
    raw = next((p for p in data["products"] if p["id"] == pid), None)
    return Product(**raw) if raw else None


def _find_customer(data: dict, cid: str) -> Customer | None:
    raw = next((c for c in data["customers"] if c["id"] == cid), None)
    return Customer(**raw) if raw else None


def _find_order(data: dict, oid: str) -> Order | None:
    raw = next((o for o in data["orders"] if o["id"] == oid), None)
    return Order(**raw) if raw else None


# ---------------------------------------------------------------------------
# Tools (mirrors tasks/tool_use/shop_orders_02/tools/)
# ---------------------------------------------------------------------------


@registry.tool("List all products currently in stock (stock > 0).")
def list_products(data: dict) -> list[dict]:
    return [Product(**p).model_dump() for p in data["products"] if p["stock"] > 0]


@registry.tool("Retrieve a customer record and account balance by customer ID.")
def get_customer(
    data: dict,
    customer_id: Annotated[str, Field(description="Customer identifier, e.g. 'C-101'")],
) -> dict:
    customer = _find_customer(data, customer_id)
    if customer is None:
        raise ToolError(f"Customer '{customer_id}' not found")
    return customer.model_dump()


@registry.tool(
    "Create a new order for a customer. "
    "Validates stock, creates an order record with status 'pending', "
    "and decrements stock for each ordered product. "
    "Returns the created order including the generated order ID."
)
def place_order(
    data: dict,
    customer_id: Annotated[str, Field(description="Customer placing the order")],
    items: Annotated[list[OrderItem], Field(description="Products to order")],
) -> dict:
    if _find_customer(data, customer_id) is None:
        raise ToolError(f"Customer '{customer_id}' not found")

    total = 0.0
    resolved: list[dict] = []
    for item in items:
        pid, qty = item.product_id, item.quantity
        product = _find_product(data, pid)
        if product is None:
            raise ToolError(f"Product '{pid}' not found")
        if product.stock < qty:
            raise ToolError(
                f"Insufficient stock for '{pid}'",
                details=[f"available={product.stock}, requested={qty}"],
            )
        resolved.append({"product": product, "quantity": qty})
        total = round(total + product.price * qty, 2)

    order_id = f"O-{len(data['orders']) + 1:03d}"
    order_raw: dict[str, Any] = {
        "id": order_id,
        "customer_id": customer_id,
        "status": "pending",
        "items": [
            {
                "product_id": r["product"].id,
                "quantity": r["quantity"],
                "unit_price": r["product"].price,
            }
            for r in resolved
        ],
        "total": total,
    }
    data["orders"].append(order_raw)

    for r in resolved:
        raw_product = next(p for p in data["products"] if p["id"] == r["product"].id)
        raw_product["stock"] -= r["quantity"]

    return Order(**order_raw).model_dump()


@registry.tool(
    "Mark a pending order as paid and deduct the total from the "
    "customer's account balance. Order must have status 'pending'."
)
def confirm_payment(
    data: dict,
    order_id: Annotated[str, Field(description="Order to pay, e.g. 'O-001'")],
) -> dict:
    order = _find_order(data, order_id)
    if order is None:
        raise ToolError(f"Order '{order_id}' not found")
    if order.status != "pending":
        raise ToolError(
            f"Order '{order_id}' cannot be paid",
            details=[f"status is '{order.status}'"],
        )

    customer = _find_customer(data, order.customer_id)
    if customer is None:
        raise ToolError("Customer linked to order not found")
    if customer.balance < order.total:
        raise ToolError(
            f"Insufficient balance for customer '{customer.id}'",
            details=[f"balance={customer.balance}, order total={order.total}"],
        )

    raw_customer = next(c for c in data["customers"] if c["id"] == customer.id)
    raw_order = next(o for o in data["orders"] if o["id"] == order_id)
    raw_customer["balance"] = round(customer.balance - order.total, 2)
    raw_order["status"] = "paid"

    return Order(**raw_order).model_dump()


if __name__ == "__main__":
    mcp.run(transport="stdio")
