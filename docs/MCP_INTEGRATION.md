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
possibly mutating. `category` is unrelated and stays as it is.

### A read rule

A tool that changes state on some calls and only reads on others declares which
calls read, by their arguments, in the `trace_checks` predicate vocabulary
([TASK_DESCRIPTION_SCHEMA.md](TASK_DESCRIPTION_SCHEMA.md)):

```python
@registry.tool(
    "Send one request to the orders API.",
    mutates_state={"read_when": {"args": {"request.method": {"in_": ["GET", "HEAD"]}}}},
)
def rest_call(data: dict, request: Request) -> dict: ...
```

A call reads when every `read_when.args` predicate holds over its arguments, and
can change state otherwise. The tool's annotation stays a bool and says
`readOnlyHint: false`, since some calls do change state; the rule itself rides in
the tool's `_meta` under the engine-owned key `tolokaforge/mutates_state`. A server
written without `DomainToolRegistry` declares a rule the same way.

The decorator checks the rule's shape when it runs and raises `ValueError` naming
the tool: exactly `read_when.args`, each path a dotted string with no empty
segment, each predicate a mapping of one or more predicate operators, the binding
operators (`equals_binding`, `contains_binding`) excluded since a rule binds
nothing. The native adapter validates the rule in full when it reads the listing,
and refuses the task at load, naming the tool and the server, when the rule is
malformed (a pattern the `linear` engine cannot compile included, unless its
predicate names another `regex_engine`), sits beside `readOnlyHint: true`, or names an argument its tool's closed
schema rules out. A wrong rule hides writes, so it is refused where a malformed
`readOnlyHint` only leaves the tool undeclared with a warning. `_meta` without the
key declares nothing.

The rule is the author's declaration: a task can lay only `mutates_state: true`
over it ([TASKS.md § Declaring which calls change state](TASKS.md#declaring-which-calls-change-state)).

### In process

The same declaration is readable in-process: `registry.TOOLS[name].mutates_state`
carries the decorator's value (`None` when undeclared, the rule as a plain mapping
for a rule), so golden replay and any other consumer that imports `TOOLS` reads the
one declaration the wire carries.

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

## Declaring a failure

A tool built with `DomainToolRegistry` refuses a request by raising
`tolokaforge.core.tools_interface.ToolError`
([ADR-0067](adr/0067-tool-error-is-its-own-status.md)):

```python
@registry.tool("Look up one order.", mutates_state=False)
def get_order(data: dict, order_id: str) -> dict:
    order = data["orders"].get(order_id)
    if order is None:
        raise ToolError(f"Order '{order_id}' not found")
    return order
```

The registry answers it with `isError: true`, the payload `{"error": ..., "details":
[...]}` as text, and `_meta: {"tolokaforge/error_category": "tool_error"}`. The runner
records the call as `tool_error`: a completed call whose response the agent reads
verbatim and the environment-error budget counts, kept apart from the other failures a
server can answer with.

| The server answers | Recorded as |
|---|---|
| `isError: true`, `_meta` `tolokaforge/error_category: tool_error` | `tool_error` — the tool ran and refused the request |
| `isError: true`, `_meta` `tolokaforge/error_category: invalid_arguments`, or JSON-RPC `-32602` | `invalid_arguments` — the caller's input was refused; the budget does not count it |
| `isError: true` with no category | `environment_error` — FastMCP refusing the arguments against the signature, or an exception escaping the tool |

A server written without `DomainToolRegistry` declares the same categories the same way;
the names are in `tolokaforge.core.mcp_error_categories`, a module with no imports.
FastMCP's own `mcp.server.fastmcp.exceptions.ToolError` is a different class: raising it
answers an untyped `isError`, recorded as `environment_error`.

Registry tools register without structured output: FastMCP validates a `CallToolResult`
a tool returns against the output schema it derives from the return annotation, which a
refusal cannot satisfy. `tools/list` carries no `outputSchema` for them and answers carry
no `structuredContent`; the engine reads only the text content, which is unchanged — except
for an answer FastMCP renders as no content at all (`None`, an empty list), which reaches
the agent as `{"content": [], "isError": false}` instead of also carrying
`structuredContent`. The registry needs `mcp>=1.19`, the first whose FastMCP passes a
returned `CallToolResult` through as it is.

## Notes

- MCP tools are loaded in addition to built-in tools.
- MCP servers can also expose state (`get_data`, `set_data`) for grading and initialization.
- For τ²-compatible tasks, see adapter-specific docs.

See `docs/ADAPTER_ARCHITECTURE.md` for adapter integration details.
