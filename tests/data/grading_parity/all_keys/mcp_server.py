#!/usr/bin/env python3
"""MCP server for the all_keys parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "all-keys")


@registry.tool("Write content to a file")
def write_file(
    data: dict,
    path: Annotated[str, Field(description="Path to the file to write")],
    content: Annotated[str, Field(description="Content to write to the file")],
) -> dict:
    return {"path": path, "content": content}


@registry.tool("Close a widget, ending work on it.")
def close_widget(
    data: dict,
    widget_id: Annotated[str, Field(description="Widget identifier, e.g. 'W1'")],
) -> dict:
    return {"widget_id": widget_id}


@registry.tool("Delete a widget.")
def delete_widget(
    data: dict,
    widget_id: Annotated[str, Field(description="Widget identifier, e.g. 'W1'")],
) -> dict:
    return {"widget_id": widget_id}


@registry.tool("Send the customer a message.")
def notify_customer(
    data: dict,
    widget_id: Annotated[str, Field(description="Widget identifier, e.g. 'W1'")],
    message: Annotated[str, Field(description="Text sent to the customer")],
) -> dict:
    return {"widget_id": widget_id, "message": message}


if __name__ == "__main__":
    mcp.run(transport="stdio")
