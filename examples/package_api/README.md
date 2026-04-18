# Package API Example

Run a benchmark programmatically using `tolokaforge.Orchestrator` and `tolokaforge.RunConfig`.

## What It Demonstrates

- Creating a `RunConfig` from Python dicts
- Loading tasks from a self-contained dataset
- Running the benchmark with Docker runtime
- Inspecting results and exit codes

## Dataset

The included `minimal_dataset/` contains a single knowledge-reasoning task:
read a problem file, compute the answer, and write it to a markdown file.

## Run

```bash
scripts/with_env.sh uv run python examples/package_api/run_minimal_dataset.py
```

Requires `OPENROUTER_API_KEY` in `.env`.

## Options

| Flag | Default | Description |
|---|---|---|
| `--dataset` | `minimal_dataset/` | Path to external dataset root |
| `--provider` | `openrouter` | LiteLLM provider name |
| `--model` | `anthropic/claude-sonnet-4-6` | Model name passed to LiteLLM |
| `--output-dir` | `results/package_api` | Output directory for results |

## Validate Tasks

```bash
uv run tolokaforge validate --tasks "examples/package_api/minimal_dataset/**/task.yaml"
```

## Expected Output

The agent should read `problem.txt`, compute `6 × 7 = 42`,
and write the answer to `submissions/answer.md`.
Results are written to `results/package_api_<timestamp>/`.
