#!/usr/bin/env python3
"""MCP server for the trace_checks_operator_negation parity fixture.

Supplies the schema of the tool this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; the body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-operator-negation")


@registry.tool("Update a support ticket with a note.")
def crm_update_ticket(
    data: dict,
    ticket_id: Annotated[str, Field(description="Ticket identifier, e.g. 'TK-88'")],
    note: Annotated[str, Field(description="The note appended to the ticket")],
) -> dict:
    return {"ticket_id": ticket_id, "note": note}


if __name__ == "__main__":
    mcp.run(transport="stdio")
