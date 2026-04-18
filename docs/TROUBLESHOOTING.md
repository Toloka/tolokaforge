# Troubleshooting

## Services Not Running

Docker services auto-start by default (`auto_start_services: true`).
If they fail to start, try manual startup:

```bash
uv run tolokaforge docker up --profile core
```

Check health:

```bash
curl http://localhost:8000/health   # json-db
curl http://localhost:8080/health   # mock-web
curl http://localhost:8001/health   # rag-service
```

## Browser Tool Errors

- Ensure Playwright is installed:
  ```bash
  uv run playwright install --with-deps chromium
  ```
- For Docker runtime, make sure the executor container is healthy.

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
uv run tolokaforge validate --tasks "examples/**/task.yaml"
```

Common causes:
- Invalid YAML
- Missing required fields
- Tool name not in built-in list
