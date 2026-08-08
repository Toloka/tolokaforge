#!/usr/bin/env python3
"""MCP server for the disallowed_tools_one parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "disallowed-tools-one")


@registry.tool("Write content to a file")
def write_file(
    data: dict,
    path: Annotated[str, Field(description="Path to the file to write")],
    content: Annotated[str, Field(description="Content to write to the file")],
) -> dict:
    return {"path": path, "content": content}


@registry.tool("Delete a customer record.")
def delete_customer(
    data: dict,
    customer_id: Annotated[str, Field(description="Customer identifier, e.g. 'C-101'")],
) -> dict:
    return {"customer_id": customer_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
