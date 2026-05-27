# Tool-use tasks example

Self-contained dataset with two public tool-use tasks. The agent drives
the calculator + helper tools with structured arguments and is graded on
the tool-call sequence plus the final answer.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/tool_use/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/tool_use/run_config.yaml
```

## Tasks included

- `dataset/tasks/tool_use/tool_use_public_example_01/`
- `dataset/tasks/tool_use/tool_use_public_example_02/`
