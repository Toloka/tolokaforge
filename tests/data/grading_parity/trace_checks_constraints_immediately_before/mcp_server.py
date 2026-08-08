#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_immediately_before parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-immediately-before")


@registry.tool("Fetch an order by its identifier.")
def order_api_get_order(
    data: dict,
    order_id: Annotated[str, Field(description="Order identifier, e.g. 'O-001'")],
) -> dict:
    return {"order_id": order_id}


@registry.tool("Search the policy library.")
def search_policy(
    data: dict,
    query: Annotated[str, Field(description="Text to search the policy library for")],
) -> dict:
    return {"query": query}


@registry.tool("Open a case for a piece of work.")
def create_case(
    data: dict,
    subject: Annotated[str, Field(description="One-line summary the case is opened under")],
) -> dict:
    return {"subject": subject}


if __name__ == "__main__":
    mcp.run(transport="stdio")
