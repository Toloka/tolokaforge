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
graded state ([ADR-0061](adr/0061-mutates-state-on-the-tool-wire.md)):

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

The same declaration is readable in-process: `registry.TOOLS[name].mutates_state`
carries the decorator's value (`None` when undeclared), so golden replay and any
other consumer that imports `TOOLS` reads the one declaration the wire carries.

### A committed cache wins

The native adapter reads a committed `fixtures/tools.json` first and never
refreshes it (see [NATIVE_ADAPTER.md](NATIVE_ADAPTER.md#tool-schemas-fixturestoolsjson)).
Adding `mutates_state=` to a tool of a server whose cache is already committed
therefore changes nothing on the wire until the cache is regenerated: delete
`fixtures/tools.json` and describe the task on the run path — a run, or
`tolokaforge run --dry-run` on a config naming the task — which queries the
server's `tools/list` again and writes the cache anew; then commit the result.

The writer persists the key for every tool: `true`, `false`, or `null` for a tool
that declares nothing (the registry's own `_tolokaforge_get_state_` /
`_tolokaforge_set_state_` declare `false` / `true`). A `null` is the author's
deliberate non-declaration — the wire reads it exactly like an absent field — and
is silent. An entry *without* the key was written before the field existed; a
description of such a task logs one warning naming the file, the tools and the
regeneration path. Regenerating the cache silences it.

## Notes

- MCP tools are loaded in addition to built-in tools.
- MCP servers can also expose state (`get_data`, `set_data`) for grading and initialization.
- For τ²-compatible tasks, see adapter-specific docs.

See `docs/ADAPTER_ARCHITECTURE.md` for adapter integration details.
