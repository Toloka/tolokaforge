#!/usr/bin/env python3
"""Stub MCP server for the example_domain canonical fixture.

Never actually launched as a subprocess in canonical tests — they exercise
adapter loading, not tool dispatch. The file just has to exist so
NativeAdapter resolves the ``mcp_server`` ref without raising.
"""
