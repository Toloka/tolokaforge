#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_severity parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-severity")


@registry.tool("Read the audit log kept for a table.")
def read_audit_log(
    data: dict,
    table: Annotated[str, Field(description="Name of the table to act on")],
) -> dict:
    return {"table": table}


@registry.tool("Verify a table's checksum against the one on record.")
def verify_checksum(
    data: dict,
    table: Annotated[str, Field(description="Name of the table to act on")],
) -> dict:
    return {"table": table}


@registry.tool("Delete one row from a table.")
def delete_row(
    data: dict,
    table: Annotated[str, Field(description="Name of the table to act on")],
    row_id: Annotated[str, Field(description="Row identifier within the table")],
) -> dict:
    return {"table": table, "row_id": row_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
