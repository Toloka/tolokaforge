# 0037. A runtime gateway is harness data, and its token dialect belongs to the runtime that provisions it

- **Status:** Accepted ([#1239](https://github.com/Toloka/tolokaforge/issues/1239))
- **Date:** 2026-08-19
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Extends:** [ADR-0033](0033-external-harness-registry.md) — `gateway_route` is
  one more operator-overridable field on the registry that ADR decided, and it
  is inert to the command assembly that ADR describes.
- **Related:** [ADR-0036](0036-tolokaforge-coding-harnesses-split.md) — the
  package split that gave a second runtime an address to read this data from,
  and the `no tolokaforge.* import` invariant that decides who expands what.
- **Related:** [`docs/LLM_LAYER.md` § "Speaking to the
  gateway"](../LLM_LAYER.md#speaking-to-the-gateway) and
  [`tolokaforge/core/llm/gateway_route.py`](../../tolokaforge/core/llm/gateway_route.py)
  — the engine's *own* "gateway route", a different thing under the same words.
  See § Two things called a gateway route.

## Context and Problem Statement

`HarnessSpec` already carries every per-harness parity knob a coding-CLI trial
needs: `config_files` for CLIs the environment block alone cannot configure,
`container_env` for static hardening, `provider_env` for the endpoint-and-key
envelope, `request_middleware` for on-the-wire repair. All four are consumed by
one path — this repo's compose synthesis, where a trial container is *built*
from a task's compose file and the spec together.

A second runtime consuming the same registry does not build the container. It
attaches to one that is already running and can only pass environment variables
in. Under that transport, every fix expressed as a `config_files` entry is
unreachable, so a harness whose CLI needs an on-disk settings file scores at a
deterministic floor there while scoring real work here — for reasons that are a
property of the transport, not of the agent.

The two runtimes also disagree about *where the provider is*. Here, a harness
reaches its provider directly (OpenRouter, Google AI Studio) or through an
operator's own gateway declared as a whole-entry overlay. There, the gateway is
the fixed point: it is what translates the CLI's wire protocol and pins the
provider, and the harness has to be told how to reach it.

Two ways to close that gap were on the table, and the shape of this repo's
registry decides between them. The registry's whole premise (ADR-0033) is that a
per-harness fix is *data*, so that adding one is a YAML edit rather than a code
change in whichever consumer noticed the problem. A per-runtime patch for each
affected harness would reintroduce exactly the address problem ADR-0033 removed,
one consumer at a time.

The hard part is not the schema. It is that a gateway route's values necessarily
carry **tokens nobody in this repo expands** — the gateway's URL, the gateway's
credential, the model alias the gateway knows a model under — and that those
tokens must be expanded in a specific order by a consumer that cannot ask this
repo what the order is. An undocumented dialect is a shim shipping a literal
`${secret:LITELLM_API_KEY}` into a container and collecting a 401 at the
gateway: the exact failure class this decision exists to remove.

## Decision Drivers

- One general capability rather than one patch per affected harness — the
  ADR-0033 premise, applied to a second consumer.
- A consuming runtime must be able to provision a route **without importing the
  engine**, which the package-boundary invariant (ADR-0036) forbids it from
  requiring.
- A token whose expander and expansion *order* are unstated is a silent
  wrong-value bug, not a loud one. The contract has to name both.
- A resolved credential must reach a container without touching host disk, an
  argument list, or an environment block — and the transport that carries it has
  to be replaceable, since `docker exec` is not every runtime's answer.
- `harness_command` must not change. A field a second runtime reads is dead
  weight at worst; a field this assembler starts reading is a routing change no
  run config asked for.
- Secrets stay with the process that owns a `SecretManager` (AGENTS.md §
  Secrets), which is not necessarily the process that provisions the container.

## Considered Options

1. **Per-harness patches in each consuming runtime** — each runtime learns the
   four affected CLIs and hard-codes their gateway settings.
2. **A gateway catalog plus an optional per-harness route, as registry data** —
   the route reuses the `config_files` / `container_env` / `provider_env` shapes,
   and this repo stores its tokens opaque.
3. **Option 2 plus a `${gateway.*}` renderer inside `_registry.py`** — this
   package resolves the gateway's URL and credential itself and hands consumers
   finished values.

## Decision

We will adopt **Option 2**.

Option 1 fails the driver it is measured against: the fix stops being data, and
the next runtime repeats the work. Option 3 fails a harder one — resolving a
gateway means reading a URL and a credential out of a secret backend, which
makes this package a secret consumer. It is deliberately not one: it imports no
engine (ADR-0036), so it has no `SecretManager`, and giving it one would put
credential resolution in a wheel whose whole selling point is that a runtime can
read harness data without installing the engine. This package stores the tokens
and publishes the order; whoever holds the secrets does the expanding.

### The catalog: `alternative_gateways` in `registry_meta.yaml`

```yaml
alternative_gateways:
  toloka_litellm:
    base_url_env: "LITELLM_BASE_URL"
    credential_env: "LITELLM_API_KEY"
    supports: ["protocol_translation", "provider_pinning"]
```

A `RuntimeGateway` records **names, not values**: which variable holds the
gateway's URL, which holds its credential. Where a gateway lives is a deployment
fact and a credential-adjacent one, so it reaches a run through the same seam a
credential does and never enters shipped registry data. `supports` is a
free-form capability tag list — this package defines no vocabulary and reads no
tag. Nothing here reads any of the three fields; the catalog exists so a route
names one endpoint by key, and so the runtime that *does* resolve the pair reads
the same names the harness data shipped with.

`ALTERNATIVE_GATEWAYS` is re-exported from the package root beside
`PROVIDER_ENV_KEYS`.

### The route: `HarnessSpec.gateway_route`

```yaml
gateway_route:
  gateway: "toloka_litellm"      # key into alternative_gateways
  passthrough_path: "/gemini"    # "" or a leading-slash path
  model_alias_pattern: "{model}-vendor-pinned"
  config_files: {}               # container path -> LITERAL content
  container_env: {}
  provider_env: {}
```

The three maps carry the names the default path already uses, so a runtime that
can provision a harness at all reuses its plumbing instead of growing a second
one. `gateway_route` is `None` by default and additive: every existing overlay,
plug-in bundle and run config loads unchanged.

`gateway_route` may coexist with `request_middleware` on the same harness. The
`config_files`-and-`request_middleware` exclusion (ADR-0033) governs the
*default* path, where a config template renders from the pre-rewrite upstream
URL and would bypass the proxy. A run taking the gateway route gets provider
pinning from the gateway's own alias and runs no proxy at all, so the two never
apply at once and no cross-validator is needed.

### Two things called a gateway route

This repo already uses the words. `docs/LLM_LAYER.md` § "Speaking to the
gateway" and `tolokaforge/core/llm/gateway_route.py` call a **gateway route**
*the name a model answers to on the gateway* — resolved at client construction
from `GET {base}/models`, with three documented outcomes for catalog-hit,
catalog-miss and catalog-unreadable. That is the engine's LLM loop talking to a
gateway on its own behalf.

`HarnessSpec.gateway_route` is the CLI-side recipe for *reaching* a gateway:
which catalog endpoint, which passthrough path, which files and environment a
harness CLI needs to speak to it. Same word, different scope, and neither reads
the other. The overlap is one field — `model_alias_pattern` answers the same
question ("under what name does the gateway know this model?") with a
hand-authored static pattern rather than a catalog lookup, because the resolver
lives in the engine that ADR-0036 forbids this package from importing, and
because the consuming runtime holds a harness spec and a model name at
provisioning time, before any client exists to query a catalog with. A pattern
that goes stale fails loud at the gateway; the two mechanisms answer to
different owners and are not expected to converge.

### Every rule is checked at load, because there is no later

`CodingHarnessDriver` (`tolokaforge/core/drivers/coding_harness.py`) reads
`gateway_route` when a run config's `models.agent.gateway_route` names one of
`ALTERNATIVE_GATEWAYS`: the driver resolves the four ADR-0037 token classes
against the operator's secrets, writes `config_files` into the trial container
verbatim, applies `model_alias_pattern` to the effective model, and skips the
shielded sidecar (the two paths are mutually exclusive). Absent that field —
the default across the shipped example configs — the driver stays on the
`credential_gateway` path, and this section's premise still holds: nothing
downstream of the registry re-checks the route. The default path can afford
to check `provider_env` keys downstream — the adapter does, over the effective
envelope, as it resolves a trial. The gateway path's driver consumption is
seam-anchored at trial attach time, so a rule that is not checked at load
becomes a runtime error the operator sees from far away. These rules
therefore fire at registry-load time, each naming the offending key or path:

| Rule | Model |
|---|---|
| `gateway` is a key of `ALTERNATIVE_GATEWAYS`; the error names the value, the accepted set, and that the catalog is shipped-only | `HarnessSpec` |
| `provider_env` keys ⊆ `PROVIDER_ENV_KEYS` | `GatewayRoute` |
| `container_env` holds no `PROVIDER_ENV_KEYS` key and no value containing `$` | `GatewayRoute` |
| `config_files` paths are absolute or `$`-rooted | `GatewayRoute` |
| `config_files` contents carry no `{{` or `{%` | `GatewayRoute` |
| `passthrough_path` is `""` or starts with `/` | `GatewayRoute` |
| `model_alias_pattern`, when set, contains `{model}` — without it every model in a matrix renders to one alias, silently | `GatewayRoute` |
| `base_url_env` / `credential_env` non-blank; `supports` entries non-blank and unique | `RuntimeGateway` |
| `alternative_gateways` keys non-blank and unpadded — a padded key files a gateway under a name no route can name, while the refusal lists that padded key as accepted | `_RegistryMeta` |

**The asymmetry with the default path is deliberate.** `HarnessSpec.provider_env`
has no load-time allow-list check — its keys are validated by the adapter that
consumes them. Widening the default path's validation to match is a separate
change with its own blast radius (operator overlays that load today would start
failing at load rather than at trial setup) and is not in scope here.

### `gateway_route.config_files` values are literals; `HarnessSpec.config_files` values are templates

Same field name, same `dict[str, str]` type, **inverted** content contract.

The default path's values are Jinja templates rendered against a deliberately
closed four-name vocabulary (`model`, `provider`, `base_url`, `api_key_env`) at
command-assembly time, in this repo. The gateway path's values are literal file
content, shipped verbatim into a container by a runtime that renders nothing. A
template pasted into the gateway map would reach the CLI unexpanded and the CLI
would read `{{ model }}` as its model name — which is why the validator refuses
`{{` and `{%` there rather than trusting the author to notice.

### The token table: who expands what, and when

The step numbers are part of the contract, not presentation. `${gateway.*}` must
expand **before** `${secret:NAME}`, because the gateway-derived URL is what a
secret reference is concatenated onto.

| Token | Appears in | Expanded by | When |
|---|---|---|---|
| `{{ … }}` / `{% … %}` (Jinja, four-name vocabulary) | `HarnessSpec.config_files` **values** | **this package**, `_TEMPLATES.from_string(...).render(...)` | command-assembly time, in-repo |
| `${HOME}` / `${CONFIG_HOME}` | `HarnessSpec.config_files` **keys** and `gateway_route.config_files` **keys** | **this package's `PathResolver`** on the default path; **the consuming runtime, calling the same exported `PathResolver`**, on the gateway path | **step 0 — before anything is written** |
| `${gateway.base_url}` | `gateway_route.provider_env` values | **the consuming runtime** | **step 1**; resolved from `ALTERNATIVE_GATEWAYS[gateway].base_url_env` |
| `${gateway.passthrough_path}` | `gateway_route.provider_env` values | **the consuming runtime** | **step 1**; resolved from `gateway_route.passthrough_path` |
| `${secret:NAME}` | `gateway_route.provider_env` values | **the consuming runtime** — see § Who owns the secret expander | **step 2**, strictly after `${gateway.*}` |
| `{model}` | `gateway_route.model_alias_pattern` | **the consuming runtime** | **step 3**; delivered through the harness's `env_model_vars`, never through `provider_env` |

**Step 0 is the one with a silent failure mode.** A gateway `config_files` key
like `${HOME}/.gemini/settings.json` is resolved on the default path by the run's
`PathResolver` (`LinuxRootResolver` maps `${HOME}` → `/root`, `${CONFIG_HOME}` →
`/root/.config`), and the resulting path is written through a double-quoted
`printf` that the container's own shell would expand anyway. A runtime writing
that file into an already-running container passes the path as *data*, so
nothing expands a leftover construct: the file lands in a literal directory named
`${HOME}` and the CLI never reads it. No crash, no log line. `PathResolver`,
`LinuxRootResolver`, `DEFAULT_PATH_RESOLVER` and `PATH_CONSTRUCT_PATTERN` are all
public API precisely so a consuming runtime reuses this package's resolver rather
than re-deriving its vocabulary.

`LinuxRootResolver` *defers* an unknown construct verbatim, which is what keeps
`${CODEX_HOME:-$HOME/.codex}` in the hands of the container's own shell on the
default path. A consuming runtime must preserve that deferral — and it follows
that **a harness whose default-path config key relies on shell deferral cannot
reuse that key in a `gateway_route`**: a route needs a key that resolves fully,
because on that path there is no shell left to finish the job.

**Step 3 does not go through `provider_env`.** A rendered model alias is a model
name, and `PROVIDER_ENV_KEYS` is a closed allow-list of provider *credential and
endpoint* names. The field that already answers "how does this CLI receive its
model name" is `HarnessSpec.env_model_vars`, and that is where a consuming
runtime delivers the alias. Routing it through `provider_env` would widen a
deliberately closed surface for the wrong reason.

### Who owns the `${secret:NAME}` expander

The `${secret:NAME}` *vocabulary* is this repo's. On the gateway path the
*expander* is not: `expand_secret_refs` lives in the engine's secrets package,
which is exactly what a runtime reading this registry is not required to install
(ADR-0036). Nothing in this repo will ever expand `gateway_route.provider_env`.

Three options were available to a consuming runtime: (a) depend on the engine's
secrets package, (b) re-implement the dialect, (c) receive already-resolved
values from its caller. **We recommend (c)**: it keeps the engine dependency out
of the consumer and leaves resolution in the process that already owns a
`SecretManager`, which AGENTS.md requires be the only reader of a secret.

### The default path's provisioning order is not reusable as-is

The adapter's provider-env resolution refuses any *resolved* value containing a
`$`, a `\n` or a `\r`, and every gateway recipe's base-URL value depends on
`${gateway.*}` surviving into that value — so a consumer copying the default
order raises on all of them. Why the check exists matters more than the check:
on the default path each value becomes one line of a per-trial compose `.env`,
where a `$` starts an interpolation and a newline splits the line. A consuming
runtime with a different transport inherits the *concern* — a value that cannot
survive its transport intact must be refused where the offending key can be
named — and applies its own check **after step 3**, not this one.

### The injection seam: `ContainerFileInjector`

Data alone does not close the gap. A `config_files` map is unreachable to a
runtime that attaches to a container someone else started, because its only
other channel is the environment block and an environment block cannot carry a
file. `tolokaforge_coding_harnesses.container_injection` ships the missing one:

```python
@dataclass(frozen=True)
class FileSpec:
    container_path: str            # absolute AND already PathResolver-resolved
    content: str = field(repr=False)
    mode: int = 0o600

class ContainerFileInjector(Protocol):
    def inject(self, container: str, files: Iterable[FileSpec]) -> None: ...

class DockerExecInjector:                       # the shipped implementation
    def __init__(self, docker_binary: str = "docker", timeout_s: float = 30.0) -> None: ...

class ContainerInjectionError(RuntimeError):    # container, container_path, returncode, stderr
    ...
```

**Why a Protocol and not just the docker class.** The transport is the part that
varies: a cluster-hosted run reaches its container through `kubectl exec`, and
that implementation is three lines against the same contract. Naming the seam
now is cheaper than discovering it later from a second consumer's fork
(AGENTS.md Core Rule 7).

**Why stdin only.** `inject` is handed a resolved credential. Passing content as
an argument puts it in `ps` output on the host *and* inside the container; an
environment variable puts it in `/proc/<pid>/environ`; a temp file puts it on
host disk with a lifetime nobody owns. Content therefore reaches the container
on stdin and nowhere else, and `FileSpec.content` carries `repr=False` so a
generated `__repr__` cannot print it into the traceback a run bundle captures.

**Why one exec per file.** A batched write cannot tell the caller *which* path
failed, and the failing path is the whole content of a useful error. A non-zero
exec raises `ContainerInjectionError` naming the container, the path and the
container's own stderr — no partial-success return value, no
logging-and-continuing (Core Rule 1). Each exec is bounded (`timeout_s`,
default 30s) and a breach raises the same error with `returncode=None`: an
unresponsive daemon writes nothing to the captured streams, so an unbounded call
is a provisioning step that hangs with nothing to read, and no int stands in for
a status that never arrived — a killed process already reports itself as a
negative code. A second transport owes the caller the same bound.

**Why a container-side `sh -c` is sanctioned and a host-side `shell=True` is
not.** These are different shells. Parent-directory creation, the mode, and the
write have to share one exec, or a missing parent silently truncates instead of
failing that file — and `mkdir -p` + redirect + `chmod` is not expressible as a
single non-shell exec. So the container gets a shell, running a **fixed literal
script** that receives the path and mode as `$1` / `$2`. What is forbidden is
*building* that script from `FileSpec` data: `sh -c ("cat > " + path)` is a
command-injection hole, and the unit tier asserts that the argv for an
injection-shaped path differs from the argv for a benign one *only in the path
element itself*. The host `subprocess` call passes a list and never sets
`shell=True`.

The write order inside that script — `touch`, then `chmod`, then `cat` — is a
security property, not a style choice: content only ever exists on disk under
the requested mode, where a trailing `chmod` would leave a credential
world-readable for the length of the copy.

**The injector expands nothing.** `FileSpec.container_path` arrives already
resolved (step 0 of the token table). It reaches the container as a quoted
positional, which writes a literal directory named `${HOME}` rather than
failing — a silent wrong-path write, which is why the resolution is the
caller's contract rather than a courtesy the injector might perform.

## Consequences

### Positive

- A harness needing an on-disk config file to reach a gateway is expressible as
  data, once, for every consumer of the registry.
- The expansion order is written down where both sides can read it, instead of
  being inferred from two YAML examples.
- `harness_command` is byte-identical with and without a route, locked by a test
  in `test_harness_command.py`. A future edit that starts consuming
  `gateway_route` on this side breaks that test rather than quietly re-routing
  every trial.
- The credential path is narrow enough to test. `FileSpec.content` reaches a
  container on one pipe, and the tests assert the negative — the value is in no
  argv element and in no `repr` — rather than only that the happy path works.

### Negative / Trade-offs

- **The gateway catalog is shipped-only.** `registry_meta.yaml` has no overlay
  layer and no plug-in layer — that is what keeps it outside both fingerprint
  digests — while the `gateway_route.gateway` validator fires on every layer,
  including operator overlays and plug-in bundles. So an operator cannot register
  their own gateway name; declaring one is a PR against that file.

  We accept it for round one, and the reason is stronger than "smallest fix":
  the operator escape hatch already exists one layer down. An operator running
  their own gateway today overlays the whole harness entry — `config_files`,
  `container_env`, `provider_env` directly, as
  `examples/terminal_bench/gemini_litellm_overlay.yaml` does — and names no
  gateway at all. `gateway_route` exists for a cross-repo runtime whose gateway
  *is* the shipped one. Nobody is blocked, and the closed set buys a load-time
  typo check for the one consumer that can check nothing itself.

  Rejected alternatives: dropping the closed-set check for overlay-loaded specs
  (two-tier validation on one field is subtler than the problem it solves), and
  making `alternative_gateways` layerable (it would move `registry_meta.yaml`
  inside the fingerprint contract, a deliberate property this decision has no
  business changing).

  **Revisit trigger:** the first operator who needs a `gateway_route` against a
  self-hosted gateway. At that point making the catalog layerable is the right
  answer, and it is its own ADR.
- **Nothing in this repo consumes `gateway_route` or `ContainerFileInjector`.**
  Adding a field to `HarnessSpec` is not free — it moves
  `HarnessFingerprint.shipped_sha256` and `resolved_sha256` for every run, and
  it is one more field an operator reading the spec has to decide is not for
  them. If no consumer lands, this schema and this module are dead weight and
  should be reverted rather than left to rot.
- **The token contract is enforced by prose on one side.** This repo can refuse
  a template in a literal map and a relative config path; it cannot enforce that
  a consumer expands `${gateway.*}` before `${secret:NAME}`, or that it resolves
  `${HOME}` at step 0. That lock belongs to the consumer's own test suite.
- **The injector's real-container proof runs on fewer lanes than the PR check.**
  The Docker-gated test lives under `tests/integration/coding_harnesses/`, and
  the integration lane is push / nightly / release-gate scoped. Every PR runs
  the stand-in-`docker` unit tier, which covers argv leakage and the
  anti-injection structure; a green PR check is not evidence the container-side
  quoting was exercised.

### Follow-ups

- **A consuming shim in the second runtime** — it implements steps 0–3 of the
  token table and calls `DockerExecInjector` (or its own
  `ContainerFileInjector`) with resolved `FileSpec`s. Until one lands, this
  schema and `container_injection` have no consumer, which the trade-off above
  says is grounds for reverting rather than leaving them to rot.
- **A `moonshotai`-pinned alias on the shipped gateway** — `kimi-code`'s
  `model_alias_pattern` names a route the gateway has to actually serve, and
  registering it is a deployment change, not a repo one. Tracked with the
  operator who owns that gateway.
- **Split `_registry.py`** into spec / registry / command modules — still open
  from ADR-0036, and this decision added a third model to the file.

## Links

- Related ADRs: [ADR-0033](0033-external-harness-registry.md),
  [ADR-0034](0034-external-harness-plugin-discovery.md),
  [ADR-0036](0036-tolokaforge-coding-harnesses-split.md),
  [ADR-0011](0011-seam-and-declaration-conventions.md) Pattern B (a `HarnessSpec`
  field addition requires this ADR and a snapshot regen).
- Related code:
  `tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/_registry.py`
  (`RuntimeGateway`, `GatewayRoute`, `HarnessSpec.gateway_route`),
  `tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/container_injection.py`
  (`FileSpec`, `ContainerFileInjector`, `DockerExecInjector`),
  `tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/data/registry_meta.yaml`.
