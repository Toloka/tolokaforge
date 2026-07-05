# Troubleshooting

## Services Not Running

Browser, JSON DB, and RAG tasks require services. `tolokaforge run`
auto-starts them, but a cold first run can be slow while images build.
Pre-build once:

```bash
uv run tolokaforge docker build
```

Inspect running containers + their host-mapped ports with:

```bash
uv run tolokaforge docker status
```

Health of a running service is reachable at `/health` on the
host-mapped port `docker status` reports (e.g.
`http://localhost:<mapped>/health`). The container-internal
convention is: `db-service` on 8000, `mock-web` on 8080,
`rag-service` on 8001.

## Browser Tool Errors

- Ensure Playwright is installed:
  ```bash
  uv run playwright install --with-deps chromium
  ```
- For Docker runtime, make sure the `runner` container is healthy —
  `tolokaforge docker status` shows per-container state.

## RAG Search Returns Empty

- Confirm the corpus directory exists in the task.
- Trigger indexing:
  ```bash
  curl -X POST http://localhost:8001/index \
    -H "Content-Type: application/json" \
    -d '{"corpus_path": "/app/tasks/<category>/<task>/rag/corpus"}'
  ```

## API Keys Not Found

Set a provider key in `.env` or your shell environment:

```bash
export OPENROUTER_API_KEY=sk-or-...
```

## Task Validation Fails

```bash
uv run tolokaforge validate --tasks "tasks/**/task.yaml"
```

Common causes:
- Invalid YAML
- Missing required fields
- Tool name not in built-in list
