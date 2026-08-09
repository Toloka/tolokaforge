#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_absent_before parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-absent-before")


@registry.tool("Search the policy library.")
def search_policy(
    data: dict,
    query: Annotated[str, Field(description="Text to search the policy library for")],
) -> dict:
    return {"query": query}


@registry.tool("Update a customer service case, recording how it was resolved.")
def servicenow_csm_update_case(
    data: dict,
    case_id: Annotated[str, Field(description="Case identifier, e.g. 'CS-1042'")],
    u_resolution_code: Annotated[str, Field(description="Resolution code written on the case")],
) -> dict:
    return {"case_id": case_id, "u_resolution_code": u_resolution_code}


if __name__ == "__main__":
    mcp.run(transport="stdio")
