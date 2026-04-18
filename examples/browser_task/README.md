# Browser Task Authoring Example

This example shows a practical browser-task authoring flow with a self-contained dataset.

## Included Task

`dataset/tasks/browser/browser_public_example_01/` — a browser task where the agent
navigates a support site, reads order and policy pages, and writes a refund recommendation.

## Prerequisites

- Docker installed and running (the orchestrator auto-starts required services)
- `OPENROUTER_API_KEY` in `.env`

## Validate

```bash
uv run tolokaforge validate --tasks "examples/browser_task/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/browser_task/run_config.yaml
```

## Configure Browser Tool

In your task config:

```yaml
tools:
  agent:
    enabled: ["browser", "write_file", "read_file"]
    browser:
      initial_url: "http://mock-web:8080/task/browser/my_task/index.html"
      allowed_actions:
        - navigate
        - click_at
        - type_text_at
        - select
        - scroll_document
        - key_combination
```

## Deterministic Grading

Use `state_checks` to validate concrete outputs under `/env/fs/agent-visible/...`.

For browser action schema details, see `docs/BROWSER_TOOLS.md`.
