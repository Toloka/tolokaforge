#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_count parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-count")


@registry.tool("Fetch a payment record by its identifier.")
def billing_api_get_payment(
    data: dict,
    payment_id: Annotated[str, Field(description="Payment identifier, e.g. 'PAY-664306'")],
) -> dict:
    return {"payment_id": payment_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
