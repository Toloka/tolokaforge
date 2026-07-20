# Coding tasks example

Self-contained dataset with two public coding tasks. The agent reads
fixtures, writes a solution under `/env/fs/agent-visible/`, and is graded
on the file content via JSONPath state checks plus a transcript rule.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/coding/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/coding/run_configs/dev.yaml
```

## Tasks included

- `dataset/tasks/coding/coding_public_example_01/`
- `dataset/tasks/coding/coding_public_example_02/`
