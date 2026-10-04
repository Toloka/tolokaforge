# tolokaforge-adapter-harbor

Runs Harbor (Terminal-Bench 2.0) tasks on the tolokaforge runner.

**Opt-in install — not part of the default.** `pip install tolokaforge` is the
engine loop only and pulls in nothing Harbor. Add this adapter with
`pip install "tolokaforge[harbor]"` (or `tolokaforge[adapters]` for all shipped
adapters).

Harbor tasks *are* Terminal-Bench 2.0 tasks: a `task.toml` beside an
`environment/` build context, with a `tests/test.sh` that writes a reward to
`/logs/verifier/reward.txt`. This adapter loads a local pack of such tasks,
materialises each task's environment on tolokaforge's own substrate, and grades
the pack's own reward through `test_execution`. The trial runs on the
tolokaforge runner — there is no nested `harbor run` inside the container.

## How it works

The on-disk Harbor task shape is identical to the one the terminal-bench
adapter already parses and synthesises, so this adapter depends on
`tolokaforge-adapter-terminal-bench` and reuses it directly:

- **discovery** delegates to that package's `discover_tasks` — a task declares
  itself with `task.toml` (or the legacy `task.yaml`); compose is optional and
  names the multi-container shape, otherwise the compose doc is synthesised from
  `environment/Dockerfile`;
- **environment synthesis** reuses `compose_synthesis.materialise_task_environment`,
  which already resolves the Harbor compose dialect (`${CONTEXT_DIR}`,
  `${MAIN_IMAGE_NAME}`, `${ENV_*_LOGS_PATH}`, `${HOST_*_LOGS_PATH}`) into a
  self-contained compose file the engine brings up unchanged;
- **grading** reads the reward the task's verifier wrote to
  `/logs/verifier/reward.txt` (`test_execution`), on both execution branches.

`HarborAdapter` itself is the focused wiring: Harbor discovery, the
`TaskConfig` / `TaskDescription` projection, the single `bash` agent tool, and
provider-env resolution.

## Execution modes

`agent_harness` selects which agent drives the trial:

- `engine-loop` (default) — tolokaforge's own turn loop drives the trial through
  the engine's LLM layer; the task image is left untouched.
- any installed coding-harness CLI (`claude-code`, `codex`, `gemini-cli`, …) —
  the vendor CLI is layered into the task image and runs the whole trial in one
  `docker exec` (delegated mode). This requires `agent_model`, since the CLI
  would otherwise pick its own default and the run config's model would not be
  the one that ran.

Both branches read the same `/logs/verifier/reward.txt`, so grading is
`test_execution` either way. The harness registry, image layering, and provider
credential plumbing are the shared machinery documented in the
[terminal-bench adapter README](../tolokaforge-adapter-terminal-bench/README.md);
this adapter forwards the same `agent_harness` / `agent_model` /
`agent_provider_env` / `harness_presets_file` params to it.

## Configuration

Adapter params go under `evaluation.harness_adapter.params`:

```yaml
evaluation:
  harness_adapter:
    type: "harbor"
    params:
      harbor_tasks_dir: "examples/harbor"   # directory of Harbor task folders
      task_ids: ["write-release-note"]       # optional allow-list
```

| Param | Meaning |
| --- | --- |
| `harbor_tasks_dir` | Directory holding Harbor task folders (each with `task.toml`). |
| `task_ids` | Optional allow-list of task-directory names to run. |
| `agent_harness` | `engine-loop` (default) or an installed coding-harness CLI. |
| `agent_model` | Model the delegated CLI runs; required unless `engine-loop`. |
| `agent_provider_env` | Provider-env overrides, unioned over the harness's shipped envelope. |
| `image_registry` / `image_tag` | Pull a pre-built task image instead of building locally. |
| `staging_root` | Where materialised environments are written. |
| `prebuild_images` | Declare the per-task image build to the orchestrator (default `true`). |

Provider credentials resolve through `expand_secret_refs` (a run config names a
credential as `${secret:NAME}`), reach the container only via the per-trial
`.env`, and are refused if a resolved value carries a newline or a `$` — the
same rules the terminal-bench adapter enforces.

## Dataset download is a manual pre-step

v1 discovers an already-present local pack only; it does not download a Harbor
dataset. Fetch the tasks you want to run yourself and point `harbor_tasks_dir`
at the directory that holds them.

## Examples

A vendored task and ready-to-edit run configs live under
[`examples/harbor/`](../../examples/harbor/), including a single-adapter run and
a multi-harness run that dispatches a Harbor leg alongside a terminal-bench leg.
