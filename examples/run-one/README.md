# run-one example — `tolokaforge run-one` subprocess

`drive_run_one.py` runs a single trial through the
[`tolokaforge run-one`](../../docs/API.md#tolokaforge-run-one) subprocess
contract and prints the grade. It spawns `tolokaforge run-one`, sends one
JSON-Lines `start` message built from a task in the bundled
`examples/native/tool_use` pack, and reads the single `result` / `error` line
back — the language-agnostic counterpart to the in-process
[`examples/library/run_trial.py`](../library/run_trial.py).

## Requirements

- An LLM provider key in `.env` at the repo root (e.g. `OPENROUTER_API_KEY`),
  exactly as a normal run needs.
- A live runner: `make docker-up`.

## Run

From the repo root:

```bash
uv run python examples/run-one/drive_run_one.py
```

It prints the single trial's grade. The subprocess is spawned with its working
directory at the task-pack root so the wire task's file assets (`grading.yaml`,
`initial_state.json`, tools) resolve — a task crossing the wire carries no
source directory of its own.
