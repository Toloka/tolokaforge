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
| Head-to-head between two CLIs on the same task pack | **Harness mode** | Change one field (`agent_harness`); everything else stays constant. |
| A model in a way another team can independently reproduce end-to-end | **Harness mode** with a shipped CLI | The CLI version is pinned in the registry; the artifact records the pin. |

## Shipped harnesses

Six vendor coding-agent CLIs ship in-tree.
[`tolokaforge_coding_harnesses/`](../tolokaforge_coding_harnesses/) is the
package; the source of truth for the catalog is
[`tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml).

| `agent_harness` | Vendor CLI | Install | Pin |
|---|---|---|---|
| `claude-code` | `@anthropic-ai/claude-code` | npm | 2.1.233 |
| `codex` | `@openai/codex` | npm | 0.147.0 |
| `gemini-cli` | `@google/gemini-cli` | npm | 0.55.1 |
| `kimi-code` | `@moonshot-ai/kimi-code` | npm | 0.28.1 |
| `opencode` | `opencode-ai` | npm | 1.18.18 |
| `grok-build` | `x.ai/cli` install script | curl-bash | 0.2.91 |

Adding a harness — in-tree or out-of-tree as a Python entry-point plug-in — is
covered in the [package README](../tolokaforge_coding_harnesses/README.md#adding-a-harness)
and [ADR-0034](adr/0034-external-harness-plugin-discovery.md).

## How the surface composes

Three moving parts, each with a clear responsibility.

- **Harness registry** — the catalog of `HarnessSpec` entries. Data-driven, no
  engine dependency (an external runtime can read the same data without
  pulling the engine in). Lives in the top-level
  [`tolokaforge_coding_harnesses/`](../tolokaforge_coding_harnesses/) workspace
  member ([ADR-0036](adr/0036-tolokaforge-coding-harnesses-split.md)).
- **Consumer adapter** — the piece that materialises the trial container,
  installs the CLI (via
  [`install-harness.sh`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/install-harness.sh)),
  writes any per-harness config files, boots the middleware proxy if the spec
  declares one, and `docker exec`s the CLI. The shipped consumer is the
  [`terminal_bench` adapter](../external_adapters/tolokaforge-adapter-terminal-bench/).
- **Task pack** — the task-specific compose stack (`docker-compose.yaml` +
  `environment/Dockerfile`) + instruction (`task.toml` / `task.yaml`) +
  verifier (`tests/test.sh` writing `/logs/verifier/reward.txt`). Shipped
  examples: [`examples/terminal_bench/fix-billing-holds/`](../examples/terminal_bench/fix-billing-holds/)
  and [`examples/terminal_bench/fix-airline-segmentation/`](../examples/terminal_bench/fix-airline-segmentation/).

The registry stays task-agnostic; the task pack stays harness-agnostic. A
harness is a fully replaceable slot on the run config, not a coupling.

## One-command demo

Run `claude-code` on the shipped `fix-billing-holds` pack:

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/terminal_bench/run_harness.yaml
```

The [`run_harness.yaml`](../examples/terminal_bench/run_harness.yaml) driver
config carries `agent_harness: claude-code` and `agent_model:
openrouter/anthropic/claude-sonnet-4-6`; swap either field to matrix over
harnesses or models. Per-trial artifacts land under
`results/terminal_bench/fix-billing-holds-claude-code/trials/fix-billing-holds/<N>/`.

Prerequisites (Docker daemon, `uv`, `.env` with a provider key), the switch
between engine-loop and harness mode, per-harness recipes (Kimi K2.7
middleware, opencode routing, Gemini LiteLLM gateway) and result-bundle layout
all live in the end-to-end guide:
[**docs/RUNNING_TERMINAL_BENCH.md**](RUNNING_TERMINAL_BENCH.md).

## The one field that switches modes

```yaml
evaluation:
  harness_adapter:
    type: "terminal_bench"
    params:
      agent_harness: "claude-code"     # any of the six above, or "engine-loop"
      agent_model:   "openrouter/anthropic/claude-sonnet-5"
```

`agent_harness: engine-loop` (or leaving the field off) keeps the engine's
turn loop; any accepted harness name layers the vendor CLI into the trial
image instead. `agent_model` is required in harness mode — the adapter reads
it directly because the run's `models.agent` config is not visible on this
code path.

## Per-harness quick reference

Rows here name the shape you'll write in a run config; per-CLI quirks
(permission-mode flags, model-name conventions, provider-env envelopes) live
next to each entry in the shipped
[`harnesses.yaml`](../tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/harnesses.yaml)
with the reason recorded inline.

| Harness | Example `agent_model` | Notes |
|---|---|---|
| `claude-code` | `openrouter/anthropic/claude-sonnet-5` | Anthropic-compat via OpenRouter. |
| `codex` | `openrouter/openai/gpt-5.6-sol` | OpenAI-compat via OpenRouter; writes `~/.codex/config.toml` + `auth.json`. |
| `gemini-cli` | `openrouter/google/gemini-3.6-flash` | Shipped default routes at Google directly. LiteLLM gateway path via `harness_presets_file` — see [RUNNING_TERMINAL_BENCH.md § Gemini CLI](RUNNING_TERMINAL_BENCH.md#recipe--gemini-cli-litellm-gateway). |
| `kimi-code` | `openrouter/moonshotai/kimi-k3` | Also `kimi-k2.7-code` — the shipped `request_middleware` pins Moonshot AI first-party routing on OpenRouter automatically. |
| `opencode` | `openrouter/meta/muse-glimmer-30b` | `openrouter/*` prefix is preserved; the shipped config declares a matching `openrouter` provider block. |
| `grok-build` | `openrouter/x-ai/grok-4.5` | Auto-configures `~/.grok/config.toml` for OpenRouter. |

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

## Related docs

- [tolokaforge_coding_harnesses/README.md](../tolokaforge_coding_harnesses/README.md) — the package landing page: how the registry resolves, the middleware proxy, adding a harness.
- [docs/RUNNING_TERMINAL_BENCH.md](RUNNING_TERMINAL_BENCH.md) — the end-to-end how-to. Run configs, per-harness recipes, result-bundle layout, common pitfalls.
- [external_adapters/tolokaforge-adapter-terminal-bench/README.md](../external_adapters/tolokaforge-adapter-terminal-bench/README.md) — the consumer adapter. Routing options (OpenRouter / LiteLLM / per-harness split), synthesis details, per-trial materialisation.
- [ADR-0033](adr/0033-external-harness-registry.md) — YAML-driven registry design.
- [ADR-0034](adr/0034-external-harness-plugin-discovery.md) — entry-point plug-in discovery.
- [ADR-0036](adr/0036-tolokaforge-coding-harnesses-split.md) — the package hoist and boundary invariant.
- [ADR-0037](adr/0037-runtime-gateway-as-harness-data.md) — gateway routing as harness data.
