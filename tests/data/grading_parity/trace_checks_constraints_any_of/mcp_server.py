#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_any_of parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-any-of")


@registry.tool("Search the policy library.")
def search_policy(
    data: dict,
    query: Annotated[str, Field(description="Text to search the policy library for")],
) -> dict:
    return {"query": query}


@registry.tool("Hand a case to a human agent.")
def escalate_to_human(
    data: dict,
    case_id: Annotated[str, Field(description="Case identifier, e.g. 'CS-1042'")],
    reason: Annotated[str, Field(description="Why the case is being escalated")],
) -> dict:
    return {"case_id": case_id, "reason": reason}


@registry.tool("Resolve a case, recording the outcome.")
def resolve_case(
    data: dict,
    case_id: Annotated[str, Field(description="Case identifier, e.g. 'CS-1042'")],
    resolution: Annotated[str, Field(description="How the case was resolved")],
) -> dict:
    return {"case_id": case_id, "resolution": resolution}


if __name__ == "__main__":
    mcp.run(transport="stdio")
