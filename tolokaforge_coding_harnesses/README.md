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
    CredentialGateway,           # per-harness config for the credential shield
    harness_command,             # assembles the trial's shell command
    harness_model,               # canonical model name the CLI receives
    resolve_effective_registry,  # shipped + operator overlay + plug-in bundles
    validate_harness,            # startup contract check
    HarnessFingerprint,          # records CLI version + spec on the artifact
    RuntimeGateway,              # gateway_route consumer surface (ADR-0037)
    ContainerFileInjector,       # deliver config_files into a running container
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
  files, envs and endpoints itself. `CodingHarnessDriver` also opts into
  this path when a run config sets `models.agent.gateway_route` to a name
  from `alternative_gateways`: the driver resolves the ADR-0037 tokens
  against the operator's secrets, writes `config_files` into the trial
  container verbatim, applies `model_alias_pattern` to the effective model,
  and skips the `credential_gateway` sidecar (the two paths are mutually
  exclusive). Absent that field, the driver stays on the shielded default
  path unchanged.

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

## Consumed by the engine's `CodingHarnessDriver`

Engine-side, the `AgentDriver` Strategy
([ADR-0039](../docs/adr/0039-coding-harness-adapter-agnostic.md)) owns
"how a trial runs". The orchestrator selects `CodingHarnessDriver` from
`models.agent.coding_harness`; the driver consumes the shipped
`HarnessSpec` and drives the trial via one `docker exec` of the vendor
CLI. Adapters carry no coding-harness state; they stage a per-trial
container (`stage_task`) and the driver decorates it.

This package ships the data + primitives the driver consumes. It never
imports the engine — the boundary invariant is enforced at
[`tests/unit/test_package_boundary.py`](tests/unit/test_package_boundary.py).

## Credentials — the shield

Every shipped harness carries a `credential_gateway` block on its
`HarnessSpec`:

```yaml
credential_gateway:
  upstream_url: "https://openrouter.ai/api"
  upstream_token_env_var: "OPENROUTER_API_KEY"     # SecretManager reads the real value
  upstream_auth_header: "Authorization"
  upstream_auth_template: "Bearer {token}"
  dummy_token_env_var: "ANTHROPIC_API_KEY"         # env var the CLI reads
  dummy_token_value: "sk-tolokaforge-shielded-dummy-not-a-real-key"
  base_url_env_var: "ANTHROPIC_BASE_URL"           # points at the gateway
  path_allowlist: [...]                            # what upstream paths are proxied
```

`CodingHarnessDriver` reads it at `attach()`, adds a
`tolokaforge-llm-gateway` sidecar service to the trial's compose stack
carrying the real credential, and bakes the dummy value + the
sidecar's compose-DNS URL (`http://tolokaforge-llm-gateway:8080`)
into the CLI's own container env. The CLI never sees the real
credential; the sidecar swaps in the correct auth header on every
allow-listed request. See
[ADR-0041](../docs/adr/0041-coding-harness-credential-gateway.md) and
[docs/SECURITY.md](../docs/SECURITY.md) for the threat model.

**Adding a new harness:** populate `credential_gateway` with the
vendor's real endpoint + env-var conventions. If the vendor uses a
non-standard credential protocol (OAuth, workload identity) or its
request paths are model-dynamic (see the gemini-cli exemption tracked
in [#1311](https://github.com/Toloka/tolokaforge/issues/1311)), leave
`credential_gateway: null` and add the harness name to
`UNSHIELDED_HARNESSES` in
[`tests/unit/test_credential_gateway_schema.py`](../tests/unit/test_credential_gateway_schema.py)
with a tracking issue.

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
  `AgentDriver` Strategy that consumes this package.
- [ADR-0041](../docs/adr/0041-coding-harness-credential-gateway.md) —
  the credential-shielded LLM gateway.
