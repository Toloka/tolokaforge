#!/usr/bin/env python3
"""MCP server for the trace_checks_alternatives parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-alternatives")


@registry.tool("Read the ledger entries held for an account.")
def read_ledger(
    data: dict,
    account_id: Annotated[str, Field(description="Account identifier, e.g. 'ACC-88'")],
) -> dict:
    return {"account_id": account_id}


@registry.tool("Recompute an account's ledger total from its entries.")
def recompute_ledger_total(
    data: dict,
    account_id: Annotated[str, Field(description="Account identifier, e.g. 'ACC-88'")],
) -> dict:
    return {"account_id": account_id}


@registry.tool("Fetch an account's most recent statement.")
def fetch_statement(
    data: dict,
    account_id: Annotated[str, Field(description="Account identifier, e.g. 'ACC-88'")],
) -> dict:
    return {"account_id": account_id}


@registry.tool("Read the closing balance off a statement.")
def read_closing_balance(
    data: dict,
    account_id: Annotated[str, Field(description="Account identifier, e.g. 'ACC-88'")],
) -> dict:
    return {"account_id": account_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
