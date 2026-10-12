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

## Declaring whether a tool changes state

A tool built with `DomainToolRegistry` declares whether a call can change the
graded state ([ADR-0057](adr/0057-mutates-state-on-the-tool-wire.md)):

```python
@registry.tool("Look up one order.", mutates_state=False)
def get_order(data: dict, order_id: str) -> dict: ...

@registry.tool("Cancel one order.", mutates_state=True)
def cancel_order(data: dict, order_id: str) -> dict: ...
```

The flag travels as MCP's own tool annotation, `readOnlyHint = not mutates_state`,
so a server written without `DomainToolRegistry`, or for another engine, declares
it the same way. The native adapter's `tools/list` introspection reads the
annotation back into `ToolSchema.mutates_state` and writes it into the
`fixtures/tools.json` cache; the runner returns it to the engine in
`RegisterTrialResponse.tool_schemas`. A tool that declares nothing gets no
annotation, its schema carries no `mutates_state`, and the engine treats it as
possibly mutating — today's behaviour. Nothing consumes the flag yet;
`category` is unrelated and stays as it is.

### A committed cache wins

The native adapter reads a committed `fixtures/tools.json` first and never
refreshes it (see [NATIVE_ADAPTER.md](NATIVE_ADAPTER.md#tool-schemas-fixturestoolsjson)).
Adding `mutates_state=` to a tool of a server whose cache is already committed
therefore changes nothing on the wire until the cache is regenerated: delete
`fixtures/tools.json` and describe the task on the run path — a run, or
`tolokaforge run --dry-run` on a config naming the task — which queries the
server's `tools/list` again and writes the cache anew; then commit the result.
A cache with an entry that lacks the key is loaded as it is and logged as a
warning naming the file and the tools, so the stale declaration is not silent.

## Notes

- MCP tools are loaded in addition to built-in tools.
- MCP servers can also expose state (`get_data`, `set_data`) for grading and initialization.
- For τ²-compatible tasks, see adapter-specific docs.

See `docs/ADAPTER_ARCHITECTURE.md` for adapter integration details.
