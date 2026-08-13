# tolokaforge-adapter-terminal-bench

Runs terminal-bench task packs on the tolokaforge engine.

## Environment contract

Terminal-bench tasks author a `docker-compose.yaml` that references
`T_BENCH_*` variables (plus `CPUS` / `MEMORY`) which terminal-bench's own
provisioner injects at up-time. The tolokaforge engine never sets those, so
the compose file is **synthesised** before provisioning — the adapter emits
a self-contained compose file the engine can bring up unchanged, alongside
a staging directory that carries the task's build context, tests, and log
mountpoints.

### Staging directory

`compose_synthesis.materialise_task_environment` writes each task's
materialised environment to
`{staging_root}/{task_id}-{digest}`, where `digest` is a content hash over
the task directory and the synthesis parameters. Two calls with the same
inputs resolve to the same directory — synthesis is idempotent.

Contents of a staging directory:

- A copy of the task pack (excluding `__pycache__`).
- `tests/test.sh` — normalised: when the task ships `run-tests.sh` at its
  root and no `tests/test.sh`, the root script is promoted to
  `tests/test.sh`; an existing `tests/test.sh` wins.
- `_logs/verifier/` and `_logs/agent/`, created empty. The per-trial
  compose context copy preserves them, so the agent-service log volumes
  find mountpoints.
- `docker-compose.tolokaforge.yaml` — the synthesised compose file.

### Synthesised compose contract

- Every service declared by the task is preserved. The adapter-owned
  variable set is **resolved at synthesis time** so no `${T_BENCH_*}`,
  `${CPUS}`, or `${MEMORY}` survives in the emitted file:

  | Variable                                    | Resolved value                                                                                            |
  | ------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
  | `T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME`     | `tbench-{task_id}:{image_tag}` (or `{image_registry}/{task_id}:{image_tag}` when `image_registry` is set) |
  | `T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME` | `tbench_${TOLOKAFORGE_TRIAL_SLUG}_{agent_service}`                                                        |
  | `T_BENCH_CONTAINER_LOGS_PATH`               | `/logs`                                                                                                   |
  | `T_BENCH_TASK_LOGS_PATH`                    | `./_logs`                                                                                                 |
  | `T_BENCH_CONTAINER_AGENT_LOGS_PATH`         | `/logs/agent`                                                                                             |
  | `T_BENCH_TASK_AGENT_LOGS_PATH`              | `./_logs/agent`                                                                                           |
  | `T_BENCH_TEST_DIR`                          | `/tests`                                                                                                  |
  | `CPUS`                                      | `str(meta.cpus)`                                                                                          |
  | `MEMORY`                                    | `{meta.memory_mb}M`                                                                                       |

  `${TOLOKAFORGE_TRIAL_SLUG}` is the one variable that survives into the
  emitted file — the engine writes it to the per-trial `.env` at provision
  time so each trial's containers get a unique name.

- The **agent service** (`main`, or the sole service when `main` is not
  declared) gets:
  - a pinned `image:` — `tbench-{task_id}:{image_tag}` for local builds
    (with the task's `build:` retained so the orchestrator can build it)
    or `{image_registry}/{task_id}:{image_tag}` when `image_registry` is
    set (with `build:` dropped so the image is pulled);
  - `container_name: tbench_${TOLOKAFORGE_TRIAL_SLUG}_{agent_service}`;
  - `volumes: ["./tests:/tests", "./_logs:/logs"]` — the relative bind
    mounts against the staging dir replace the `T_BENCH_*` log mounts;
  - `TEST_DIR=/tests` in its `environment:`.

- Two engine services are **injected** alongside the task's own:
  - `runner` (default image `tolokaforge-runner:local`) — exposes gRPC on
    `50051`, addresses `db-service` via `DB_SERVICE_URL`, depends on the
    agent service (`service_started`) and `db-service` (`service_healthy`).
  - `db-service` (default image `tolokaforge-db-service:local`) — exposes
    HTTP on `8000` with a `/health` probe.

- Fail-loud rules:
  - A task compose file declaring its own `runner` or `db-service` raises
    `ValueError` naming the collision and the task.
  - A compose file declaring more than one service and none named `main`
    raises `ValueError` naming the task and the declared services.
  - A floating `image_tag` (`latest`, `main`, `master`, `edge`, `stable`,
    `dev`, `develop`, `nightly`, `head`) is rejected — the same rule
    `EnvironmentManifest._check_pinned_images` enforces, applied earlier
    with the adapter's own message.

### Agent-image pre-build

The synthesised compose file references `tbench-{task_id}:{image_tag}` for
the agent service. That image must exist locally before the trial's
provision brings the stack up, otherwise `docker compose up --wait` triggers
a full image build inside the `--wait` window — and two concurrent trials
of the same task both build the same tag.

The adapter declares the build via
`DockerStackRequirements.image_builds` (one
`ComposeImageBuild(compose_file=..., service=agent_service)` per task); the
orchestrator's image-preparation step runs `docker compose build` once per
run before any trial provisions. Synthesis itself never shells out —
`materialise_task_environment` only reads and writes files.

## Harness mode — coding-harness CLIs

`agent_harness` selects which agent drives the trial. The default,
`engine-loop`, keeps tolokaforge's own turn loop and leaves the task image
untouched — named for what actually runs, since this repo installs no
Terminus-2 scaffold and a trial labelled `terminus-2` would be claiming a
comparison it did not run. Any other accepted value (`claude-code`, `codex`,
`gemini-cli`) installs that vendor's CLI into the task image and requires
`agent_model`: the CLI would otherwise pick its own default and the run
config's model would not be the one measured.

Each CLI is installed at a **pinned version** recorded in `HARNESSES`. The
version rides the layered image tag and `metadata["agent_harness_version"]`,
because the agent is the largest single variable in a coding benchmark and the
orchestrator skips a build whose tag already resolves locally — a floating
version would freeze per-machine and appear in no artifact.

The model reaches the CLI with any `openrouter/` prefix **stripped**. A vendor
CLI does not go through litellm; it reaches OpenRouter through the
`*_BASE_URL` variables below. Left on, the prefix makes the CLI select its own
direct-vendor handler, read the blank vendor key, and 401. The engine loop
keeps the prefix, which is what litellm needs to route.

### Image layering

The task's image and the CLI install are separate layers, so a task rebuild
and a harness switch don't invalidate each other:

| Compose service    | Image                                    | Role                                              |
| ------------------ | ---------------------------------------- | ------------------------------------------------- |
| `{agent}-base`     | `tbench-{task_id}:{image_tag}`           | The task's own build. Build-only — never started. |
| `{agent}`          | `tbench-{task_id}:{image_tag}-{harness}-{version}` | `FROM` the base, plus the pinned CLI.   |

The base service carries `profiles: [tolokaforge-build]`, which is what keeps
it out of `docker compose up`: it exists so the base image has a service name
`docker compose build` can address. `docker_stack_requirements()` declares
both builds, base first.

The layer's Dockerfile is generated into the staging directory as
`_harness/harness.Dockerfile` and runs `_harness/install-harness.sh`, a copy
of the adapter's own [`harness/install-harness.sh`](src/tolokaforge_adapter_terminal_bench/harness/install-harness.sh).
That script is the single place a harness's install steps live; an
unrecognised harness name aborts the image build rather than producing an
image whose missing CLI would surface as a trial-time "command not found".

Because the layered image tag carries the harness name and its pinned version,
switching harnesses or bumping a CLI can never reuse a stale cached image — and
the harness is part of the staging digest, so each gets its own staging
directory. A `.dockerignore` in the staging dir keeps the task sources, tests,
and log mountpoints out of the layer's build context.

`describe_environment_identity` records `{agent}-base` among the trial's
services even though the compose profile keeps it out of `up` — it is a
declared service that never runs.

When `image_registry` is set the base image is pulled rather than built, so
no base service is declared and only the layer is built locally.

### Provider credentials

A harness CLI authenticates against its vendor's API from inside the task
container, so it needs credentials the engine's own LLM layer never puts
there. Each harness ships the envelope its CLI needs as
`HarnessSpec.provider_env` — claude-code declares
`ANTHROPIC_API_KEY: "${secret:OPENROUTER_API_KEY}"` plus the Anthropic-shaped
OpenRouter `ANTHROPIC_BASE_URL` — so a run config that names only the harness
already reaches the provider, given the `OPENROUTER_API_KEY` secret.

`agent_provider_env` **unions over** that envelope, key by key, with the run
config winning on conflict. Pointing the CLI at a different endpoint therefore
does not drop the credential, and naming a vault key the harness does not ship
(`ANTHROPIC_AUTH_TOKEN`) does not drop the endpoint:

```yaml
evaluation:
  harness_adapter:
    type: "terminal_bench"
    params:
      agent_harness: "claude-code"
      agent_model: "openrouter/anthropic/claude-sonnet-4-6"
      agent_provider_env:
        ANTHROPIC_BASE_URL: "https://proxy.internal.example"
```

Under `engine-loop` there is no harness and no shipped envelope, so nothing is
forwarded unless the run config asks for it.

Every adapter param goes under `evaluation.harness_adapter.params`.
`EvaluationConfig` is `extra="ignore"`, so a block at the wrong depth is
dropped without a word and the run goes through the engine loop instead —
see [`run_harness.yaml`](../../examples/terminal_bench/run_harness.yaml) for a
complete working config.

Values resolve through `expand_secret_refs`, so a run config names a
credential rather than carrying it. A value containing a newline or a `$` is
refused: each becomes one line of the per-trial `.env`, where a newline splits
the line and a `$` starts an interpolation. Keys are checked against an
allow-list —
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `GOOGLE_API_KEY` — and anything else is
refused naming the accepted set.

The path from config to container:

`HarnessSpec.provider_env` + `harness_adapter.params.agent_provider_env` →
`StackPatch.inputs` →
`project_loader.resolve` → `EnvironmentManifest.stack_inputs` →
`PerTrialRuntimeBackend.provision` → the per-trial `.env` → compose
interpolation at up-time.

Values live only in the per-trial `.env` — never in the compose file, the
staging digest, or the image.

**The compose variable is namespaced, and that is load-bearing.** The agent
service gets `ANTHROPIC_API_KEY=${TBENCH_PROVIDER_ANTHROPIC_API_KEY}`, and
`TBENCH_PROVIDER_ANTHROPIC_API_KEY` is what the `.env` supplies. Compose
resolves `${VAR}` from the invoking shell's environment *before* the per-trial
`.env`, so naming the provider variable on both sides — or declaring it as a
bare pass-through — would let whatever `ANTHROPIC_API_KEY` the operator's shell
happens to hold silently replace the declared value. That puts a real
production key inside a benchmark container and into its trial artifacts.
Nothing sets the prefixed name by accident, so the container's environment is
exactly what the run config declared.

### Harness parity policy

A harness trial's job is to produce a reward comparable to the one the same
CLI would produce when driven by its out-of-tree host (the Harbor CLI is the
reference). Reproducibility across pipelines depends on how the CLI is
invoked, not just which model it calls — a stable divergence surfaced
during matrix validation traced to invocation-shape differences alone
(0.133 delta on `fix-billing-holds × claude-code`; closed to 0.033 once
the shape aligned).

`HarnessSpec` carries a small set of *parity knobs* — one field per
dimension of the alignment. Every knob is data on the spec, not code
somewhere else, so a per-harness policy is one entry to read.

| Aspect | The alignment | Field |
|---|---|---|
| CLI version | Pinned to a specific release. The pin rides the layered image tag and `metadata["agent_harness_version"]`. | `HarnessSpec.version` |
| Reasoning-mode flags | Flags between the CLI name and `--permission-mode` — for claude-code, `--verbose --output-format=stream-json` (the mode Harbor drives). | `HarnessSpec.flags_pre_permission` |
| Instruction path | `"argv"` (positional argument) or `"stdin"` (`printf "%s" '<instr>' \| cli …`). `"stdin"` sidesteps every shell-escape edge case a positional-arg prompt would have to survive. Claude Code uses stdin. | `HarnessSpec.instruction_channel` |
| Model routing | Env variables the resolved model exports into. Non-empty for CLIs whose sub-agents route model independently of the top-level `--model` flag: without the export, Task/Explore sub-agents fall back to the CLI's own sonnet-default and may pick a different provider mid-trial. Claude Code declares the quartet `ANTHROPIC_MODEL` + `_DEFAULT_SONNET_MODEL` / `_OPUS_MODEL` / `_HAIKU_MODEL` + `CLAUDE_CODE_SUBAGENT_MODEL`. When set, the redundant `--model` CLI flag is dropped. | `HarnessSpec.env_model_vars` |
| Static hardening env | Env pairs the compose `environment:` block writes for the agent service. Claude Code declares `IS_SANDBOX=1` (root-user bypass, without which the CLI refuses `--permission-mode=bypassPermissions` under UID 0) and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (opt-in telemetry off). | `HarnessSpec.container_env` |
| Provider envelope | The variables the CLI needs to reach its provider, as defaults a run config unions over. Claude Code declares `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL`; codex the `OPENAI_*` pair; gemini-cli `GOOGLE_API_KEY`. | `HarnessSpec.provider_env` |
| Model-name form | Whether a leading `vendor/` namespace is dropped before the model reaches the CLI. Codex and gemini-cli catalogs use bare names; a namespaced string makes them drop OpenRouter routing for the vendor's default endpoint. | `HarnessSpec.strip_vendor_namespace` |

**Skills are deliberately not aligned.** Harbor copies the operator's
`~/.claude/skills/` into the container, so its Claude sees whatever
personal skills the person running the eval has installed on their
laptop. That is not reproducible across operators and it is not a
property a benchmark reward should quietly depend on, so the TF adapter
installs no skills. The delta this creates is the *right* delta — it
shows up as "the container has zero skills", stable across machines and
dates.

Some CLIs need file-based configuration that no env var (compose or
otherwise) can supply. `HarnessSpec.pre_exec_shell` is a shell fragment
chained before the CLI with `&&`: codex reads `openai_base_url` from
`$CODEX_HOME/config.toml` and the API key from `$CODEX_HOME/auth.json`,
so its `pre_exec_shell` writes both files from the compose-forwarded
env vars before invoking `codex exec`.

### The harness registry is data

The shipped specs live in
[`data/harnesses.yaml`](src/tolokaforge_adapter_terminal_bench/data/harnesses.yaml),
loaded into `HARNESSES` at import. Adding a harness or bumping a pinned CLI
version is a YAML edit; a typo (unknown field, missing required field,
unparseable document) is refused at load naming the file and the harness key,
so no entry is ever silently dropped.

`install_method` picks how `install-harness.sh` puts the CLI in the image and
what `install_source` has to be — the pair is validated at load, so a source
the method cannot consume fails there rather than at `docker build`:

| `install_method` | `install_source`                              | What the layer runs                                              |
| ---------------- | --------------------------------------------- | ---------------------------------------------------------------- |
| `npm` (default)  | package name (`@scope/` allowed)               | `npm install -g <source>@<version>`, after installing Node 20 LTS |
| `pip`            | PyPI distribution name                         | `pip install --no-cache-dir <source>==<version>`                  |
| `curl-bash`      | installer-script URL                           | downloads it, then `sh <script> --version <version>`              |
| `binary`         | URL to a `.tar.gz`/`.tgz` or a bare executable | unpacks / installs it into `/usr/local/bin`                       |

The script records what it installed at `/opt/tolokaforge/installed-version.txt`
inside the layer. `version: "latest"` is accepted by `npm` and `pip`, which can
be asked afterwards what they resolved; `curl-bash` and `binary` refuse it,
since neither can report what an installer chose and an unrecorded agent
version is not a benchmark result.

An operator ships their own entries without an adapter release by pointing
`harness_presets_file` at a second YAML of the same shape — see
[ADR 0031](../../docs/adr/0031-external-harness-registry.md) for the decision
record, which mirrors the operator-pointed preset file in
[ADR 0002](../../docs/adr/0002-external-model-registry.md) for the model
registry. Merging is
**whole-entry**: a harness the overlay declares replaces the shipped spec
completely and is validated on its own, and a harness it does not declare is
untouched. A field-wise merge would let an overlay inherit a shipped default it
never meant to keep — a pinned version, a mandatory permission flag — and
produce an invocation neither side declared. An overlay may also add a harness
the adapter does not ship; `install-harness.sh` installs whatever the entry's
`install_method` / `install_source` / `version` name. The path may be absolute or relative to the
working directory; a missing file, malformed YAML, or an invalid entry is
refused when the adapter is constructed, naming the file and the failing key.

```yaml
evaluation:
  harness_adapter:
    type: "terminal_bench"
    params:
      harness_presets_file: "./harness_presets.yaml"
      agent_harness: "codex"
```

### Trial-level timeout

Under harness mode the whole trial runs inside a single `docker exec`, so the
`bash` tool's `timeout_s` carries the task's `[agent] timeout_sec` rather than
the 120 s per-call default. That one value governs both timers the runner
applies — the RPC deadline and the compose-exec wrapper's `subprocess.run` —
and they must agree, because abandoning the RPC does not stop the subprocess.
A run whose effective episode budget is below the harness budget is **refused**
rather than silently shortened: a cut-short exec would be graded while the CLI
was still writing to the container.
