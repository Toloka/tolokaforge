#!/usr/bin/env python3
"""MCP server for the trace_checks_operator_nullness parity fixture.

Supplies the schema of the tool this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; the body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-operator-nullness")


@registry.tool("Apply a credit to a billing account.")
def billing_api_apply_credit(
    data: dict,
    account_id: Annotated[str, Field(description="Account identifier, e.g. 'AC-77'")],
    amount: Annotated[float, Field(description="Credit amount in the account currency")],
    coupon: Annotated[
        str | None, Field(description="Coupon code, or null for an uncouponed credit")
    ] = None,
    legacy_flag: Annotated[
        bool | None, Field(description="Deprecated: route through the legacy credit path")
    ] = None,
) -> dict:
    return {"account_id": account_id, "credited": amount}


if __name__ == "__main__":
    mcp.run(transport="stdio")
