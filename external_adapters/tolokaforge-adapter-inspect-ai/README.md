# tolokaforge-adapter-inspect-ai

A tolokaforge adapter for [Inspect AI](https://inspect.aisi.org.uk/) tasks. Runs under
`tolokaforge run` via a run config, the same way the terminal-bench adapter does.

## How it works

Inspect tasks have no portable, static format — a task is Python that runs on Inspect's
own runtime (solvers, scorers, sandbox). So this adapter **delegates** to Inspect rather
than reimplementing it:

1. **Discover / translate** — enumerate Inspect `@task` functions via `list_tasks` and
   translate each into a tolokaforge `TaskConfig`.
2. **Run in a container** — the adapter synthesises a two-stack compose plan (engine +
   task) whose agent container has `inspect_ai` + the task pack baked in. The trial's
   "agent" step is a single `inspect eval <task> --model <model>` command (Inspect runs
   its own solver + scorer autonomously).
3. **Grade** — `test_execution`: a generated `tests/test.sh` reads the mean score out of
   the `.eval` log and writes it to `/logs/verifier/reward.txt`, which the runner reads.

Model credentials reach the container through `tolokaforge.secrets`: `agent_provider_env`
values (which may be `${secret:NAME}` refs) are resolved at load time and passed as
per-trial compose inputs — never read from the environment by this package.

## Usage

```bash
# offline, $0 (mockllm); build the runner image since the runner shells out to docker
scripts/with_env.sh uv run tolokaforge run \
  --config examples/inspect_ai/run_config.yaml --image-source build
```

Run config (`examples/inspect_ai/run_config.yaml`):

```yaml
evaluation:
  projects: ["examples/inspect_ai/tasks"]
  harness_adapter:
    type: inspect_ai
    params:
      inspect_task_dir: "examples/inspect_ai/tasks"
      agent_model: "mockllm/model"        # inspect model; e.g. openai/gpt-4o for a real run
      # agent_provider_env:               # for a real model through a gateway
      #   OPENAI_API_KEY: "${secret:OPENROUTER_API_KEY}"
      #   OPENAI_BASE_URL: "https://openrouter.ai/api/v1"
```

Requires a running Docker daemon. `params` also accepts `base_image`, `inspect_version`,
`agent_timeout_s`, `network_policy`, `tasks_glob`, and `task_ids`.

## Development

```bash
uv sync
uv run pytest external_adapters/tolokaforge-adapter-inspect-ai/tests -v
```
