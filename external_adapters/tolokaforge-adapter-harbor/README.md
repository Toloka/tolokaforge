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
   `harbor run -p /app/task -a terminus-2 -m <model> -e docker --jobs-dir
   <host-identity-path> --job-name tf-<trial> --agent-setup-timeout-multiplier 10
   -k 1 -y` command. The job name is unique per trial (it expands the engine's
   per-trial slug in the container), so `repeats>1` / `workers>1` of one task
   never collide on a job directory. Because `harbor run -e docker` shells
   `docker compose` to build its own sandbox, the agent container bind-mounts the
   host Docker socket (Docker-out-of-Docker).
3. **Grade** — `test_execution`: a generated `tests/test.sh` reads
   `verifier_result.rewards.reward` out of Harbor's native `result.json` at this
   trial's exact job path and writes the number to `/logs/verifier/reward.txt`,
   which the runner reads. `harbor run` exits 0 even when the agent never started,
   so when the result shows an `exception_info`, no `verifier_result`, or is
   missing/off-schema, the verifier writes an **ungradeable sentinel** instead of
   a number and exits non-zero — the runner then books the trial as a **grading
   error**, not a `0.0` score (an absent reward is not a zero reward). The pinned
   Harbor version is stamped into the verifier output so a schema drift is visible.

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
| `agent` | `terminus-2` | Harbor agent (`harbor run -a`). v1 supports the terminus family (`terminus`/`terminus-1`/`terminus-2`) and the keyless `oracle`; any other agent (vendor coding CLIs) fails loud. |
| `sandbox_backend` | `docker` | Harbor sandbox backend (`harbor run -e`). |
| `agent_provider_env` | `{}` | Env the model provider needs (e.g. `OPENROUTER_API_KEY`), forwarded into the trial container; values may be `${secret:NAME}` refs. |
| `agent_setup_timeout_multiplier` | `10` | Multiplier on Harbor's 360s agent-setup budget (`--agent-setup-timeout-multiplier`); Terminus installs tooling per trial and the default is too tight. Omitted for `oracle`. |
| `agent_kwargs` | `{}` | `key=value` pairs forwarded as `harbor run --ak key=value`. |
| `harbor_version` | `0.23.0` | `harbor` pin installed into the agent image, and stamped into each trial's metadata/verifier output so a schema drift is visible. |
| `base_image` | `python:3.12-slim-bookworm` | Agent-image base (Harbor needs Python ≥ 3.12). |
| `agent_timeout_s` | task's `[agent].timeout_sec` | Per-trial agent budget. |
| `network_policy` | `full_internet` | Trial network policy. |
| `staging_root` | temp dir | Where the build context + compose files are materialised. |

The model Terminus 2 runs comes from `models.agent.name` (lifted to
`harbor run -m <model>`); `agent='oracle'` omits `-m` entirely.

### Grading

`test_execution`. The generated `tests/test.sh` reads this trial's one Harbor job
result at `<jobs-dir>/tf-<trial>/<task[:32]>__<7char>/result.json` (an exact
per-trial path, never a "newest job" glob), takes `verifier_result.rewards.reward`
(falling back to the mean of per-step rewards for a multi-step task), and writes
the float to `/logs/verifier/reward.txt`. The runner reads that reward as the
trial's score. Harbor is never imported by the verifier — it reads Harbor's JSON
directly.

**Infra-failure is a grading error, not a `0.0`.** `harbor run` exits 0 even when
the agent never started. The verifier only writes a real number when the result
shows a genuine evaluation; when Harbor recorded an `exception_info`, wrote no
result, produced no `verifier_result`, or the result is off-schema, the verifier
writes an ungradeable sentinel and exits non-zero, and the runner books the trial
as an errored grade rather than a legitimate failing score.

### Credentials

Model credentials reach the container through `tolokaforge.secrets`:
`agent_provider_env` values (which may be `${secret:NAME}` refs) are resolved at
load time and passed as per-trial compose inputs — never read from the
environment by this package, and never interpolated into the logged `harbor run`
argv. The terminus family routes its model through LiteLLM, so an `openrouter/`
model slug keeps its prefix (LiteLLM's OpenRouter handler reads
`OPENROUTER_API_KEY`); the adapter never strips it. Vendor coding CLIs
(claude-code/codex/gemini-cli) use the mirror recipe — prefix stripped, OpenRouter
reached via per-CLI `*_BASE_URL` + token — which is not wired in v1, so those
agents are refused up front rather than mis-authed silently.

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

## Real Terminus (keyed) runs — DooD realities

The keyless `oracle` path exercises the whole chain without a model, but a real
`terminus-2` run adds Docker-out-of-Docker realities the oracle never hits:

- **The model call can originate on the host.** Harbor builds its sandbox on the
  **host** daemon (DooD), and the Terminus LLM/proxy call can leave from there
  rather than from inside the trial container. Name resolution and egress must
  work for the host, not just the trial network — a run that resolves the
  provider only from inside the trial container can still fail at the model call.
- **The host needs `docker buildx`.** Harbor builds an egress sidecar for its
  sandbox, which requires buildx on the host daemon. Install it before a keyed
  run (`docker buildx version` should succeed).
- **`staging_root` must be host-visible.** Harbor's sandbox bind-mounts the job
  directory through the host daemon, so a container-only `staging_root` is
  invisible to it and every trial silently produces no results. When the adapter
  runs inside a container it **fails loud** on a non-host-visible `staging_root`
  rather than producing empty results; point it at a host-mounted path (e.g.
  under the mounted output dir). On a bare-host run any absolute path works.

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
