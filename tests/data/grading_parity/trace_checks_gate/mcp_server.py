#!/usr/bin/env python3
"""MCP server for the trace_checks_gate parity fixture.

Supplies the schemas of the tools this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; each body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-gate")


@registry.tool("Read a meter's current value.")
def read_meter(
    data: dict,
    meter_id: Annotated[str, Field(description="Meter identifier, e.g. 'M-7'")],
) -> dict:
    return {"meter_id": meter_id}


@registry.tool("Reset a meter to zero.")
def reset_meter(
    data: dict,
    meter_id: Annotated[str, Field(description="Meter identifier, e.g. 'M-7'")],
) -> dict:
    return {"meter_id": meter_id}


if __name__ == "__main__":
    mcp.run(transport="stdio")
