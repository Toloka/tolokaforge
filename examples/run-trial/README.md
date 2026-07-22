# run-trial examples — `tolokaforge run-trial` subprocess

Two examples of driving trials through the
[`tolokaforge run-trial`](../../docs/API.md#tolokaforge-run-trial) subprocess
contract. Both read the same JSON-Lines wire (`"v":1`) and both target the
bundled `examples/native/tool_use` task pack — pick the one that matches how
far along your driver is.

Start with the [standalone runner guide](../../docs/STANDALONE_RUNNER.md) if
you want the mental model + when-to-use guidance before touching code.

## `drive_run_trial.py` — the "hello world"

One trial, one JSON message in, one JSON message out, print the grade. About
90 lines, no aggregation, no comparison. Read this first if the wire format
is new to you.

```bash
uv run python examples/run-trial/drive_run_trial.py
```

## `drive_run_trial_sweep.py` — end-user shape

Runs both bundled `tool_use` tasks against two agent models
(`anthropic/claude-sonnet-4.6` and `openai/gpt-4o` by default), each trial in
its own isolated subprocess, and prints a per-task per-model comparison table
plus per-model averages. Errors are dispatched by `error_type` rather than
caught as tracebacks — a `run-trial` driver never sees a Python traceback
across the wire.

```bash
uv run python examples/run-trial/drive_run_trial_sweep.py
```

Override the model list with `TOLOKAFORGE_SWEEP_MODELS`:

```bash
TOLOKAFORGE_SWEEP_MODELS=anthropic/claude-sonnet-4.6 \
    uv run python examples/run-trial/drive_run_trial_sweep.py
```

## Requirements

Both examples need:

- An LLM provider key in `.env` at the repo root (e.g. `OPENROUTER_API_KEY`),
  exactly as a normal `tolokaforge run` needs.
- A live runner: `make docker-up`.

Each subprocess is spawned with its working directory at the task-pack root,
so the wire task's file assets (`grading.yaml`, `initial_state.json`,
fixtures, tool code) resolve — a task crossing the wire carries no source
directory of its own.

## In-process counterpart

For the Python-in-process equivalent (no subprocess boundary, direct
`TrialResult` return), see [`examples/library/run_trial.py`](../library/run_trial.py).
