# Library example — `tolokaforge.runner.run_trial`

`run_trial.py` runs a single trial in-process through the
[`tolokaforge.runner.run_trial`](../../docs/API.md#run_trial) library API and
prints the grade. It loads a `TaskConfig` from the bundled
`examples/native/tool_use` pack, so no config file is needed.

## Requirements

- An LLM provider key in `.env` at the repo root (e.g. `OPENROUTER_API_KEY`),
  exactly as a normal run needs.
- A live runner: `make docker-up`.

## Run

From the repo root:

```bash
uv run python examples/library/run_trial.py
```

It prints `result.trajectory.grade` for the single trial.
