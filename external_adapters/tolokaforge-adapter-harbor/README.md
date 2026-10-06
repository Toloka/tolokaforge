# tolokaforge-adapter-harbor

A tolokaforge adapter that runs [Terminal-Bench 2.0](https://www.tbench.ai/) task
packs by **delegating execution to the real [Harbor](https://www.tbench.ai/docs)
harness** (`harbor run` + Terminus 2). It runs under `tolokaforge run` via a run
config, the same way the terminal-bench and inspect_ai adapters do.

**Opt-in install — not part of the default.** `pip install tolokaforge` is the
engine loop only and pulls in nothing Harbor. Add this adapter with
`pip install "tolokaforge[harbor]"` (or `tolokaforge[adapters]` for all shipped
adapters).

## Delegation, not task-reuse

A TB2 task pack (`task.toml` + `environment/` + `tests/`) can run two different
ways, and this adapter is only one of them:

- **Harbor delegation (this adapter).** The task's agent step is a single
  `harbor run` command; Harbor runs its **own** Terminus 2 agent and its own
  verifier inside a sandbox Harbor builds. You are measuring the Harbor harness.
- **Task-reuse (the `terminal_bench` adapter).** The same TB2 pack runs on
  **tolokaforge's own engine loop** (or a registry coding-harness CLI) with
  tolokaforge's per-trial isolation and spend caps. You are measuring your own
  loop. Reach for `tolokaforge-adapter-terminal-bench` when that is what you want.

Pick deliberately — see [Forfeitures](#forfeitures-what-you-give-up-under-harbor).

## How it works

Harbor tasks run on Harbor's own runtime (its agent, sandbox, and verifier), so
this adapter **delegates** rather than reimplementing anything:

1. **Discover / translate** — the TB2 on-disk shape is parsed through the
   terminal-bench task parser (`task.toml` + `environment/` + `tests/`), so the
   `[harbor]` extra depends on `tolokaforge-adapter-terminal-bench` — never on the
   `harbor` pip distribution, which is installed only inside the agent image.
2. **Run in a container** — the adapter synthesises a two-stack compose plan
   (engine + task) whose agent container has `harbor` + the Docker CLI + the task
   pack baked in. The trial's "agent" step is a single
   `harbor run -p /app/task -a terminus-2 -m <model> -e docker -o /logs/harbor
   --job-name trial -k 1 -y` command. Because `harbor run -e docker` shells
   `docker compose` to build its own sandbox, the agent container bind-mounts the
   host Docker socket (Docker-out-of-Docker).
3. **Grade** — `test_execution`: a generated `tests/test.sh` reads
   `verifier_result.rewards.reward` out of Harbor's native
   `/logs/harbor/trial/*__*/result.json` and writes it to
   `/logs/verifier/reward.txt`, which the runner reads.

## Usage

```bash
# Keyless $0 smoke — Harbor's `oracle` agent runs the task's reference solution:
scripts/with_env.sh uv run tolokaforge run \
  --config examples/harbor/run_harbor.yaml --image-source build   # oracle variant: see the config header
```

Run config (`examples/harbor/run_harbor.yaml`):

```yaml
models:
  agent:
    provider: "openrouter"
    name: "openrouter/anthropic/claude-sonnet-4.6"   # the model Terminus 2 runs
    harness: "harbor"                                # selects the Harbor adapter's model path
  user:
    provider: "openrouter"
    name: "openrouter/anthropic/claude-sonnet-4.6"
evaluation:
  projects: ["examples/harbor"]
  tasks_glob: "write-release-note/task.toml"
  harness_adapter:
    type: "harbor"
    params:
      harbor_tasks_dir: "examples/harbor"
      task_ids: ["write-release-note"]
      agent: "terminus-2"
      agent_provider_env:
        OPENROUTER_API_KEY: "${secret:OPENROUTER_API_KEY}"
      sandbox_backend: "docker"
```

The model Terminus 2 runs rides the delegated-adapter model surface
(`models.agent.harness: harbor` + `models.agent.name`), which the orchestrator
lifts into `harbor run -m <model>` — the same lift the inspect_ai adapter uses.
A multi-harness example that runs a `harbor` leg beside a `native` leg over
comparable tasks (to surface the per-harness comparison report) is in
`examples/harbor/run_harbor_multi.yaml`.

### Parameters (`harness_adapter.params`)

| Param | Default | Meaning |
|---|---|---|
| `harbor_tasks_dir` | first project / base dir | Directory the TB2 packs are discovered under. |
| `task_ids` | all discovered | Allow-list of task ids to run. |
| `agent` | `terminus-2` | Harbor agent (`harbor run -a`). Use `oracle` for a keyless reference run. |
| `sandbox_backend` | `docker` | Harbor sandbox backend (`harbor run -e`). |
| `agent_provider_env` | `{}` | Env the model provider needs, forwarded into the trial container; values may be `${secret:NAME}` refs. |
| `agent_kwargs` | `{}` | `key=value` pairs forwarded as `harbor run --ak key=value`. |
| `harbor_version` | `0.23.0` | `harbor` pin installed into the agent image. |
| `base_image` | `python:3.12-slim-bookworm` | Agent-image base (Harbor needs Python ≥ 3.12). |
| `agent_timeout_s` | task's `[agent].timeout_sec` | Per-trial agent budget. |
| `network_policy` | `full_internet` | Trial network policy. |
| `staging_root` | temp dir | Where the build context + compose files are materialised. |

The model Terminus 2 runs comes from `models.agent.name` (lifted to
`harbor run -m <model>`); `agent='oracle'` omits `-m` entirely.

### Grading

`test_execution`. The generated `tests/test.sh` reads the one Harbor job result
at `/logs/harbor/trial/<task[:32]>__<7char>/result.json`, takes
`verifier_result.rewards.reward` (falling back to the mean of per-step rewards
for a multi-step task), and writes the float to `/logs/verifier/reward.txt`. The
runner reads that reward as the trial's score. Harbor is never imported by the
verifier — it reads Harbor's JSON directly.

### Credentials

Model credentials reach the container through `tolokaforge.secrets`:
`agent_provider_env` values (which may be `${secret:NAME}` refs) are resolved at
load time and passed as per-trial compose inputs — never read from the
environment by this package. Harbor also supports OpenRouter (`-m openrouter/...`
with `OPENROUTER_API_KEY`).

## Forfeitures — what you give up under Harbor

When you delegate to Harbor, **Harbor owns the sandbox**, and several tolokaforge
guarantees stop at the `harbor run` boundary. Choose task-reuse
(`terminal_bench`) vs Harbor delegation with eyes open:

- **No per-trial `TaskIsolation`.** tolokaforge's fail-loud per-trial isolation
  does **not** apply inside Harbor's run — Harbor builds and tears down its own
  task sandbox, on its own terms.
- **No spend-cap / crash-restart guarantees.** tolokaforge's spend caps and
  crash-restart handling cover the engine loop, not the agent loop Harbor runs
  inside its sandbox. A runaway Terminus 2 run is bounded only by Harbor's own
  timeouts, not tolokaforge's budget controls.
- **Harbor owns sibling-container cleanup (orphan risk).** `harbor run -e docker`
  spins up Docker-out-of-Docker **sibling** containers on the host daemon. Harbor
  cleans them up — but a Harbor **crash** can orphan those containers (and their
  images) on the host, since they are not children of the trial container
  tolokaforge tears down.

If those guarantees matter for your run, run the TB2 pack on tolokaforge's own
loop through `tolokaforge-adapter-terminal-bench` instead.

## Development

```bash
uv sync --extra harbor
uv run pytest external_adapters/tolokaforge-adapter-harbor/tests -v
```

The oracle end-to-end integration test (`tests/test_oracle_e2e.py`,
`@pytest.mark.integration` + `requires_docker`) builds the agent image, runs the
real `harbor run -a oracle`, and asserts the parsed reward lands in the grade. It
requires a running Docker daemon and is slow (nested image builds); it skips when
Docker is unavailable.
