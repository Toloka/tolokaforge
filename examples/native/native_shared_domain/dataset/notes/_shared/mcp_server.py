#!/usr/bin/env python3
"""Notes MCP Server — minimal shared-domain demo.

Loads its initial state from ``initial_state.json`` next to this file. For
the shared-domain layout the per-case ``initial_state.json`` is copied into
this directory at task-bundling time so each trial sees the right state.
"""

from tolokaforge.core.tools_interface import create_server

mcp, registry, TOOLS = create_server(__file__, "notes")

from tools import register_all  # noqa: E402

register_all(registry)

if __name__ == "__main__":
    mcp.run(transport="stdio")
