# tolokaforge-coding-harnesses

Coding-harness CLI registry, installer and middleware proxy shared by every
tolokaforge runtime that drives a harness trial. A harness trial replaces the
engine's LLM turn loop with a single invocation of a vendor coding-agent CLI
inside the task container; the six ends that need to agree on the same small
set of facts — the installer, the trial exec, the fingerprint recorded on the
artifact, and any consumer promising the same recipe elsewhere — read this
package.

This package **does not import the engine**. That is the invariant that lets a
second consumer read the same registry data without inheriting an engine
version pin. `tests/unit/test_package_boundary.py` holds the line.

## Shipped harnesses

Six vendor coding-agent CLIs are shipped in
[`src/tolokaforge_coding_harnesses/data/harnesses.yaml`](src/tolokaforge_coding_harnesses/data/harnesses.yaml).
Each entry is a `HarnessSpec` — the field list, the semantics and the
extension policy live in
[ADR-0033](../docs/adr/0033-external-harness-registry.md).

| `agent_harness` | Vendor CLI | Install | Pinned |
|---|---|---|---|
| `claude-code` | `@anthropic-ai/claude-code` | npm | 2.1.233 |
| `codex` | `@openai/codex` | npm | 0.147.0 |
| `gemini-cli` | `@google/gemini-cli` | npm | 0.55.1 |
| `kimi-code` | `@moonshot-ai/kimi-code` | npm | 0.28.1 |
| `opencode` | `opencode-ai` | npm | 1.18.18 |
| `grok-build` | `https://x.ai/cli/install.sh` | curl-bash | 0.2.91 |

Provider envelopes, model-name conventions and per-harness quirks live in the
YAML alongside each entry. Every version pin is deliberate — bumping one lands
with an ADR entry so a scored-run replay does not silently drift.

The installer that provisions each CLI at trial-image build time is
[`src/tolokaforge_coding_harnesses/install-harness.sh`](src/tolokaforge_coding_harnesses/install-harness.sh)
— a POSIX shell script that auto-detects apt / apk and installs Node 20 LTS,
python3+pip, or curl+ca-certificates on demand.

## Public surface

```python
from tolokaforge_coding_harnesses import (
    HARNESSES,                   # dict[str, HarnessSpec] — the shipped registry
    HarnessSpec,                 # pydantic model — one entry
    harness_command,             # assembles the trial's shell command
    harness_model,               # canonical model name the CLI receives
    resolve_effective_registry,  # shipped + operator overlay + plug-in bundles
    validate_harness,            # startup contract check
    HarnessFingerprint,          # records CLI version + spec on the artifact
    RuntimeGateway,              # gateway_route consumer surface (ADR-0037)
    ContainerFileInjector,       # deliver config_files into a running container
    SkillDelivery,               # per-task skill bundle delivery
    CodingHarnessAdapterMixin,   # adapter-side pattern: registry resolve, command,
                                 # metadata, tool schema, test_execution grading,
                                 # standalone install-script Dockerfile layer
)
```

Full signatures live in
[`src/tolokaforge_coding_harnesses/_registry.py`](src/tolokaforge_coding_harnesses/_registry.py).

`__all__` in [`src/tolokaforge_coding_harnesses/__init__.py`](src/tolokaforge_coding_harnesses/__init__.py)
is the flat surface consumers import from; nothing else is public.

## How the registry resolves

Three layers compose at import time, exactly in this order:

1. **Shipped catalog.** [`data/harnesses.yaml`](src/tolokaforge_coding_harnesses/data/harnesses.yaml)
   is the base. Every entry is a `HarnessSpec`; every value has a reason
   recorded next to it.
2. **Operator overlay.** A run may name a `harness_presets_file` — a YAML with
   the same shape that whole-replaces an entry, never partial-merges. The
   Gemini→LiteLLM path
   ([`examples/terminal_bench/gemini_litellm_overlay.yaml`](../examples/terminal_bench/gemini_litellm_overlay.yaml))
   is the worked example.
3. **Plug-in bundles.** Any Python distribution registered under the
   `HARNESS_REGISTRY_ENTRY_POINT_GROUP` entry-point group contributes one
   `PluginBundle`. Discovery is automatic on install — an external
   harness ships as a separate distribution and needs no edit here.
   [ADR-0034](../docs/adr/0034-external-harness-plugin-discovery.md) is the
   contract; the composition rules and duplicate-registration behaviour are in
   [ADR-0033 § "Registry composition"](../docs/adr/0033-external-harness-registry.md).

`resolve_effective_registry()` runs the three layers, validates every
resulting `HarnessSpec`, and returns a `ResolvedHarnessRegistry`.

## What each field does

Rather than reproduce the field list here, read the two references that stay
in sync with the code:

- **`HarnessSpec` field list + semantics** —
  [ADR-0033 § "Harness spec schema"](../docs/adr/0033-external-harness-registry.md).
- **Why each shipped value is what it is** — the inline comments next to each
  entry in [`data/harnesses.yaml`](src/tolokaforge_coding_harnesses/data/harnesses.yaml).
  Every non-obvious flag records the failure mode it prevents (permission
  bypass, root-under-sandbox, config-file precedence, empty-response fallback
  behaviour, and so on).

## Middleware proxy — Kimi K2.7 provider pinning

Some harnesses need a body-injection knob no CLI flag exposes. `kimi-code`
declares a `request_middleware`: at trial start the runtime boots the stdlib
proxy in [`middleware_proxy.py`](src/tolokaforge_coding_harnesses/middleware_proxy.py)
on `127.0.0.1:8899` inside the container and rewrites `KIMI_MODEL_BASE_URL` to
point at it. The proxy injects
`{"provider": {"only": ["moonshotai"], "allow_fallbacks": false}}` into every
`/chat/completions` body so `moonshotai/kimi-k2.7-code` reaches Moonshot AI
first-party routing on OpenRouter (14 fanout providers, mostly INT4/FP4, would
otherwise return empty completions and fall to the CLI's baseline score).

The proxy is stdlib only (~200 LOC), boots inside the harness-command preamble
and is unaware to the CLI. Nothing to configure — it applies automatically
when `agent_harness: kimi-code`.

## Gateway routing — the two paths, one recipe

The shipped defaults route each CLI at OpenRouter (or, for `gemini-cli`,
directly at Google). To route through a team-hosted LiteLLM gateway instead,
two paths ship with the exact same recipe:

- **`harness_presets_file` overlay** — what a tolokaforge run reads, because
  it composes the trial container from the registry.
  [`examples/terminal_bench/gemini_litellm_overlay.yaml`](../examples/terminal_bench/gemini_litellm_overlay.yaml)
  whole-replaces the `gemini-cli` entry.
- **`gateway_route` on the spec** — what an external runtime reads when it
  attaches to a container it did not build and has to provision the same
  files, envs and endpoints itself. Nothing in this repo consumes it.

The two are siblings with different audiences.
[ADR-0037](../docs/adr/0037-runtime-gateway-as-harness-data.md) is the design
record; `tests/canonical/test_gateway_route_recipes.py` renders both and
compares the endpoint, credential reference and settings file they land — so
editing one and not the other fails CI.

`alternative_gateways` in
[`data/registry_meta.yaml`](src/tolokaforge_coding_harnesses/data/registry_meta.yaml)
names the gateways a `gateway_route` may point at.

## Adding a harness

Two shapes. Pick by whether your harness is generally useful (ships here) or
private to your organisation (out-of-tree plug-in).

- **In-tree.** Add a `HarnessSpec` entry to
  [`data/harnesses.yaml`](src/tolokaforge_coding_harnesses/data/harnesses.yaml)
  + any new provider env keys to `provider_env_keys` in
  [`data/registry_meta.yaml`](src/tolokaforge_coding_harnesses/data/registry_meta.yaml).
  If the CLI needs a new install method, extend
  [`install-harness.sh`](src/tolokaforge_coding_harnesses/install-harness.sh).
  Every registry change bumps the commit-audit snapshot at
  `tests/canonical/snapshots/harness_registry_replay/metric.json` — regenerate
  it via the dev MCP `update_canonical_snapshots` in the same PR.
- **Out-of-tree.** Ship a Python distribution that exposes a
  `PluginBundle` under the `HARNESS_REGISTRY_ENTRY_POINT_GROUP` entry-point
  group. See [ADR-0034 § "Plug-in discovery"](../docs/adr/0034-external-harness-plugin-discovery.md).
  [`src/tolokaforge_coding_harnesses/testing.py`](src/tolokaforge_coding_harnesses/testing.py)
  ships the `isolate_discovery` helper for plug-in test suites.

## Adopting the mixin

An adapter opts a task pack into coding-harness mode by inheriting
[`CodingHarnessAdapterMixin`](src/tolokaforge_coding_harnesses/adapter_support.py)
alongside `BaseAdapter`. Class shape:

```python
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge_coding_harnesses import CodingHarnessAdapterMixin


class MyAdapter(CodingHarnessAdapterMixin, BaseAdapter):
    ...
```

The mixin sets `supports_coding_harness: ClassVar[bool] = True` — the
capability flag the engine's config-validation gate reads. Adapters
that do not inherit the mixin (or override the flag to `False`) refuse
a run declaring `models.agent.harness` before any container work.

Six helpers ship on the mixin. Contracts (parameters live in the
mixin's own docstrings — read those, not this table):

| Helper | Contract |
|---|---|
| `resolve_harness_spec(agent_harness, agent_model, provider_env=None, presets_file=None, plugin_discovery=True, version_override=None)` | Resolves against the shipped catalog + operator overlay + installed plug-ins, then validates. Refuses unknown harness names and empty models. `version_override` (from `models.agent.harness_version` or the `name@version` slug on `models.agent.harness`) replaces the shipped pin on the returned spec's `version` — every downstream consumer (Dockerfile install line, artefact metadata, fingerprint) sees the override. |
| `build_harness_command(agent_harness, spec, instruction, model, provider_env=None, *, path_resolver=None)` | Assembles the `bash -c`-shaped command the trial exec runs. Threads argv, model routing, provider-env, and (when the spec declares it) the middleware-proxy preamble. |
| `emit_harness_metadata(agent_harness, spec, command, model)` | Four-key handshake the conductor branches on: `agent_harness`, `agent_harness_version`, `agent_harness_model`, `agent_harness_command`. |
| `emit_harness_tool_schema(*, service, compose_project_prefix, timeout_s, toolset="coding_harness")` | Payload for the runner's `bash` tool routed through `DockerComposeExecToolWrapper`. `timeout_s` must cover the whole trial. |
| `emit_test_execution_grading()` | Payload for the runner's `RunnerGradingConfig`. Grades by reading `/logs/verifier/reward.txt`. |
| `write_install_script_layer(context_dir, base_image, spec, middleware_proxy=False)` | Writes a standalone Dockerfile snippet + the shipped install script (and the middleware proxy when declared) into `context_dir`. Returns the Dockerfile's relative path. |

**Payload dicts, not engine types.** `emit_harness_tool_schema` and
`emit_test_execution_grading` return dicts. Adapters construct the
engine types at the call site (`ToolSchema(**payload)`,
`RunnerGradingConfig(**payload)`); pydantic v2 reconstructs nested
types (`ToolSource` from `source={…}`) transparently. This preserves
the boundary invariant — `tolokaforge_coding_harnesses/` imports no
engine module.

**Reference adopters.** The bundled
[`NativeAdapter`](../tolokaforge/adapters/native.py) drives a native
task pack via harness mode when `models.agent.harness` is set; the
out-of-tree
[`TerminalBenchAdapter`](../external_adapters/tolokaforge-adapter-terminal-bench/src/tolokaforge_adapter_terminal_bench/adapter.py)
adopts the mixin with compose synthesis wrapped around
`write_install_script_layer` from the compose context root. See
[ADR-0039](../docs/adr/0039-coding-harness-adapter-agnostic.md) for the
design record.

## Related docs

- [docs/RUNNING_TERMINAL_BENCH.md](../docs/RUNNING_TERMINAL_BENCH.md) — the
  end-to-end how-to for running a harness trial. Start here when you want to
  actually run one.
- [docs/CODING_HARNESSES.md](../docs/CODING_HARNESSES.md) — the coding-harness
  surface in context: engine-loop mode vs harness mode, per-harness quick
  reference, cross-repo composition.
- [examples/terminal_bench/](../examples/terminal_bench/) — the two shipped
  task packs (`fix-billing-holds`, `fix-airline-segmentation`) and the
  `run_harness.yaml` driver config.
- [ADR-0033](../docs/adr/0033-external-harness-registry.md) — YAML-driven
  registry design.
- [ADR-0034](../docs/adr/0034-external-harness-plugin-discovery.md) —
  entry-point plug-in discovery.
- [ADR-0036](../docs/adr/0036-tolokaforge-coding-harnesses-split.md) — the
  package hoist and boundary invariant.
- [ADR-0037](../docs/adr/0037-runtime-gateway-as-harness-data.md) — gateway
  routing as harness data.
- [ADR-0039](../docs/adr/0039-coding-harness-adapter-agnostic.md) — the
  adapter-agnostic lift and the mixin contract.
