# Tools Reference

Tolokaforge exposes built-in tools via function calling. Enable them per task in `task.yaml`.

## Built-in Tools

- `browser`: Playwright-based browser automation (coordinate actions).
- `mobile`: Mobile app interaction tool (tap/type/scroll/app switching).
- `bash`: Allowlisted shell execution.
- `read_file`: Read from `/env/fs/agent-visible`.
- `write_file`: Write to `/env/fs/agent-visible`.
- `list_dir`: List files in `/env/fs/agent-visible`.
- `db_query`: JSONPath query against JSON DB service.
- `db_update`: JSONPath updates against JSON DB service.
- `sql_query`: SQL query against JSON DB service.
- `get_db_schema`: SQL schema inspection for JSON DB tables.
- `search_kb`: RAG search over indexed corpus.
- `http_request`: Restricted HTTP client for mock web services.
- `calculator`: Safe arithmetic calculator.

## Browser and Mobile Action Reference

`browser` supports these action types:

- `open_web_browser`, `navigate`, `wait_5_seconds`, `go_back`, `go_forward`, `search`
- `click_at`, `select`, `hover_at`, `type_text_at`, `key_combination`
- `scroll_document`, `scroll_at`, `drag_and_drop`

`mobile` supports these action types:

- `open_app`, `click_at`, `type_text_at`, `scroll_document`, `scroll_at`
- `key_combination`, `wait_5_seconds`, `go_back`, `drag_and_drop`, `select`, `press_enter`

See [BROWSER_TOOLS.md](BROWSER_TOOLS.md) for action schemas, examples, and coordinate behavior.

## Enabling Tools

```yaml
tools:
  agent:
    enabled: ["browser", "db_query", "db_update", "search_kb"]
  user:
    enabled: []
```

## MCP Tools

Custom tools can be provided via an MCP server:

```yaml
tools:
  agent:
    enabled: ["custom_tool"]
    mcp_server: "../mcp_server.py"
```

See `docs/MCP_INTEGRATION.md` for details.
