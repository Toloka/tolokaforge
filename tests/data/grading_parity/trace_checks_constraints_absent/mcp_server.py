#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_absent parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-absent")


@registry.tool("Refund a payment in full.")
def billing_api_refund_payment(
    data: dict,
    payment_id: Annotated[str, Field(description="Payment identifier, e.g. 'PAY-664306'")],
) -> dict:
    return {"payment_id": payment_id}


@registry.tool("Delete a customer account and everything filed under it.")
def customer_api_delete_account(
    data: dict,
    customer_id: Annotated[str, Field(description="Customer identifier, e.g. 'C-101'")],
) -> dict:
    return {"customer_id": customer_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
