# MCP Integration

Tolokaforge can load custom tools via an MCP server module referenced in `task.yaml`.

## Task Configuration

```yaml
tools:
  agent:
    enabled: ["custom_tool_1", "custom_tool_2"]
    mcp_server: "../mcp_server.py"
```

The MCP server should expose a `TOOLS` mapping (function name → tool spec) and an `invoke_tool` handler.

## Notes

- MCP tools are loaded in addition to built-in tools.
- MCP servers can also expose state (`get_data`, `set_data`) for grading and initialization.
- For τ²-compatible tasks, see adapter-specific docs.

See `docs/ADAPTER_ARCHITECTURE.md` for adapter integration details.
