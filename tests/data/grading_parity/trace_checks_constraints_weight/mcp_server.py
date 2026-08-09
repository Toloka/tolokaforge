#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_weight parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-weight")


@registry.tool("Verify that the caller is who they say they are.")
def verify_identity(
    data: dict,
    customer_id: Annotated[str, Field(description="Customer identifier, e.g. 'C-101'")],
) -> dict:
    return {"customer_id": customer_id}


@registry.tool("File a note about a contact on the customer record.")
def log_contact_note(
    data: dict,
    customer_id: Annotated[str, Field(description="Customer identifier, e.g. 'C-101'")],
    note: Annotated[str, Field(description="Text of the note kept on the contact record")],
) -> dict:
    return {"customer_id": customer_id, "note": note}


if __name__ == "__main__":
    mcp.run(transport="stdio")
