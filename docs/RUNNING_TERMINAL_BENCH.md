# Running terminal-bench evaluations

End-to-end guide for running terminal-bench tasks through tolokaforge, in two modes:

- **Engine-loop mode** — tolokaforge's own runner drives the LLM turn loop. The
  model is called through `litellm.completion()`; every message assembly and
  response policy the [`ModelCapabilities`](LLM_LAYER.md) registry declares
  applies.
- **Harness mode** — a vendor coding CLI (`claude-code`, `codex`, `gemini-cli`,
  `grok-build`, `kimi-code`, `opencode`) is installed inside the trial container
  and drives the task itself. Tolokaforge orchestrates the trial (compose bring-up,
  bash exec, trajectory capture, grading) but does not touch the LLM turn loop.

Both modes produce the same per-trial bundle layout at
`<output-dir>/trials/<task_id>/<trial_index>/`, so downstream tooling reads one
shape regardless of which produced it.

For a head-to-head **tolokaforge-vs-Harbor** matrix comparison, see the
`tbench-compare` guide in the internal
[`tolokaforge-tools`](https://github.com/toloka-partners/tolokaforge-tools) repo.

## Prerequisites

- A local Docker daemon. Every trial provisions at least one container
  (`docker compose up`), so Docker Desktop or an equivalent must be running.
- `uv` installed on the host (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
  All tolokaforge commands assume `uv run ...` — never a raw `python` /
  `pip install` (see [AGENTS.md](../AGENTS.md) Setup and Commands).
- Provider credentials in your `.env` at the repo root. Every non-oracle run
  needs at least one of `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` / `GEMINI_API_KEY` / `LITELLM_API_KEY`. What each harness
  actually reads is documented in the terminal-bench adapter's
  [`README.md` § Routing options](../external_adapters/tolokaforge-adapter-terminal-bench/README.md#routing-options--openrouter-litellm-or-a-mix).
- The task pack you want to run. The examples in this guide use the shipped
  `examples/terminal_bench/fix-billing-holds` task; substitute any other
  terminal-bench task directory.

Bootstrap:

```bash
uv sync
make docker-up  # or: docker compose up -d if you don't want the make target
```

## Engine-loop mode — tolokaforge drives the LLM loop

Use when you want to measure a **model** (not a coding CLI). The engine's own
turn loop calls `litellm.completion()` for every step; the trial container runs
only the task's own services.

**1. Write a run config.** Save this as `run_engine.yaml`:

```yaml
models:
  agent:
    provider: "openrouter"
    name: "openrouter/anthropic/claude-sonnet-5"
    temperature: 0.0
    max_tokens: 4096
compute:
  workers: 1
storage:
  queue:
    backend: sqlite
orchestrator:
  repeats: 3
  strict_task_load: true
  timeouts:
    episode_s: 1800
evaluation:
  projects:
    - "examples/terminal_bench"
  tasks_glob: "fix-billing-holds/task.yaml"
  output_dir: "results/engine-sonnet-5"
  harness_adapter:
    type: "terminal_bench"
    params:
      terminal_bench_dir: "examples/terminal_bench"
      task_ids: ["fix-billing-holds"]
      # engine-loop is the default when agent_harness is omitted; naming it
      # explicit here makes the run's intent visible in the artifact.
      agent_harness: "engine-loop"
```

**2. Run it.** The `scripts/with_env.sh` wrapper sources `.env` so
`SecretManager` can resolve `${secret:...}` references:

```bash
scripts/with_env.sh uv run tolokaforge run --config run_engine.yaml
```

**3. Read the results.** `results/engine-sonnet-5/aggregate.json` carries the
per-task means. Per-trial artifacts live under
`results/engine-sonnet-5/trials/fix-billing-holds/<0..2>/`:

- `trajectory.yaml` — every user / assistant / tool turn
- `metrics.yaml` — `api_calls`, token counts, `cost_usd`
- `grade.yaml` — `binary_pass`, component scores, verifier stdout
- `env.yaml` — the recorded `HarnessSpec` (`engine-loop` here, so no CLI layer)

**When to reach for engine-loop mode**:

- You are measuring a model's raw capability, not a CLI's scaffolding.
- You want the engine's [`ModelCapabilities` policies](LLM_LAYER.md) to apply
  (Gemini schema sanitizers, Claude system prompt shape, etc.).
- You want honest token / cost accounting through `litellm`.

## Harness mode — a vendor CLI drives the trial

Use when you want to measure a **coding-harness CLI** (Claude Code, Codex,
Gemini CLI, etc.) instead of a bare model. The CLI installs into the trial
image via `install-harness.sh`, and the trial exec invokes it directly.

Six harnesses ship in-tree. Each is a data entry in
`external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/data/harnesses.yaml`
— adding one is a YAML edit, not a code change.

### The recipe — same shape, changing `agent_harness` and `agent_model`

```yaml
# run_harness.yaml
models:
  agent:
    provider: "openrouter"
    name: "openrouter/anthropic/claude-sonnet-5"
    temperature: 0.0
    max_tokens: 4096
compute:
  workers: 1
storage:
  queue:
    backend: sqlite
orchestrator:
  repeats: 3
  strict_task_load: true
  timeouts:
    episode_s: 1800
evaluation:
  projects:
    - "examples/terminal_bench"
  tasks_glob: "fix-billing-holds/task.yaml"
  output_dir: "results/harness-claude-code"
  harness_adapter:
    type: "terminal_bench"
    params:
      terminal_bench_dir: "examples/terminal_bench"
      task_ids: ["fix-billing-holds"]
      # THE knob that switches modes. Any accepted harness name here layers
      # the CLI into the trial image; `engine-loop` keeps the engine's turn loop.
      agent_harness: "claude-code"
      # REQUIRED in harness mode — the adapter can't see the run's model config.
      # The CLI is measured on this model.
      agent_model: "openrouter/anthropic/claude-sonnet-5"
```

Run it the same way:

```bash
scripts/with_env.sh uv run tolokaforge run --config run_harness.yaml
```

Per-trial artifacts land at `results/harness-claude-code/trials/fix-billing-holds/<N>/`,
same shape as engine-loop.

### Per-harness quick reference

| Harness | `agent_harness` | Example `agent_model` | Notes |
|---|---|---|---|
| Claude Code | `claude-code` | `openrouter/anthropic/claude-sonnet-5` | Anthropic-compat via OpenRouter. |
| Codex | `codex` | `openrouter/openai/gpt-5.6-sol` | OpenAI-compat via OpenRouter. Writes a `~/.codex/config.toml` per trial. |
| Grok Build | `grok-build` | `openrouter/x-ai/grok-4.5` | Auto-configures `~/.grok/config.toml` for OpenRouter. |
| Kimi Code | `kimi-code` | `openrouter/moonshotai/kimi-k3` | Also runs `kimi-k2.7-code` — but see the middleware caveat below. |
| OpenCode | `opencode` | `openrouter/meta/muse-glimmer-30b` | See the routing / auth notes below. |
| Gemini CLI | `gemini-cli` | `openrouter/google/gemini-3.6-flash` | Shipped default is direct Google; LiteLLM overlay is the practical path (see below). |

### Recipe — Kimi K2.7 Code (`request_middleware`)

`moonshotai/kimi-k2.7-code` on OpenRouter fans out across 14 providers, mostly
INT4/FP4 third-parties whose tool-call continuation returns empty completions
(`APIEmptyResponseError`). The shipped `kimi-code` entry declares a
`request_middleware` that boots a stdlib HTTP proxy inside the trial container
and injects `{"provider": {"only": ["moonshotai"], "allow_fallbacks": false}}`
into every `/chat/completions` body — forcing Moonshot AI first-party routing.

Nothing to configure — it applies automatically when `agent_harness: kimi-code`.
The proxy binds `127.0.0.1:8899` inside the container and `KIMI_MODEL_BASE_URL`
is rewritten to `http://127.0.0.1:8899` before the CLI starts.

### Recipe — Muse Glimmer / any non-Anthropic OpenRouter model on opencode

Opencode's `strip_openrouter_prefix: false` (declared in `harnesses.yaml`)
preserves the `openrouter/` prefix on the model name so opencode's config
routes to its `openrouter` provider block (an OpenAI-compat surface pointing at
`openrouter.ai/api/v1`). Nothing else to do — pass a model like
`openrouter/meta/muse-glimmer-30b` or `openrouter/qwen/qwen3.7-max` verbatim.

If you swap the shipped opencode config template for a different one, keep the
`apiKey` field as bash `$ANTHROPIC_API_KEY` (not `${env:ANTHROPIC_API_KEY}`).
Opencode 1.18.x reads `${env:...}` substitutions on its native `anthropic`
provider block but not on `@ai-sdk/openai-compatible` blocks — the literal
string ends up in the config file and OpenRouter answers 401 "Missing
Authentication header".

### Recipe — Gemini CLI (LiteLLM gateway)

The shipped default routes gemini-cli at Google directly and needs a real
`GEMINI_API_KEY`. To route through a team LiteLLM gateway instead (recommended
for Toloka's setup), point `harness_presets_file` at the shipped operator
overlay:

```yaml
evaluation:
  harness_adapter:
    type: "terminal_bench"
    params:
      agent_harness: "gemini-cli"
      agent_model: "openrouter/google/gemini-3.6-flash"
      harness_presets_file: "examples/terminal_bench/gemini_litellm_overlay.yaml"
```

The overlay expects `LITELLM_API_KEY` and `LITELLM_BASE_URL` in your `.env`. It
whole-replaces the gemini-cli entry with a GATEWAY-flavoured one that emits a
`~/.gemini/settings.json` flip (`security.auth.selectedType: gateway`) and
routes at `${secret:LITELLM_BASE_URL}/gemini`. LiteLLM's Gemini passthrough
serves `generateContent` requests at that path and forwards to Google using the
gateway's own credential.

`google/gemini-3.6-flash` and `google/gemini-3.1-pro-preview` are the two
Pro-tier slugs that resolve today. Google retired `gemini-3-pro-preview` and
moved it to "Previous models (Shut down)" — 3.1 Pro Preview is the successor.

## Running many models × tasks at once

The v2 matrix driver at `scripts/matrix/` (invoked via the same
`tolokaforge run` command with a matrix config) generates one run per
(model × task × pipeline) cell and writes a rolled-up `matrix.json`. For the
TF-vs-Harbor comparison shape used in the priority-model tracking doc, use
`tbench-compare` from the internal tolokaforge-tools repo — see its
[`RUNNING_MATRIX_COMPARISON.md`](https://github.com/toloka-partners/tolokaforge-tools/blob/main/docs/RUNNING_MATRIX_COMPARISON.md)
guide for the end-to-end recipe.

Between rows on long matrix runs, invoke the docker prune helper so
`/var/cache/apt/archives/` doesn't fill up:

```bash
scripts/matrix/prune-docker.sh
```

## Reading the results

Every trial produces the same bundle:

- **`aggregate.json`** — per-run means / stdev; the one file to grep for a
  headline number.
- **`trials/<task>/<N>/trajectory.yaml`** — messages, tool calls, reasoning
  tokens. The place to look when a score surprises you.
- **`trials/<task>/<N>/metrics.yaml`** — `api_calls`, token counts,
  `cost_usd`. Harness-mode trials often report `api_calls: 0` because the
  vendor CLI issues its own LLM calls out of process — that's a metrics gap,
  not a run failure. Real work is visible in the trajectory.
- **`trials/<task>/<N>/grade.yaml`** — component scores + verifier stdout. If
  the score is 0.0 or the container error message mentions "not running",
  look here first — it's usually a task-side setup issue (e.g. uvicorn died
  mid-session).
- **`trials/<task>/<N>/env.yaml`** — the recorded `HarnessSpec` (harness name,
  pinned CLI version, container_env). This is what the milestone-31 replay
  tests consume to detect drift.

## Common pitfalls

- **"docker compose up failed: no available IPv4 pool"** — Docker Desktop has
  a fixed number of subnet pools; long matrix runs exhaust them. Fix:
  `docker network prune -f`.
- **"container not running" at grade time on `fix-billing-holds`** — uvicorn
  died mid-session. The task now runs uvicorn under supervisord with
  `autorestart=true` so this shouldn't happen. If you see it, verify your
  cached tbench-fix-billing-holds image was rebuilt against the current
  Dockerfile (`docker rmi tbench-fix-billing-holds:*` and rerun).
- **Deterministic baseline scores across n=3 trials** — the model isn't
  responding at all. Check the trajectory for `APIEmptyResponseError` (Kimi
  K2.7 needs the middleware — see recipe above) or `UnknownError` (opencode
  provider misconfig — check the recipes above).
- **`${secret:X}` not resolving** — the secret is not in `.env` or the
  process didn't source it. Always invoke through `scripts/with_env.sh`.
- **Metrics show `cost_usd: null` on harness mode** — expected; the vendor
  CLI issues its LLM calls outside tolokaforge's client. Real cost is visible
  in the CLI's own output stream captured in `trajectory.yaml`.

## Further reading

- **[terminal-bench adapter README](../external_adapters/tolokaforge-adapter-terminal-bench/README.md)** —
  architectural details for extending the adapter or adding a harness. The
  "Routing options" section covers OpenRouter / LiteLLM / per-harness split.
- **[LLM layer](LLM_LAYER.md)** — how the engine-loop's `litellm` transport is
  configured and why it is engine-loop-only.
- **[ADR-0033](adr/0033-external-harness-registry.md)** — the
  `HarnessSpec` field list + why the registry is data.
- **[Priority-model evaluation tracking](https://github.com/toloka-partners/eval-tracking/blob/main/evaluation-tracking.md)**
  (internal) — the running matrix results.
