#!/usr/bin/env python3
"""MCP server for the trace_checks_operator_dates parity fixture.

Supplies the schema of the tool this pack's grading and trials name. The
substrate-parity suites replay recorded results, so nothing here is ever
dispatched; the body echoes what it was handed rather than modelling a domain.
"""

from typing import Annotated

from pydantic import Field

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "trace-checks-operator-dates")


@registry.tool("Book a flight departing on the given date.")
def travel_api_book_flight(
    data: dict,
    booking_reference: Annotated[str, Field(description="Booking reference, e.g. 'BK-2201'")],
    departure_date: Annotated[
        str, Field(description="Departure date, ISO-8601, e.g. '2026-03-15' or with a time")
    ],
) -> dict:
    return {"booking_reference": booking_reference, "departure_date": departure_date}


if __name__ == "__main__":
    mcp.run(transport="stdio")
