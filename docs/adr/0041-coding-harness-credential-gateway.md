# 0041. Credential-shielded LLM gateway for coding-harness mode

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Related:**
  - [ADR-0022](0022-runtime-independence.md) — the runner-independence arc
    the cluster-mode variant of this design will land alongside.
  - [ADR-0025](0025-runner-wheel-split.md) — the frozen Open-agent-loop
    row that the cluster-mode extension depends on.
  - [ADR-0033](0033-external-harness-registry.md) — the `HarnessSpec`
    field list this ADR extends with `credential_gateway`.
  - [ADR-0038](0038-grader-detachment.md) — the pattern (protocol seam,
    two topologies) this ADR reuses for the local-vs-cluster split.
  - [ADR-0039](0039-coding-harness-adapter-agnostic.md) — the AgentDriver
    Strategy on which the gateway lifecycle attaches.
  - [ADR-0040](0040-standalone-grader.md) — the grading-substrate pattern
    the gateway launcher pattern mirrors.
  - Closes [#1042](https://github.com/Toloka/tolokaforge/issues/1042).
    Sibling of [#1115](https://github.com/Toloka/tolokaforge/issues/1115)
    (runner-service credential policy).
  - Follow-up: [#1311](https://github.com/Toloka/tolokaforge/issues/1311)
    (shield gemini-cli — config-file pin + prefix path allow-list).

## Context

Coding-harness mode replaces the engine's own LLM turn loop with a
single invocation of a vendor coding-agent CLI inside the trial
container ([ADR-0039](0039-coding-harness-adapter-agnostic.md)). The
CLI *is* the LLM client — it originates requests to OpenRouter /
Anthropic / OpenAI from inside the container that the trial's model
also has code-execution control over.

Before this ADR, the driver resolved the real LLM API key via
`SecretManager` and baked it plaintext into the trial container's
compose `environment:` block. The model reads it in one command:

```bash
$ printenv ANTHROPIC_API_KEY
sk-or-v1-<REDACTED-64-hex-chars>
```

Verified against a real smoke of `examples/terminal_bench/run_harness.yaml`.
This contradicts the discipline `tolokaforge/secrets/manager.py` +
`AGENTS.md` § "Secrets — single abstraction" enforces everywhere else
in the codebase: no `os.environ.get` for credentials, no baking into
images / build args / mounts / image tags. Engine-loop mode was already
compliant — `LLMClient` runs on the orchestrator host and credentials
never enter the trial container.

The user's forward-looking constraint changes the design: in a future
where the orchestrator, runner, and grader run on independent hosts
(cluster / K8s / Modal — see the `0.12.0` and `1.0.0+` rows of
[`ROADMAP.md`](../ROADMAP.md)), the runner must complete a trial
without the orchestrator alive. A credential shield that assumes the
orchestrator process holds the real key bakes in the coupling we've
been dismantling. The right shape keeps the shield concept but binds
its lifecycle to whichever component owns the trial at any moment.

### Three prior arts

- **Formal.ai / mitmproxy pattern** — the CLI sees a dummy
  `ANTHROPIC_API_KEY` and a base URL pointing at a proxy that injects
  the real credential before forwarding upstream. Third-party,
  documented against Claude Code's `httpProxyPort` setting.
- **Vault Agent Injector on Kubernetes** — a dedicated sidecar
  container per pod authenticates with Vault using the pod's service
  account token, renders secrets to a shared memory volume, handles
  renewal and revocation. Agent CLI reads a file, not an env var. This
  is what "cluster mode" should look like eventually.
- **`feat/native-domain-factory` prototype** — an unmerged tolokaforge
  branch's `BYOH-001..070` commits shipped an HTTP CONNECT forward
  proxy with `cap_drop=[ALL]`, `no_new_privileges`, per-trial
  bearer-token auth, and `SecretManager`-based credential provisioning.
  194 commits behind main; can't be textually merged; design intent
  survives.

None ship in tolokaforge today.

## Decision

**Ship the LLM gateway as a compose sidecar service inside every
shielded trial's stack.** The `CodingHarnessDriver` adds a
`tolokaforge-llm-gateway` service to the trial's compose file — the
shipped `tolokaforge-runner:local` image running
`python -m tolokaforge.runner.llm_gateway_serve` on port 8080. The
sidecar holds the real provider credential (resolved once at
bootstrap via `SecretManager`); the CLI's own container sees only a
dummy token and a base URL pointing at the sidecar over docker's own
compose DNS.

**Two-layer split** — the agent loop pluggability from earlier
CodingHarnessDriver planning remains:

```
Layer 2: THE AGENT LOOP     ← claude-code, codex, Harbor, custom driver
                              (varies per driver; pluggable)
Layer 1: THE GATEWAY        ← _GatewayHTTPServer (reverse proxy) +
                              llm_gateway_serve (sidecar entrypoint)
                              (one implementation, docker-networked)
```

Adding a new agent loop is a new `AgentDriver` implementation + new
`HarnessSpec` entry (Layer 2). Adding a new credential protocol (OAuth,
workload identity) is a new protocol adapter inside `_GatewayHTTPServer`
(Layer 1). The docker compose network topology handles what an earlier
plan tried to split into a separate "launcher" layer.

### The shipping shape

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Trial compose stack (host or K8s Job, same shape either way)           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Orchestrator process (host)                                            │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  SecretManager: DotEnvProvider + EnvProvider                     │   │
│  │  CodingHarnessDriver                                             │   │
│  │  └─ at attach(): resolves real token, stashes for compose write  │   │
│  │  └─ at compose write: adds tolokaforge-llm-gateway service       │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  Compose stack (netpolicy_internal + netpolicy_edge networks)           │
│                                                                         │
│  ┌── main (CLI container) ─────────────────────────────────────────┐    │
│  │  ANTHROPIC_API_KEY  = "sk-tolokaforge-shielded-dummy…"          │    │
│  │  ANTHROPIC_BASE_URL = "http://tolokaforge-llm-gateway:8080"     │    │
│  │  NO_PROXY           = "tolokaforge-llm-gateway"                 │    │
│  │  depends_on: tolokaforge-llm-gateway (service_healthy)          │    │
│  │  claude-code CLI ───────────────┐                               │    │
│  └────────────────────────────────┬┴───────────────────────────────┘    │
│                                   │                                     │
│                       docker DNS  │  (netpolicy_internal, internal:true)│
│                                   │                                     │
│  ┌── tolokaforge-llm-gateway ──── ▼─────────────────────────────────┐   │
│  │  image: tolokaforge-runner:local                                 │   │
│  │  command: python -m tolokaforge.runner.llm_gateway_serve         │   │
│  │  env: TF_GATEWAY_UPSTREAM_TOKEN=<real key> (bootstrap only —     │   │
│  │       SecretManager takes over; register_runtime_secret +        │   │
│  │       install_global_redactor put it on the scrub set)           │   │
│  │  bridged onto BOTH netpolicy_internal + netpolicy_edge           │   │
│  │  ────────────────────────────────┐                               │   │
│  └───────────────────────────────── │ ──────────────────────────────┘   │
│                                     │  (netpolicy_edge, has egress)     │
│                                     ▼                                   │
│                            https://openrouter.ai                        │
└─────────────────────────────────────────────────────────────────────────┘
```

Two `EnvironmentManifest` fields make this work under any pack policy:

- **`bridged_services`** — the netpolicy enforcement attaches every
  named service to BOTH `netpolicy_internal` (isolated, CLI-reachable)
  and `netpolicy_edge` (has egress), same treatment as `runner_service`.
  The driver sets it to `{"tolokaforge-llm-gateway"}` so the sidecar
  reaches the upstream even when the pack declares `no_internet`.
- **`stripped_container_secrets`** — `inject_runner_credentials` omits
  these keys from the runner container's `TOLOKAFORGE_SECRETS_JSON`
  payload. The driver sets it to `{spec.credential_gateway.upstream_token_env_var}`
  so the credential lives in exactly one service in the trial stack
  (the sidecar) rather than being duplicated into the runner too.

### K8s / remote-runner (accommodated by construction)

Nothing in the sidecar shape depends on the orchestrator being alive.
A K8s Job that materialises this same compose stack as pods gets the
same shield — the sidecar reads its bootstrap env once, then the trial
runs to completion on the runner-pod side. Bootstrap-time credential
provisioning is the extension seam: a new `SecretProvider` (Vault, CSI,
workload identity) plugs into how `TF_GATEWAY_UPSTREAM_TOKEN` gets into
the sidecar's env, without changing the sidecar itself.

### Alternate agent-loop shape (accommodated by construction)

```
Harbor as an embedded library in a custom driver:

  HarborDriver (peer of CodingHarnessDriver, implements AgentDriver)
  └─ decorate_task_description
      ├─ container gets: dummy env, gateway URL
      └─ decorated wire schema

  Same tolokaforge-llm-gateway sidecar. Same compose shape. No adapter edit.
```

## Consequences

**What changes today:**

- Every `tolokaforge run` operator gets credential-shielded coding-harness
  runs by default. No config change required.
- The CLI's own compose service `environment:` never contains a real
  LLM provider credential. Verified against
  `examples/terminal_bench/run_harness.yaml` (score 0.833) and
  `examples/native/coding_harness/` under `network_policy=no_internet`
  (score 1.0, latency 16.4s, network policy preserved unchanged): the
  resolved-secret string does not appear in the CLI service's env or
  the trial artifacts.
- The `CodingHarnessDriver` sets `bridged_services` and
  `stripped_container_secrets` on the manifest; the netpolicy
  enforcement attaches the sidecar to both internal and edge networks,
  and `inject_runner_credentials` omits the shielded token from the
  runner container's payload — the credential exists in exactly one
  service (`tolokaforge-llm-gateway`) in the whole trial stack.
- `AgentDriver.close()` is a no-op under sidecar mode — compose stack
  teardown stops the sidecar along with every other trial container.
  The method stays on the protocol so runtimes may call it
  unconditionally.
- Every shipped harness in `data/harnesses.yaml` carries a
  `credential_gateway` block except `gemini-cli` — Gemini's REST auth
  is `x-goog-api-key` and its request paths are model-dynamic, both of
  which need config-file pinning and prefix path-allow-list support
  the gateway does not provide. Tracked in
  [#1311](https://github.com/Toloka/tolokaforge/issues/1311). A
  well-typed `UNSHIELDED_HARNESSES` set at
  `tests/unit/test_credential_gateway_schema.py` makes silent
  regressions impossible.

**Operator escape hatch:** `models.agent.disable_credential_gateway: true`
in the run config restores the pre-shield behavior — real key baked
into the CLI's container env, no sidecar added. Intended for the rare
CLI that a proxied backend cannot drive; none of the six shipped
harnesses need it today. The driver logs a warning naming the harness
when the escape hatch fires.

**Egress restriction:** the netpolicy's isolation still applies. Under
`no_internet` the CLI's service has no direct route to the outside
world; only the sidecar bridges the internal→edge boundary, and only
for the paths in the harness's `credential_gateway.path_allowlist`.
Under `limited_internet` a squid forward proxy is also injected for
any pack-declared outbound HTTP; the CLI's `NO_PROXY` skips squid for
the sidecar hop, which travels direct over the shared internal
network. Under `full_internet` no netpolicy runs.

**Named follow-up work:**

- `VaultProvider`, `CSIProvider`, `CloudMetadataProvider` — new
  `SecretProvider` subclasses so a runner pod bootstraps its own
  credentials from Vault / CSI-mounted secrets / cloud workload
  identity, without piggybacking on the orchestrator's
  `TOLOKAFORGE_SECRETS_JSON` payload. The `manager.py:299-301`
  comment already names these as targets.
- `gemini-cli` shielding (#1311) — config-file pinning for the
  `selectedType: gateway` invariant + prefix path-allow-list support
  in the gateway's HTTP handler.
- Interaction with roadmap `0.12.0` "Remote runner: orchestrator and
  runner on separate hosts" — the sidecar shape already works cluster-side
  (the compose stack becomes a K8s Job manifest); the remaining work
  is credential bootstrap, tracked above.

## Alternatives considered and rejected

- **Host-side proxy reached via `extra_hosts: host-gateway`.** Runs
  the gateway as a thread in the orchestrator process; the trial
  container reaches it through docker-desktop's `host-gateway` magic
  IP. Rejected because it fails under any pack declaring
  `network_policy=no_internet` — the CLI's service lives on a docker
  `internal:true` network with no route to the host, so `curl
  http://tolokaforge-llm-gateway:PORT` SYN-sends indefinitely
  (reproduced: 900s hang, zero assistant turns). The sidecar shape
  works under every network policy because it lives on the same
  compose network the CLI is on.
- **Extend the existing squid sidecar with credential injection.**
  Reuses one sidecar. Rejected because squid's auth mechanisms (ICAP,
  custom `auth_param`) are not designed for header rewriting on the
  outgoing hop; the sidecar config balloons and squid becomes
  responsible for two concerns.
- **Host-side proxy owned by the `Conductor`.** Conductor is per-trial
  (short-lived), N conductors × N ports for a run of N trials. The
  credential is per-run, not per-trial. Wrong lifetime.
- **In-container proxy (extend today's `middleware_proxy`).**
  Same-PID-namespace as the CLI, so the model can inspect / kill it.
  Not a credential shield.

## The invariants this ADR locks

1. `SecretManager` is the only reader of credentials — always.
   Enforced by `AGENTS.md` + `tests/unit/secrets/test_no_raw_secret_access.py`.
2. The CLI's own compose service ALWAYS receives a dummy
   `<dummy_token_env_var>` value + a base URL pointing at the sidecar
   (when the harness declares a `credential_gateway`).
3. The gateway sidecar ALWAYS routes its real credential through
   `SecretManager`: `TF_GATEWAY_UPSTREAM_TOKEN` is read once at
   bootstrap into a scoped `DictProvider`, registered via
   `register_runtime_secret`, and the log redactor is installed —
   after which every access goes through `get_default()`.
4. The gateway ALWAYS enforces path allow-list + header injection.
5. The shielded upstream token is ALWAYS in
   `EnvironmentManifest.stripped_container_secrets`, so
   `inject_runner_credentials` omits it from the runner container's
   `TOLOKAFORGE_SECRETS_JSON` payload. The credential exists in
   exactly one service in the trial stack.
6. Adding a new harness to `data/harnesses.yaml` MUST populate its
   `credential_gateway` block OR appear on the `UNSHIELDED_HARNESSES`
   set at `tests/unit/test_credential_gateway_schema.py`. There is no
   silent third path.

## Verification

- **Unit**: `_GatewayHTTPServer` — path allow-list, header rewriting,
  streaming pass-through byte-identity (`test_llm_gateway.py`).
  Sidecar bootstrap — reads `TF_GATEWAY_UPSTREAM_TOKEN` once through
  `SecretManager` + `register_runtime_secret` + `install_global_redactor`,
  refuses empty/unset (`test_llm_gateway_serve.py`). Driver — the
  CLI's compose service `environment:` provably never contains a
  canary standing in for the real token; the sidecar service does;
  `bridged_services` and `stripped_container_secrets` are set
  correctly (`test_coding_harness_gateway.py`).
- **Real-task smoke, terminal-bench**: `examples/terminal_bench/run_harness.yaml`
  under `claude-code` — score 0.833, 493s, 55 real assistant turns.
  Only dummy values reach the CLI container's `environment:`; the
  real key does not appear in trial artifacts.
- **Real-task smoke, native**: `examples/native/coding_harness/run_harness.yaml`
  under `claude-code` with the pack's declared `network_policy=no_internet`
  (preserved unchanged) — score 1.0, 16.4s, 1 tool call, 1 turn. The
  gateway sidecar is bridged to both `netpolicy_internal` and
  `netpolicy_edge`; the runner container's `TOLOKAFORGE_SECRETS_JSON`
  payload does NOT contain the shielded upstream token; the real key
  does not appear in trial artifacts.
- **Escape hatch**: `disable_credential_gateway=True` restores the
  pre-shield path — real token in the CLI's container env, no sidecar
  service added. Warning logged.
- **Package boundary**: `tolokaforge_coding_harnesses` still imports no
  engine modules — the driver depends on the harness package, never
  the reverse.
