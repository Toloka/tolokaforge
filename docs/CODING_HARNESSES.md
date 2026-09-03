# Coding-harness mode

A trial normally runs the engine's own LLM turn loop: `litellm.completion()` is
called every step, and the response policies declared for the active model
apply. **Coding-harness mode** hands that loop over to a vendor coding-agent
CLI (`claude-code`, `codex`, `gemini-cli`, `kimi-code`, `opencode`,
`grok-build`) installed inside the trial container. The engine still
orchestrates the trial — compose bring-up, bash exec, trajectory capture,
grading — but does not touch the LLM turn loop.

Both modes produce the same per-trial bundle layout, so downstream tooling
reads one shape regardless of which produced it.

## When to reach for each mode

Pick by what you are measuring.

| You want to measure | Use | Because |
|---|---|---|
| A model's raw capability on a task | **Engine-loop mode** | The engine's [`ModelCapabilities`](LLM_LAYER.md) policies apply — schema sanitizers, cache markers, reasoning replay, response coercion. Cost and tokens are honestly reported via `litellm`. |
| A coding CLI's scaffolding on top of a model | **Harness mode** | The CLI's own prompt shape, tool ontology and step logic dominate the outcome — you're evaluating the whole vendor product, not the bare model. |
| Head-to-head between two CLIs on the same task pack | **Harness mode** | Change one field (`models.agent.coding_harness`); everything else stays constant. |
| A model in a way another team can independently reproduce end-to-end | **Harness mode** with a shipped CLI | The CLI version is pinned in the registry; the artifact records the pin. |

## Declaring a harness

A run config declares the harness alongside the model on `models.agent`:

```yaml
models:
  agent:
    provider: "openrouter"
    name: "openrouter/anthropic/claude-sonnet-4-6"
    coding_harness: "claude-code"   # any shipped harness name; omit for engine loop
    temperature: 0.0
evaluation:
  projects: ["examples/native/coding_harness"]
  harness_adapter:
    type: "native"
```

`models.agent.coding_harness = null` (the default) keeps the engine's turn loop.
A non-empty string is a coding-harness name resolved against the effective
registry ([ADR-0033](adr/0033-external-harness-registry.md)) and the CLI
consumes `models.agent.name` verbatim (with per-harness vendor-prefix
handling declared in the shipped
[`data/harnesses.yaml`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml)).

### Overriding the CLI version

The shipped registry pins each CLI to a specific version so scored runs
reproduce across machines. To try a different release without editing the
registry, name it inline on the slug:

```yaml
models:
  agent:
    coding_harness: "claude-code@2.2.0"   # overrides the shipped pin
```

Equivalently, use the struct form (identical after parse — visible in a
config diff):

```yaml
models:
  agent:
    coding_harness: "claude-code"
    coding_harness_version: "2.2.0"
```

The version segment passes to `install-harness.sh` at trial-image build
time and lands on the recorded artefact's `HarnessSpec.version`, so
replay can see the override. Trade-off: reproducibility. Two operators
running the "same" run config with different overrides get different
scores. Use for ad-hoc research; leave the field off for scored runs.
Setting both the slug's `@version` and `coding_harness_version` to
different values is a hard error naming both.

### How the mode gets selected

The orchestrator selects an `AgentDriver`
([ADR-0039](adr/0039-coding-harness-adapter-agnostic.md)) per run from
`models.agent.coding_harness`: absent → `EngineLoopDriver` (a
no-op passthrough); non-empty → `CodingHarnessDriver`. The driver
attaches to an adapter that stages a per-trial container (`native`,
`terminal_bench` today) and refuses to attach otherwise, naming the
compatible adapter set. The adapter itself carries no coding-harness
state.

### Credentials — how the shield works

**Coding-harness runs never receive the real LLM provider credential
in the trial container.** The vendor CLI runs inside the container and
is the LLM client that originates requests to OpenRouter / Anthropic /
OpenAI; if the real key sat in the container's `environment:`, the
model would read it in one `printenv` call. Instead:

- Every shipped harness in
  [`data/harnesses.yaml`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml)
  carries a `credential_gateway` block declaring the upstream URL,
  the real-token env var (read via `SecretManager`), the auth header
  format, and a **dummy** value the CLI is allowed to see.
- The `CodingHarnessDriver` adds a `tolokaforge-llm-gateway` sidecar
  service to the trial's compose stack — the shipped
  `tolokaforge-runner:local` image running `python -m
  tolokaforge.runner.llm_gateway_serve` on port 8080. The sidecar
  reads the real credential once at bootstrap through `SecretManager`
  and swaps in the correct auth header on every forwarded request.
- The CLI's own compose service receives only the dummy token + a
  base URL pointing at `http://tolokaforge-llm-gateway:8080`. Docker
  compose's DNS resolves the hostname over the shared internal
  network — no `extra_hosts`, no host-network hop.
- For CLIs that write an on-disk auth file (`codex`, `opencode`),
  the file carries the dummy too.
- The sidecar's service is registered as `bridged_services` so
  netpolicy attaches it to both the internal (CLI-reachable) and
  edge (has-egress) networks under any pack policy including
  `no_internet`. The shielded token is also `stripped_container_secrets`,
  so `inject_runner_credentials` omits it from the runner container's
  payload — the credential lives in exactly one service in the trial
  stack.

See [ADR-0041](adr/0041-coding-harness-credential-gateway.md) and
[`docs/SECURITY.md`](SECURITY.md) for the full threat model.

**Escape hatch**: `models.agent.disable_credential_gateway: true`
reverts to the pre-shield behavior — real token in the container env.
Intended for the rare CLI a proxied backend cannot drive; none of the
shipped harnesses need it today. The driver logs a warning naming the
harness.

**Unshielded harness**: `gemini-cli` currently ships
`credential_gateway: null` — its REST auth uses `x-goog-api-key`
(not `Bearer`) and its request paths are model-dynamic
(`/v1beta/models/<model>:generateContent`). Tracked by
[#1311](https://github.com/Toloka/tolokaforge/issues/1311).

## What each shipped adapter provides

Two adapters stage per-trial containers the `CodingHarnessDriver` layers onto today. Both accept `models.agent.coding_harness`; they differ in how they materialise the trial container around the CLI.

### The native adapter

Bundled with the engine (`tolokaforge.adapters.native.NativeAdapter`).
In harness mode the adapter mints its own `TaskDescription`, bypassing
MCP wiring: a single `bash` tool routed through
`DockerComposeExecToolWrapper` (`service: "main"`,
`compose_project_prefix: "tfnative_"`, `agent_visible_dir: "/work"`),
`test_execution` grading against `/logs/verifier/reward.txt`, and the
four-key harness metadata handshake. The example pack in
[`examples/native/coding_harness/`](../examples/native/coding_harness/)
(fix-factorial) is the reference layout — a single-service compose
stack (Python 3.11), one small bug the agent fixes, a `tests/test.sh`
verifier.

### The terminal-bench adapter

Ships out-of-tree as
[`tolokaforge-adapter-terminal-bench`](../external_adapters/tolokaforge-adapter-terminal-bench/).
Materialises the task's own compose stack, injects `runner` /
`db-service` sidecars, and layers the harness image via the
adapter-local
[`compose_synthesis`](../external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/compose_synthesis.py)
staging: `stage_task` produces a `StagedTask` pointing at a
task-local `docker-compose.tolokaforge.yaml` with the pack's own
service + a synthesised base build target, and the
`CodingHarnessDriver` writes the harness image layer on top. Example packs live
under [`examples/terminal_bench/`](../examples/terminal_bench/)
(`fix-billing-holds`, `fix-airline-segmentation`) with the shipped
driver [`examples/terminal_bench/run_harness.yaml`](../examples/terminal_bench/run_harness.yaml).
The trial's agent-visible dir is `/app`.

## Grading composability

Harness mode composes with **any** grading method. Two paths:

- **`test_execution`** — the driver's default. The runner reads a reward
  float from `/logs/verifier/reward.txt` and returns before assembling
  jsonpath state. Both shipped example packs use this shape (`tests/test.sh`
  writes the file). Byte-identical wire output between adapter versions is
  proven by the canonical snapshot at
  `tests/canonical/snapshots/tbench_echo_hello_harness/`.
- **`state_checks` / `transcript` / `rubric`** — assemble through the
  standard combiner. When a harness-mode trial's metadata carries both
  `agent_harness_command` and `agent_visible_dir` AND a
  `DockerComposeExecToolWrapper` is registered for it, the runner
  snapshots the container's agent-visible directory into
  `state["filesystem"]` via
  [`tolokaforge/runner/harness_state.py`](../tolokaforge/runner/harness_state.py).
  A `state_checks` assertion against
  `$.filesystem['/work/factorial.py']` resolves against what the CLI
  actually left behind — the same shape a non-harness native task uses
  today.

## The six shipped harnesses

Six vendor coding-agent CLIs ship in-tree. The catalog lives in
[`tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml);
the package's [`README.md`](../tolokaforge_coding_harnesses/README.md#shipped-harnesses)
carries the version-pin table.

| `models.agent.coding_harness` | Vendor CLI | Install |
|---|---|---|
| `claude-code` | `@anthropic-ai/claude-code` | npm |
| `codex` | `@openai/codex` | npm |
| `gemini-cli` | `@google/gemini-cli` | npm |
| `kimi-code` | `@moonshot-ai/kimi-code` | npm |
| `opencode` | `opencode-ai` | npm |
| `grok-build` | `x.ai/cli` install script | curl-bash |

Provider envelopes, model-name conventions and per-CLI quirks (permission
flags, root-under-sandbox, config-file precedence) live in the YAML
alongside each entry with the failure mode each flag prevents recorded
inline.

## One-command demo

Run `claude-code` on the native `fix-factorial` pack:

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/coding_harness/run_harness.yaml
```

Or on the terminal-bench `fix-billing-holds` pack:

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/terminal_bench/run_harness.yaml
```

Swap `models.agent.coding_harness` or `models.agent.name` to matrix over CLIs
or models. Per-trial artifacts land under the run's `evaluation.output_dir`.

Prerequisites (Docker daemon, `uv`, `.env` with a provider key), the
switch between engine-loop and harness mode, per-harness recipes
(Kimi K2.7 middleware, opencode routing, Gemini LiteLLM gateway) and
result-bundle layout all live in the end-to-end guide:
[**docs/RUNNING_TERMINAL_BENCH.md**](RUNNING_TERMINAL_BENCH.md).

## Per-harness quick reference

Rows here name the shape you'll write in a run config; per-CLI quirks
live next to each entry in the shipped
[`harnesses.yaml`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml).

| Harness | Example `models.agent.name` | Notes |
|---|---|---|
| `claude-code` | `openrouter/anthropic/claude-sonnet-5` | Anthropic-compat via OpenRouter. |
| `codex` | `openrouter/openai/gpt-5.6-sol` | OpenAI-compat via OpenRouter; writes `~/.codex/config.toml` + `auth.json`. |
| `gemini-cli` | `openrouter/google/gemini-3.6-flash` | Shipped default routes at Google directly. LiteLLM gateway path via `harness_presets_file` — see [RUNNING_TERMINAL_BENCH.md § Gemini CLI](RUNNING_TERMINAL_BENCH.md#recipe--gemini-cli-litellm-gateway). |
| `kimi-code` | `openrouter/moonshotai/kimi-k3` | Also `kimi-k2.7-code` — the shipped `request_middleware` pins Moonshot AI first-party routing on OpenRouter automatically. |
| `opencode` | `anthropic/claude-sonnet-4-6` | Routes through opencode's shipped `anthropic` provider block (`baseURL` points at OpenRouter's Anthropic-compat surface). Non-Anthropic vendors need an operator overlay populating the `openrouter` block's `models` dict — see the caveat below. |
| `grok-build` | `openrouter/x-ai/grok-4.5` | Auto-configures `~/.grok/config.toml` for OpenRouter. |

Two things about the `opencode` row are load-bearing on 1.18.x:

- The `openrouter` provider block that opencode's shipped config declares
  carries an empty `models` dict. opencode 1.18.x validates every incoming
  slug against that dict before any HTTP call and refuses unknown ones with
  `ProviderModelNotFoundError`. Reaching a non-Anthropic vendor through this
  block needs an operator overlay populating `models` for the concrete slug
  the run uses.
- Claude family goes through opencode's separate, natively-populated
  `anthropic` provider block instead, whose `baseURL` already points at
  OpenRouter's Anthropic-compat surface. That is why the shipped example
  above names `anthropic/claude-sonnet-4-6`, not `openrouter/anthropic/…`.

## Adding a harness

Two shapes. In-tree — edit
[`data/harnesses.yaml`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml)
and any new install method in
[`install-harness.sh`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/install-harness.sh);
out-of-tree — ship a Python entry-point plug-in under
`HARNESS_REGISTRY_ENTRY_POINT_GROUP` ([ADR-0034](adr/0034-external-harness-plugin-discovery.md)).
Both are documented in
[`tolokaforge_coding_harnesses/README.md`](../tolokaforge_coding_harnesses/README.md).

## Hosting harness runs on a new adapter

An adapter opts into hosting harness runs by overriding
[`BaseAdapter.stage_task(task_id) -> StagedTask | None`](../tolokaforge/adapters/base.py)
to materialise a per-trial staging directory with a synthesised compose
file the driver can layer the CLI install onto. The `StagedTask` frozen
dataclass names the compose file, agent service, base image, and
compose project prefix — everything `CodingHarnessDriver` needs to
write the harness `Dockerfile`, add sidecars, and rewrite the
compose `environment:`. The orchestrator refuses
`models.agent.coding_harness` against an adapter whose `stage_task`
returns `None`, naming the currently opted-in set. Adapters carry no
coding-harness state and never import driver code. Design records:
[ADR-0039](adr/0039-coding-harness-adapter-agnostic.md) (driver
protocol) and
[ADR-0041](adr/0041-coding-harness-credential-gateway.md) (credential
shield).

## Gateway routing (external runtimes only)

A second consumer that attaches to an already-running container — a runtime
this repo does not ship — reads the same registry data and provisions the
same files, envs and endpoints itself. `HarnessSpec.gateway_route` carries the
recipe as data for those runtimes; nothing in this repo consumes it, so a
route changes nothing about the trial command run here.
[ADR-0037](adr/0037-runtime-gateway-as-harness-data.md) is the design
record. `tests/canonical/test_gateway_route_recipes.py` keeps the
`gateway_route` data and the shipped
`harness_presets_file` overlay in lock-step.

## Compatibility — the legacy config shape

A pre-lift shape parses too:

```yaml
evaluation:
  harness_adapter:
    type: "terminal_bench"
    params:
      agent_harness: "claude-code"
      agent_model: "openrouter/anthropic/claude-sonnet-4-6"
```

`RunConfig._lift_harness_adapter_params_aliases` lifts each key into
its canonical home on `models.agent.{harness,name}` at parse time,
emits one `DeprecationWarning` per key naming the new location, and
drops the legacy entry from `params`. A collision between equal values
warns once; differing values raise. Removal target: the next scheduled
major-version bump. The lift keeps existing run configs working through
one deprecation window; a new run config should write the canonical
shape.

## Related docs

- [tolokaforge_coding_harnesses/README.md](../tolokaforge_coding_harnesses/README.md) — the package landing page: how the registry resolves, the middleware proxy, credential shielding, adding a harness.
- [docs/RUNNING_TERMINAL_BENCH.md](RUNNING_TERMINAL_BENCH.md) — the end-to-end how-to. Run configs, per-harness recipes, result-bundle layout, common pitfalls.
- [external_adapters/tolokaforge-adapter-terminal-bench/README.md](../external_adapters/tolokaforge-adapter-terminal-bench/README.md) — the terminal-bench adopter. Routing options (OpenRouter / LiteLLM / per-harness split), synthesis details, per-trial materialisation.
- [docs/SECURITY.md](SECURITY.md) — the credential trust boundary in one place.
- [ADR-0033](adr/0033-external-harness-registry.md) — YAML-driven registry design.
- [ADR-0034](adr/0034-external-harness-plugin-discovery.md) — entry-point plug-in discovery.
- [ADR-0036](adr/0036-tolokaforge-coding-harnesses-split.md) — the package hoist and boundary invariant.
- [ADR-0037](adr/0037-runtime-gateway-as-harness-data.md) — gateway routing as harness data.
- [ADR-0039](adr/0039-coding-harness-adapter-agnostic.md) — the `AgentDriver` Strategy that hosts coding-harness mode.
- [ADR-0041](adr/0041-coding-harness-credential-gateway.md) — the credential-shielded LLM gateway.
