"""Telecom-resolution tools (tau2-bench-derived, native stubs).

Lifts the multi-step telecom resolution FLOW and the complex wire SHAPES from
the public tau2-bench telecom domain: id-threading + fan-in across a long call
chain (subscriber_id / invoice_id / service_id), a nested Subscriber/Address,
the discriminated PaymentMethod union inside a dict-map, Decimal money amounts,
closed enums, validation constraints, and a no-param status tool. Stubs only
(echo); no policy, no business logic.

No ``from __future__ import annotations`` here: FastMCP must resolve the model
types as real objects to derive the schemas.
"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from models import Address, PaymentMethod

from tolokaforge.core.tools_interface import DomainToolRegistry


def register(registry: DomainToolRegistry) -> None:
    @registry.tool(
        "Look up a subscriber by phone number. Returns the subscriber id that "
        "later calls must reference."
    )
    def lookup_subscriber(
        data: dict,
        phone_number: Annotated[
            str, Field(description="Phone number in E.164 form.", examples=["+15551234567"])
        ],
    ) -> dict:
        return {"ok": True, "subscriber_id": "SUB-7F3A", "phone_number": phone_number}

    @registry.tool(
        "Read a subscriber's profile: nested billing address, registered payment "
        "methods, and the ids of their service lines."
    )
    def get_subscriber_details(
        data: dict,
        subscriber_id: Annotated[str, Field(description="Subscriber id.", examples=["SUB-7F3A"])],
    ) -> dict:
        return {"ok": True, "subscriber_id": subscriber_id, "service_ids": ["SVC-1", "SVC-2"]}

    @registry.tool(
        "List a subscriber's invoices. Returns invoice ids and statuses that "
        "later calls must reference."
    )
    def get_invoices(
        data: dict,
        subscriber_id: Annotated[str, Field(description="Subscriber id.", examples=["SUB-7F3A"])],
    ) -> dict:
        return {
            "ok": True,
            "subscriber_id": subscriber_id,
            "invoices": [{"invoice_id": "INV-204", "status": "overdue", "amount": "42.50"}],
        }

    @registry.tool(
        "Register a subscriber's payment methods. `methods` is a map of a label to "
        "a payment method, where each method is a card, a voucher, or a bank entry."
    )
    def register_payment_methods(
        data: dict,
        subscriber_id: Annotated[str, Field(description="Subscriber id.", examples=["SUB-7F3A"])],
        methods: Annotated[
            dict[str, PaymentMethod],
            Field(
                description="Map label -> payment method (card | voucher | bank).",
                min_length=1,
            ),
        ],
    ) -> dict:
        keys = list(methods.keys()) if isinstance(methods, dict) else []
        return {"ok": True, "subscriber_id": subscriber_id, "labels": keys}

    @registry.tool(
        "Send a payment request for one invoice. Requires BOTH the subscriber id "
        "and the invoice id returned by the earlier lookups."
    )
    def send_payment_request(
        data: dict,
        subscriber_id: Annotated[str, Field(description="Subscriber id.", examples=["SUB-7F3A"])],
        invoice_id: Annotated[str, Field(description="Invoice id.", examples=["INV-204"])],
        amount: Annotated[Decimal, Field(gt=0, description="Amount to request.", examples=["42.50"])],
    ) -> dict:
        return {
            "ok": True,
            "subscriber_id": subscriber_id,
            "invoice_id": invoice_id,
            "amount": str(amount),
        }

    @registry.tool(
        "Set the status of one service line. Requires BOTH the subscriber id and "
        "the service id. `status` is one of active | suspended | pending."
    )
    def set_service_status(
        data: dict,
        subscriber_id: Annotated[str, Field(description="Subscriber id.", examples=["SUB-7F3A"])],
        service_id: Annotated[str, Field(description="Service id.", examples=["SVC-1"])],
        status: Annotated[
            Literal["active", "suspended", "pending"], Field(description="New service status.")
        ],
    ) -> dict:
        return {
            "ok": True,
            "subscriber_id": subscriber_id,
            "service_id": service_id,
            "status": str(status),
        }

    @registry.tool("Update a subscriber's billing address (a nested address object).")
    def update_address(
        data: dict,
        subscriber_id: Annotated[str, Field(description="Subscriber id.", examples=["SUB-7F3A"])],
        address: Annotated[Address, Field(description="New billing address.")],
    ) -> dict:
        addr = address.model_dump() if hasattr(address, "model_dump") else address
        return {"ok": True, "subscriber_id": subscriber_id, "address": addr}

    @registry.tool(
        "Top up a service line's monthly data. `gb` must be at least 5 and a "
        "multiple of 5."
    )
    def top_up_data(
        data: dict,
        subscriber_id: Annotated[str, Field(description="Subscriber id.", examples=["SUB-7F3A"])],
        service_id: Annotated[str, Field(description="Service id.", examples=["SVC-1"])],
        gb: Annotated[int, Field(ge=5, multiple_of=5, description="Gigabytes to add.")],
    ) -> dict:
        return {"ok": True, "subscriber_id": subscriber_id, "service_id": service_id, "gb": gb}

    @registry.tool("Get the current network status. Takes no parameters.")
    def get_network_status(data: dict) -> dict:
        return {"ok": True, "network": "nominal"}
