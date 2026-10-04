# Harbor example tasks

Harbor (Terminal-Bench 2.0) tasks that run through the `HarborAdapter` plugin.
A Harbor task ships a `task.toml`, an `environment/` build context, and a
`tests/test.sh` that writes a reward float to `/logs/verifier/reward.txt`. The
adapter runs each task on tolokaforge's own runner and grades that reward — the
trial runs on tolokaforge's substrate, not inside a nested `harbor run`.

| Task | Difficulty | Stack | Goal |
|---|---|---|---|
| [`write-release-note`](write-release-note/) | easy | Python 3.11 slim | write an exact release note the verifier checks |

`write-release-note` is a small self-contained task: the agent is given a single
`bash` tool scoped to the task container, and the verifier scores 1.0 when
`/app/RELEASE_NOTE.txt` holds exactly the required line.

## Prerequisites

1. **Docker daemon** running locally.
2. **Adapter plugin** installed into the tolokaforge workspace:
   ```bash
   uv pip install -e external_adapters/tolokaforge-adapter-harbor
   ```
3. **LLM API key** in `.env` (at least one of `ANTHROPIC_API_KEY`,
   `OPENAI_API_KEY`) when driving a real run.

A larger Harbor pack is a manual pre-step: fetch the tasks you want and point
`harbor_tasks_dir` at the directory that holds them (the adapter does not
download datasets).

## Run one task

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/harbor/run_harbor.yaml
```

## Run a Harbor leg beside a terminal-bench leg (multi-harness)

[`run_harbor_multi.yaml`](run_harbor_multi.yaml) uses a `harnesses:` block to
dispatch one adapter per leg — a Harbor task and a terminal-bench task in the
same run, each graded by its own adapter:

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/harbor/run_harbor_multi.yaml
```

## Run under a coding-harness CLI (delegated mode)

Add `agent_harness` + `agent_model` to the adapter params to layer a vendor CLI
into the task image and let it drive the trial; grading is unchanged. See the
[adapter README](../../external_adapters/tolokaforge-adapter-harbor/README.md)
for the full param set and the provider-credential plumbing.
