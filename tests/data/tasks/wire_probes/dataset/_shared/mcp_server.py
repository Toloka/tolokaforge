#!/usr/bin/env python3
"""Samples MCP server for the manufacturing-derived wire-probe sandbox.

Complex dict-map / discriminated-union tool shapes, policy-stripped, neutral
entities, stubbed.
"""

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "wire_probes")

from tools import register_all  # noqa: E402

register_all(registry)

if __name__ == "__main__":
    mcp.run(transport="stdio")
