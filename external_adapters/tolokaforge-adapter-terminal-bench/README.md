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
of [`install-harness.sh`](../../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/install-harness.sh)
from the `tolokaforge-coding-harnesses` package.
That script is the single place a harness's install steps live; an
unrecognised harness name aborts the image build rather than producing an
image whose missing CLI would surface as a trial-time "command not found".

Because the layered image tag carries the harness name and its pinned version,
switching harnesses or bumping a CLI can never reuse a stale cached image — and
the harness is part of the staging digest, so each gets its own staging
directory. A `.dockerignore` in the staging dir keeps the task sources, tests,
and log mountpoints out of the layer's build context; everything the layer
copies is re-included there by name.

Nothing else is in that Dockerfile by default. A task pack's skills bundle
reaches it only because the adapter's shipped `SkillDelivery` —
`ImageLayerSkillDelivery` — appends its own `COPY` and `.dockerignore`
exceptions after the CLI install, so editing a bundle invalidates the copy
layer without reinstalling the CLI. A run driving the adapter with a different
delivery gets a harness image with no skills in it.

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

### Routing options — OpenRouter, LiteLLM, or a mix

`HarnessSpec.provider_env` names CLI-native env vars (`ANTHROPIC_BASE_URL`,
`OPENAI_BASE_URL`, `GOOGLE_GEMINI_BASE_URL`, `KIMI_MODEL_BASE_URL`, …); its
values name literal URLs and credential keys. The adapter has no opinion on
which gateway or vendor those URLs belong to — an overlay swaps the whole
`provider_env` block, and the CLI reads whatever it was handed. That leaves the
operator free to choose:

**OpenRouter (the shipped default).** Every shipped harness except gemini-cli
targets `openrouter.ai/api` (Anthropic-compat surface for claude-code /
opencode, OpenAI-compat surface for codex / grok-build / kimi-code). One
credential — `OPENROUTER_API_KEY` — covers all five. gemini-cli is the exception:
its wire protocol (Google's `generateContent`) is not on OpenRouter's surface,
so the shipped default targets Google directly and `GEMINI_API_KEY` is required.

**LiteLLM (operator overlay).** A team-hosted LiteLLM gateway centralises
credentials and can serve wire protocols OpenRouter does not — most notably
Google's `generateContent`, via LiteLLM's Gemini passthrough at
`{base}/gemini/v1beta/models/…`. The route is a `harness_presets_file` overlay
that whole-replaces the harness entry with a LiteLLM-flavoured `provider_env`
and `container_env`. A worked example ships at
[`examples/terminal_bench/gemini_litellm_overlay.yaml`](../../examples/terminal_bench/gemini_litellm_overlay.yaml)
for gemini-cli.

**Per-harness split.** Because each harness's `provider_env` resolves
independently, one run can leave five harnesses on OpenRouter and route
gemini-cli through LiteLLM by naming a `harness_presets_file` that only overlays
the gemini-cli entry — the other five stay as shipped. This is the current
recommended shape for Toloka's own matrix runs.

`LITELLM_API_KEY`, `LITELLM_BASE_URL`, and `GOOGLE_GEMINI_BASE_URL` are all in
`PROVIDER_ENV_KEYS` (allow-listed by the adapter), so switching a harness to
LiteLLM does not need an adapter release — it is a data-only change on the
overlay side.

### Harness parity policy

A harness trial's job is to produce a reward comparable to the one the same
CLI would produce when driven by its out-of-tree host — the reference
vendor-CLI invocation. Reproducibility across pipelines depends on how the
CLI is invoked, not just which model it calls — a stable divergence surfaced
during matrix validation traced to invocation-shape differences alone
(0.133 delta on `fix-billing-holds × claude-code`; closed to 0.033 once
the shape aligned).

`HarnessSpec` carries a small set of *parity knobs* — one field per
dimension of the alignment. Every knob is data on the spec, not code
somewhere else, so a per-harness policy is one entry to read.

| Aspect | The alignment | Field |
|---|---|---|
| CLI version | Pinned to a specific release. The pin rides the layered image tag and `metadata["agent_harness_version"]`. | `HarnessSpec.version` |
| Reasoning-mode flags | Flags between the CLI name and `--permission-mode` — for claude-code, `--verbose --output-format=stream-json` (the mode the reference invocation drives). | `HarnessSpec.flags_pre_permission` |
| Instruction path | `"argv"` (positional argument) or `"stdin"` (`printf "%s" '<instr>' \| cli …`). `"stdin"` sidesteps every shell-escape edge case a positional-arg prompt would have to survive. Claude Code uses stdin. | `HarnessSpec.instruction_channel` |
| Model routing | Env variables the resolved model exports into. Non-empty for CLIs whose sub-agents route model independently of the top-level `--model` flag: without the export, Task/Explore sub-agents fall back to the CLI's own sonnet-default and may pick a different provider mid-trial. Claude Code declares the quartet `ANTHROPIC_MODEL` + `_DEFAULT_SONNET_MODEL` / `_OPUS_MODEL` / `_HAIKU_MODEL` + `CLAUDE_CODE_SUBAGENT_MODEL`. When set, the redundant `--model` CLI flag is dropped. | `HarnessSpec.env_model_vars` |
| Static hardening env | Env pairs the compose `environment:` block writes for the agent service. Claude Code declares `IS_SANDBOX=1` (root-user bypass, without which the CLI refuses `--permission-mode=bypassPermissions` under UID 0) and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (opt-in telemetry off). | `HarnessSpec.container_env` |
| Provider envelope | The variables the CLI needs to reach its provider, as defaults a run config unions over. Claude Code and OpenCode declare `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` (both route through OpenRouter's Anthropic-compat surface); codex the `OPENAI_*` pair; Kimi Code the `KIMI_MODEL_*` pair; Grok Build the `OPENROUTER_*` pair; gemini-cli `GEMINI_API_KEY` (public default routes at Google AI Studio; an operator overlay swaps in a GATEWAY-compatible endpoint like a LiteLLM proxy that speaks Google's native `generateContent` shape). | `HarnessSpec.provider_env` |
| OpenRouter-prefix routing | Whether the `openrouter/` marker on the model name reaches the CLI. Stripped by default: a vendor CLI reads `*_BASE_URL` for OpenRouter routing and would otherwise select its own direct-vendor handler and 401. Preserved on opencode, whose config template defines a provider *literally* named `openrouter` and expects the caller to route `openrouter/<vendor>/<model>` to it. | `HarnessSpec.strip_openrouter_prefix` |
| Request-body / header rewrite | An HTTP proxy that lands inside the trial image and rewrites the CLI's provider requests before they leave the container. Declares body-field deep-merges and per-request header injections keyed on a URL-path filter. Ships stdlib-only (`middleware_proxy.py`). Motivating case: kimi-code injects `provider.only=["moonshotai"]` on every `/chat/completions` body to force Moonshot AI first-party routing (OpenRouter's default fan-out to INT4/FP4 third-party providers returns empty completions on tool-call continuation). | `HarnessSpec.request_middleware` |
| Model-name form | Whether a leading `vendor/` namespace is dropped before the model reaches the CLI. Codex and gemini-cli catalogs use bare names; a namespaced string makes them drop OpenRouter routing for the vendor's default endpoint. | `HarnessSpec.strip_vendor_namespace` |
| Model-flag form | Whether the model flag and its value are two argv words (`--model gpt-5`) or one (`--model=gpt-5`). A CLI parsing its flags strictly accepts only one of the two. | `HarnessSpec.model_flag_style` |
| File-based configuration | Files the CLI reads its configuration from, rendered per trial. | `HarnessSpec.config_files` |
| Skills | Where a task pack's own `harness_skills_dir` bundle lands — absolute, or rooted at a `${HOME}` / `${CONFIG_HOME}` construct the adapter's `PathResolver` answers. *How* it gets there is the adapter's `SkillDelivery`; the shipped answer is an image-layer `COPY`. Unset means the harness installs no skills; the operator's `~/.claude/skills` is never a source. | `HarnessSpec.skills_dir_target` |

**Operator skills are deliberately not aligned.** The out-of-tree host we
compared against copies the operator's `~/.claude/skills/` into the
container, so its Claude sees whatever personal skills the person running
the eval has installed on their laptop. That is not reproducible across
operators and it is not a
property a benchmark reward should quietly depend on, so the adapter
never reads the operator's home directory. The delta this creates is the
*right* delta — stable across machines and dates.

A task that genuinely needs domain skills ships them itself. Its
`task.yaml` declares `harness_skills_dir: <task-relative path>`, and that
directory reaches the CLI's skills path — `HarnessSpec.skills_dir_target`,
`${HOME}/.claude/skills/` for claude-code, which the shipped
`LinuxRootResolver` places at `/root/.claude/skills/`.
This keeps every property the smuggled version loses: the bundle is
versioned with the tests it is scored against, it shows up in a `git
diff` on the task pack, and its content hash is recorded on the trial
artifact as `metadata["harness_skills_bundle_sha"]` (per-file sha256,
hashed in sorted path order, so a rename moves it as surely as an edit).

The declared path is refused unless it resolves to a directory *inside*
the task pack — checked after symlink resolution, since a link out of
the pack would reintroduce exactly the host contamination the policy
rejects. A harness leaving `skills_dir_target` unset installs no skills:
a pack that ships them still runs under it, without them and with a
warning, so one task stays comparable across harnesses. The bundle hash
is written only when a bundle was handed to the run's `SkillDelivery`, so
an absent key reads as "this agent had no skills" and never as "unknown".
That the bundle then arrives is the delivery's contract: one that cannot
place it raises.

`skills_dir_target` says *where* the bundle lands; `SkillDelivery` says
*how* it gets there. The shipped `ImageLayerSkillDelivery` appends a `COPY`
to the generated harness Dockerfile, so the bundle rides the image; an
embedder driving a different runtime passes its own as
`TerminalBenchAdapter(params, skill_delivery=…)` and the image stops
carrying skills at all. The target it receives has already been through
the run's `PathResolver`, and `ImageLayerSkillDelivery` refuses one that
still carries a `${…}` construct: Docker expands a `COPY` destination from
the *image's* own `ENV`, so a base image declaring `ENV HOME=/home/agent`
would answer a question the resolver was asked. That case is foreclosed
deliberately — letting Docker place the skills while the resolver places
the config files would give one harness two different homes. A runtime
that wants image-`ENV` semantics supplies a `PathResolver` returning those
paths, and stays the single authority on where its CLI lives.

Some CLIs need file-based configuration that no env var (compose or
otherwise) can supply — codex reads `openai_base_url` from
`$CODEX_HOME/config.toml` and its API key from `$CODEX_HOME/auth.json`,
and honours neither env var alone. `HarnessSpec.config_files` maps a
container path to a Jinja template; each file is written by the shell
chain that runs before the CLI. A path is one of three things:

- an absolute path (`/etc/mycli/config.toml`), which every resolver returns
  unchanged;
- a `${HOME}` or `${CONFIG_HOME}` construct, which the adapter's
  `PathResolver` answers while assembling the command. The shipped
  `LinuxRootResolver` reads them as `/root` and `/root/.config`; an embedder
  driving the adapter from Python passes its runtime's own answer as
  `TerminalBenchAdapter(params, path_resolver=…)`. These two names are the
  *adapter's* answer, not the container's — a container that changed its user
  would not change them;
- any other `$VAR`-rooted reference
  (`${CODEX_HOME:-$HOME/.codex}/config.toml`), which reaches the container
  verbatim and is expanded by the container's own shell — so a harness need
  not assume the container's user, and a variable the resolver does not know
  keeps the container's answer.

Templates render against four variables and no
others — `model` (as the CLI receives it), `provider` (the routing prefix
the run config's model named), `base_url` (the provider envelope's
`*_BASE_URL` value, after any run-config override), and `api_key_env`
(the *name* of its `*_API_KEY` entry). An unknown name is a load-time
error, since a silently empty substitution surfaces as a provider auth
failure many layers from the typo.

Content is written through a double-quoted `printf`, so a `$VAR`
reference expands inside the container: `{"{{ api_key_env }}":
"${{ api_key_env }}"}` puts the credential in the file without the
assembled command — which trial metadata records — ever carrying its
value. A template must therefore not carry a literal `$` it does not
want expanded.

### The harness registry is data

The shipped specs live in
[`data/harnesses.yaml`](../../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml),
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

An external contributor ships a harness as a **pip-installable bundle**: a
package declaring the `tolokaforge_adapter_terminal_bench.harness_registries`
entry-point group, whose value names the package shipping a `harnesses.yaml`
beside its `__init__.py`.

```toml
[project.entry-points."tolokaforge_adapter_terminal_bench.harness_registries"]
my_org = "my_org.tolokaforge_harnesses"
```

The adapter discovers every installed bundle when it is constructed and unions
them over the shipped registry, logging at INFO which distribution contributed
which harness keys — and at WARNING when a bundle replaces a shipped entry,
since the pinned version and argv for that name are then not the ones this
repo ships. Two installed bundles claiming the same harness name are refused
naming both distributions: they disagree about what that name installs and how
it is invoked, so install order must not decide which agent a benchmark
measures. Discovery is on by default and skipped entirely by
`disable_harness_plugins: true`, for runs that must reproduce independently of
what else is installed in the environment. See
[ADR 0034](../../docs/adr/0034-external-harness-plugin-discovery.md).

An operator ships their own entries without an adapter release by pointing
`harness_presets_file` at a second YAML of the same shape — see
[ADR 0033](../../docs/adr/0033-external-harness-registry.md) for the decision
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

The three layers compose lowest to highest — shipped YAML, then installed
plugin bundles, then the overlay — with whole-entry replacement at each
transition.

Every layer writes its `config_files` keys and its `skills_dir_target` against
the same path vocabulary: `${HOME}` and `${CONFIG_HOME}` are the *adapter's*
answers, supplied by the run's `PathResolver`, and any other `${VAR}`
construct is left for the container's own shell. The shipped entries use the
vocabulary — claude-code's skills land at `${HOME}/.claude/skills/`,
opencode's config at `${CONFIG_HOME}/opencode/opencode.json` — so a second
runtime consumes this YAML unforked by supplying its own resolver. An absolute
`/root/...` path stays valid and resolves to itself, so an overlay writing one
needs no change.

### What a run records about its registry

Because the effective registry is composed from three layers, two runs of the
same config on two machines can drive different CLI versions. So the adapter
reports what it resolved: `engine_run_state.json` carries it under
`adapter_fingerprints["terminal_bench"]["harness"]` (see
[`docs/OUTPUT_FORMAT.md`](../../docs/OUTPUT_FORMAT.md) §
`engine_run_state.json`). Every field is read off the registry the run
composed.

| Field | Type | Meaning |
|---|---|---|
| `resolved_sha256` | 64-hex string | Digest over the post-plugin, post-overlay registry — the specs that actually drove the run. |
| `shipped_sha256` | 64-hex string | Digest over the shipped `data/harnesses.yaml` alone. |
| `agent_harness` | string | The harness this run selected (`engine-loop` when no CLI drives the trial). |
| `agent_harness_version` | string \| null | The effective spec's pinned version — `null` under the engine loop. |
| `overlay_file` | string \| null | Resolved absolute path of the `harness_presets_file` overlay. |
| `plugin_bundles` | list | `{distribution, version, harnesses[]}` per installed registry bundle, ordered by distribution. |

`resolved_sha256 == shipped_sha256` exactly when neither a bundle nor an
overlay changed anything. An overlay or a plugin that alters any spec moves
`resolved_sha256`; `shipped_sha256` moves only when this repo's own registry
changes, so it is the fixed reference point the other digest is read against.
Both digests hash the *parsed* specs, so reformatting the YAML or editing a
comment leaves them where they are. `data/registry_meta.yaml` is outside both:
it is shipped-only rather than layerable, and folding it in would blur what
`shipped == resolved` means.

Two things this payload's names do not mean:

- `overlay_file` is the **registry** overlay (`harness_presets_file`, the
  YAML declaring harness specs). It is unrelated to `engine_run_state.json`'s
  top-level `presets_file`, which is the engine's *model-preset* overlay.
- `agent_harness_version` is the same value the per-trial
  `metadata["agent_harness_version"]` carries — both read `HarnessSpec.version`
  off the effective registry (the CLI-version row in [Harness parity
  policy](#harness-parity-policy) above), so they cannot disagree. They differ
  in *presence*, not value: under `engine-loop` the per-trial key is absent,
  because that surface uses absence to say "no CLI ran", while the run-level
  field is an explicit `null` in a fixed-shape record.

### Trial-level timeout

Under harness mode the whole trial runs inside a single `docker exec`, so the
`bash` tool's `timeout_s` carries the task's `[agent] timeout_sec` rather than
the 120 s per-call default. That one value governs both timers the runner
applies — the RPC deadline and the compose-exec wrapper's `subprocess.run` —
and they must agree, because abandoning the RPC does not stop the subprocess.
A run whose effective episode budget is below the harness budget is **refused**
rather than silently shortened: a cut-short exec would be graded while the CLI
was still writing to the container.
