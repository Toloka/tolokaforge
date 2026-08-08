#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_all_of parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-all-of")


@registry.tool("Write up a case in full.")
def write_report(
    data: dict,
    case_id: Annotated[str, Field(description="Case identifier, e.g. 'CS-1042'")],
    body: Annotated[str, Field(description="Text of the report")],
) -> dict:
    return {"case_id": case_id, "body": body}


@registry.tool("Close a case, ending work on it.")
def close_case(
    data: dict,
    case_id: Annotated[str, Field(description="Case identifier, e.g. 'CS-1042'")],
) -> dict:
    return {"case_id": case_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
