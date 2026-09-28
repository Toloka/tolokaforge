# tolokaforge-adapter-inspect-ai

A tolokaforge adapter for [Inspect AI](https://inspect.aisi.org.uk/) tasks.

## How it works

Inspect tasks have no portable, static format — an Inspect task is Python that runs
on Inspect's own runtime (its solvers, scorers and sandbox). So this adapter does
**not** reimplement Inspect; it *delegates* to it:

1. **Discover** — enumerate the `@task` functions in a task pack via Inspect's
   `list_tasks`, and translate each into a tolokaforge `TaskConfig`.
2. **Execute** — hand the task to Inspect (`inspect eval`), which runs its own solver
   and scorer against the model under test.
3. **Normalize** — read the resulting `.eval` log back into tolokaforge's `Grade` and
   `Trajectory` (`normalize.py`), so reporting and analysis are unchanged.

Model routing goes through an OpenAI-compatible / LiteLLM-proxy endpoint configured by
the run. Secrets (keys, base URLs) are resolved via `tolokaforge.secrets` when wiring
the eval environment — never read from the process environment by this package.

## Usage

Install the package (so the `tolokaforge-inspect` command and the `tolokaforge.adapters`
entry point are available), then run a pack of Inspect tasks:

```bash
# discover tasks
tolokaforge-inspect list path/to/inspect/tasks

# run them (offline, $0)
tolokaforge-inspect run path/to/inspect/tasks --model mockllm/model --output-dir runs/inspect

# run against a real model through a gateway; the key is resolved via tolokaforge.secrets
tolokaforge-inspect run path/to/inspect/tasks \
  --model litellm-proxy/<model> \
  --set-env LITELLM_PROXY_BASE_URL=http://localhost:4000 \
  --set-secret LITELLM_PROXY_API_KEY=LITELLM_API_KEY \
  --output-dir runs/inspect
```

`run` writes one `<task_id>.json` (grade + trajectories) per task plus a `summary.json`.
The same flow is available in Python via `tolokaforge_adapter_inspect_ai.runner.run_pack`.

## Scope

Tasks run through the `tolokaforge-inspect` CLI / `runner.run_pack`, which drives the
local subprocess bridge (`bridge.py` → `inspect eval` → `normalize`) and writes
tolokaforge `Grade` + `Trajectory` results — exercised end-to-end with the offline
`mockllm` provider at $0.

Execution through the tolokaforge engine runner (`tolokaforge run`) is a separate
backend and is not wired here: the adapter's `to_task_description` and `grade` raise
`NotImplementedError`. Run Inspect tasks with `tolokaforge-inspect` instead.

## Development

```bash
uv sync
uv run pytest external_adapters/tolokaforge-adapter-inspect-ai/tests -v
```
