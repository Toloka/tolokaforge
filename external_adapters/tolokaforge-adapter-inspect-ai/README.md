# tolokaforge-adapter-inspect-ai

A tolokaforge adapter for [Inspect AI](https://inspect.aisi.org.uk/) tasks.

## How it works

Inspect tasks have no portable, static format — an Inspect task is Python that runs
on Inspect's own runtime (its solvers, scorers and sandbox). So this adapter does
**not** reimplement Inspect; it *delegates* to it:

1. **Discover** — enumerate the `@task` functions in a task pack via Inspect's
   `list_tasks`, and translate each into a tolokaforge `TaskConfig` / `TaskDescription`.
2. **Execute** — hand the task to Inspect (`inspect eval`), which runs its own solver
   and scorer against the model under test.
3. **Normalize** — read the resulting `.eval` log back into tolokaforge's `Grade` and
   `Trajectory` (`normalize.py`), so reporting and analysis are unchanged. Inspect's
   per-task score is the reward the runner's `test_execution` grader reads.

Model routing goes through an OpenAI-compatible / LiteLLM-proxy endpoint configured by
the run. Secrets (keys, base URLs) are resolved via `tolokaforge.secrets` when wiring
the eval environment — never read from the process environment by this package.

## Usage

Install so the `tolokaforge.adapters` entry point is discoverable, then select the
adapter in a run config:

```yaml
evaluation:
  harness_adapter:
    type: inspect_ai
    params:
      inspect_task_dir: path/to/inspect/tasks   # pack of *.py Inspect tasks
      tasks_glob: "**/*.py"                       # optional; scans the pack by default
      task_ids: ["poc_smoke"]                     # optional filter
```

`agent_model` (the model under test) is supplied by the run's model config.

## Scope

- **Now (local delegation):** discovery + translation, and a subprocess bridge
  (`bridge.py`) that runs `inspect eval` and normalizes the log — exercised end-to-end
  with the offline `mockllm` provider at $0.
- **Next:** the containerized runner path (Inspect's docker sandbox coexisting with
  tolokaforge's runtime) and driving the model under test through Inspect's own solver.
- **Later:** injecting a tolokaforge agent/scaffold into Inspect tasks via Inspect's
  agent bridge.

## Development

```bash
uv sync
uv run pytest external_adapters/tolokaforge-adapter-inspect-ai/tests -v
```
