#!/usr/bin/env python3
"""MCP server for the trace_checks_constraints_absent_between parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-constraints-absent-between")


@registry.tool("Open a case for a piece of work.")
def open_case(
    data: dict,
    subject: Annotated[str, Field(description="One-line summary the case is opened under")],
) -> dict:
    return {"subject": subject}


@registry.tool("Close a case, ending work on it.")
def close_case(
    data: dict,
    case_id: Annotated[str, Field(description="Case identifier, e.g. 'CS-1042'")],
) -> dict:
    return {"case_id": case_id}


@registry.tool("Send the customer a message.")
def notify_customer(
    data: dict,
    case_id: Annotated[str, Field(description="Case identifier, e.g. 'CS-1042'")],
    template: Annotated[str, Field(description="Identifier of the message template to send")],
) -> dict:
    return {"case_id": case_id, "template": template}


if __name__ == "__main__":
    mcp.run(transport="stdio")
