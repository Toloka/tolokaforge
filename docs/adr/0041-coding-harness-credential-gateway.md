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

**Introduce an `LLMGatewayEndpoint`** — a small `SecretManager`-consuming
HTTP proxy that holds the real credential and forwards to upstream —
and bind its lifecycle to a **launcher** whose choice varies by
deployment. The CLI container sees only a dummy token and a base URL
pointing at the gateway; the real credential never enters the compose
`environment:`.

**Three-layer split:**

```
Layer 3: THE AGENT LOOP        ← claude-code, codex, Harbor, custom driver
                                  (varies per driver; pluggable)
Layer 2: THE GATEWAY ENDPOINT  ← LLMGatewayEndpoint(spec, SecretManager)
                                  (one class, portable across launchers)
Layer 1: THE LAUNCHER          ← HostGatewayLauncher (this PR)
                                  SidecarGatewayLauncher (follow-up)
                                  (varies per deployment)
```

Each layer varies independently. Adding a new agent loop is a new
driver class + new `HarnessSpec` entry (Layer 3). Adding a new
deployment shape is a new launcher class (Layer 1). Adding a new
credential protocol (OAuth, workload identity) is a new protocol
adapter inside the gateway (Layer 2).

### Ships this PR (local mode)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Local lifecycle                                                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Orchestrator process (host)                                            │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  SecretManager: DotEnvProvider + EnvProvider                     │   │
│  │  CodingHarnessDriver                                             │   │
│  │  ├─ container_env: dummy token, gateway URL                      │   │
│  │  ├─ _gateway_upstream_env: real token (in memory, never on disk) │   │
│  │  └─ LLMGatewayEndpoint (HTTP server on ephemeral port)           │   │
│  │       launched by LocalHostGatewayLauncher at attach()           │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│               ▲                                                         │
│               │ extra_hosts:                                            │
│               │   tolokaforge-llm-gateway:host-gateway                  │
│               │                                                         │
│  Trial container (docker default bridge)                                │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  ANTHROPIC_API_KEY = "sk-tolokaforge-shielded-dummy"             │   │
│  │  ANTHROPIC_BASE_URL = "http://tolokaforge-llm-gateway:52621"     │   │
│  │  NO_PROXY = "tolokaforge-llm-gateway"                            │   │
│  │  claude-code CLI  ────────────────────────────────────────────┐  │   │
│  └──────────────────────────────────────────────────────────────┼──┘   │
│                                                                 │      │
│                             LLMGatewayEndpoint forwards ────────┘      │
│                             with real header:                          │
│                             Authorization: Bearer <real key>           │
│                                             │                          │
│                                             ▼                          │
│                                    https://openrouter.ai               │
└─────────────────────────────────────────────────────────────────────────┘
```

### Reserved for a follow-up PR (cluster mode)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Cluster lifecycle (K8s Job; orchestrator may die)                      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Runner pod                                                             │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  SecretManager: VaultProvider (Vault Agent Injector)             │   │
│  │                 OR CSIProvider (mounted secret file)             │   │
│  │                 OR CloudMetadataProvider (IRSA / GKE WI)         │   │
│  │                                                                  │   │
│  │  Sidecar: LLMGatewayEndpoint served by SidecarGatewayLauncher    │   │
│  │           on the pod's internal network                          │   │
│  │           ├─ reads real credentials from the pod's SecretManager │   │
│  │           └─ orchestrator not needed once launched               │   │
│  │                                                                  │   │
│  │  Trial container (in the same pod)                               │   │
│  │  ├─ Dummy env, gateway base URL, K8s NetworkPolicy               │   │
│  │  └─ Reaches gateway sidecar over localhost — no host needed      │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                             │                                           │
│                             │ egress                                    │
│                             ▼                                           │
│                    https://openrouter.ai                                │
└─────────────────────────────────────────────────────────────────────────┘
```

### Alternate agent-loop shape (accommodated by construction)

```
Harbor as an embedded library in a custom driver:

  HarborDriver (peer of CodingHarnessDriver, implements AgentDriver)
  └─ decorate_task_description
      ├─ container gets: dummy env, gateway URL, extra_hosts
      └─ decorated wire schema

  Same LLMGatewayEndpoint. Same host-side launcher. No adapter edit.
```

## Consequences

**What changes today:**

- Every `tolokaforge run` operator gets credential-shielded coding-harness
  runs by default. No config change required.
- The trial container's compose `environment:` no longer contains any
  real LLM provider credential. Verified against
  `examples/terminal_bench/run_harness.yaml` + `examples/native/coding_harness/`:
  the resolved-secret string does not appear anywhere in the per-trial
  staging directory.
- `LLMGatewayEndpoint` starts at driver `attach()` and stops at
  `driver.close()` — a new `AgentDriver` protocol method the
  orchestrator invokes at run teardown. `EngineLoopDriver.close()` is a
  no-op.
- Every shipped harness in `data/harnesses.yaml` carries a
  `credential_gateway` block except `gemini-cli` — Gemini's REST auth
  is `x-goog-api-key` and its request paths are model-dynamic, both of
  which need config-file pinning and prefix path-allow-list support
  the initial gateway does not provide. Tracked in
  [#1311](https://github.com/Toloka/tolokaforge/issues/1311). A
  well-typed `UNSHIELDED_HARNESSES` set at
  `tests/unit/test_credential_gateway_schema.py` makes silent
  regressions impossible.

**Operator escape hatch:** `models.agent.disable_credential_gateway: true`
in the run config restores the pre-shield behavior — real key baked
into the container env. Intended for the rare CLI that a proxied
backend cannot drive; none of the six shipped harnesses need it today.
The driver logs a warning naming the harness when the escape hatch
fires.

**Egress restriction (defense-in-depth) is deferred.** The initial plan
paired the credential shield with `NetworkPolicy.LIMITED_INTERNET` so
squid would refuse any request from the CLI container except the
gateway hop. Local-mode incompatibility: `LIMITED_INTERNET` puts the
CLI on a docker `internal: true` network, and the host-side gateway
lives on the docker default bridge — the CLI has no route to the
gateway. A real smoke reproduced this: the CLI hung 900 seconds
waiting on the gateway that was one docker network away.

The credential shield still holds without egress restriction: the
container never sees the real key. What is deferred is preventing a
malicious model from exfiltrating task data to an attacker-controlled
URL. That protection requires moving the gateway to a sidecar
container on the same internal network as the CLI —
`SidecarGatewayLauncher`, called out as a follow-up.

**What follow-up work is named for this ADR:**

- `SidecarGatewayLauncher` — the cluster-mode adapter. Runs the
  gateway as a sidecar container in the trial's compose stack /
  pod. Makes `LIMITED_INTERNET` egress restriction viable again in the
  local case, and is the only viable shape in K8s.
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
  runner on separate hosts" — the gateway pattern must land BEFORE
  remote runner so cluster deployments don't re-invent it.

## Alternatives considered and rejected

- **Sidecar container in the compose stack for local mode.** Proper
  airgap possible (compose internal network). Rejected because the
  local-mode value is high on its own — every operator running
  `tolokaforge run` today benefits — and the extra container has
  measurable startup cost + operational complexity that hurts every
  local run for a defense-in-depth win only some operators need.
  Kept as `SidecarGatewayLauncher` for cluster mode, where the sidecar
  is the only viable shape.
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
2. The trial container ALWAYS receives a dummy `<dummy_token_env_var>`
   value + a base URL pointing at a gateway (when the harness declares
   a `credential_gateway`).
3. The gateway ALWAYS reads its real credentials via `SecretManager`
   — never directly from env.
4. The gateway ALWAYS enforces path allow-list + header injection +
   audit logging via `install_global_redactor`.
5. Adding a new harness to `data/harnesses.yaml` MUST populate its
   `credential_gateway` block OR appear on the `UNSHIELDED_HARNESSES`
   set at `tests/unit/test_credential_gateway_schema.py`. There is no
   silent third path.

## Verification

- **Unit**: `LLMGatewayEndpoint` — path allow-list, header rewriting,
  streaming pass-through byte-identity, constructor provably never
  reads `os.environ`. `CodingHarnessDriver` — container_env provably
  never contains a canary standing in for the real token
  (`test_coding_harness_gateway.py`).
- **Real-task smoke**: `examples/terminal_bench/run_harness.yaml` under
  `claude-code` reaches grading with the shield active. The staging
  directory contains no `sk-or-v1-` substring anywhere; only the
  dummy value reaches the container `environment:`. The CLI makes
  real tool calls (real database queries visible in the trajectory)
  and returns a real reward.
- **Escape hatch**: `disable_credential_gateway=True` restores the
  pre-shield path — real token in container env, no gateway attach,
  no `extra_hosts`. Warning logged.
- **Package boundary**: `tolokaforge_coding_harnesses` still imports no
  engine modules — the driver depends on the harness package, never
  the reverse.
