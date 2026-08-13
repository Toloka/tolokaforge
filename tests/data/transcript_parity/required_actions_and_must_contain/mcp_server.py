#!/usr/bin/env python3
"""MCP server for the required_actions_and_must_contain parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "required-actions-and-must-contain")


@registry.tool("Write content to a file")
def write_file(
    data: dict,
    path: Annotated[str, Field(description="Path to the file to write")],
    content: Annotated[str, Field(description="Content to write to the file")],
) -> dict:
    return {"path": path, "content": content}


@registry.tool("Issue a refund against an order.")
def issue_refund(
    data: dict,
    order_id: Annotated[str, Field(description="Order identifier, e.g. 'O-001'")],
) -> dict:
    return {"order_id": order_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
