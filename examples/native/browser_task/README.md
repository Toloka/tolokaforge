# Browser Task Authoring Example

This example shows a practical browser-task authoring flow with a self-contained dataset.

## Included Tasks

- `dataset/tasks/browser/browser_public_example_01/` — agent navigates a
  support site, reads order and policy pages, and writes a refund
  recommendation.
- `dataset/tasks/browser/browser_public_example_02/` — agent reads an
  incident ticket and runbooks, then writes a remediation plan.

## Prerequisites

Browser tasks require Docker environment services (mock-web):

```bash
make docker-up
```

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/browser_task/dataset/**/task.yaml"
```

## Run

```bash
# Uses the pack's run_configs/dev.yaml (requires API keys in .env)
scripts/with_env.sh uv run tolokaforge run --config examples/native/browser_task/run_configs/dev.yaml
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
