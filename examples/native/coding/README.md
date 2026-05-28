# Coding tasks example

Self-contained dataset with two public coding tasks. The agent reads
fixtures, writes a solution under `/env/fs/agent-visible/`, and is graded
on the file content via JSONPath state checks plus a transcript rule.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/coding/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/coding/run_config.yaml
```

## Tasks included

- `dataset/tasks/coding/coding_public_example_01/`
- `dataset/tasks/coding/coding_public_example_02/`
