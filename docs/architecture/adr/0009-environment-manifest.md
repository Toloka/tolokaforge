# 0009. `EnvironmentManifest` — compose-as-source-of-truth for per-trial environments

- **Status:** Proposed
- **Date:** 2026-07-02
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Multi-service tasks (agent + database + tool services) need per-trial environment isolation: each trial gets its own containers, its own bind mounts, its own initial state. The engine's current shared-stack path — one docker-compose project shared across every trial in a run — makes cross-trial state contamination structural. It also caps concurrency at one trial per stack.

Solving that requires the engine to know, for a given task, what its environment looks like. Not just "which containers" — but also which service is the agent's runner, which fixtures to apply before the run starts, what network posture to enforce, and what security defaults to apply to every service.

That declaration is what this ADR defines: `EnvironmentManifest`, a small Pydantic wrapper that points at a Docker Compose file, adds the engine-specific fields the provisioner needs, and applies safety validators against the loaded compose contents at construction time.

## Architecture context — picture before prose

### Today: one stack, shared across trials

```
                          Orchestrator
                                │
                                ▼
                     ServiceStack (shared)
                    ┌───────┬───────┬───────┐
                    │  db   │ runner│  rag  │
                    └───────┴───────┴───────┘
                        ▲       ▲       ▲
                        │       │       │
                    trial 0 trial 1 trial 2   (all trials share the same containers)
```

### Next: one isolated stack per trial, declared by a manifest

```
              Orchestrator
                     │
                     ▼
    ┌────────────┬────────────┬────────────┐
    ▼            ▼            ▼            ▼
 stack 0     stack 1     stack 2     stack N
 ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐
 │  db  │   │  db  │   │  db  │   │  db  │
 │runner│   │runner│   │runner│   │runner│
 └──────┘   └──────┘   └──────┘   └──────┘
    ▲          ▲          ▲          ▲
    │          │          │          │
 trial 0    trial 1    trial 2    trial N
```

The manifest is the declarative side of this arc; the runtime backend (ADR-0010) is the consuming side.

## Decision Drivers

- **The compose file is already the industry-standard artefact for declaring a multi-service environment.** Task authors know Docker Compose. Tooling (`docker compose config`, IDE integrations, `docker compose up`) works out of the box. Adjacent agent-eval harnesses that solve the same problem (Inspect AI in particular) consume compose files directly.
- **Substrate portability.** Docker Compose translates to Kubernetes Pods via industry-standard tooling (`kompose`, Testcontainers' k8s module) and to remote sandbox platforms via their own SDKs. Adopting compose as the source-of-truth avoids building a parallel schema that would then need those same translators anyway.
- **Safety belongs to the wrapper, not the substrate.** The engine's safety controls (path-traversal guards on bind mount sources, rejection of `network_mode: host`, rejection of `privileged: true`, rejection of `cap_add`) are engine concerns, not compose concerns. Encoding them as load-time validators against the compose contents keeps the substrate agnostic and the safety story ours to enforce.
- **Ecosystem interop.** Tasks whose sandbox spec is already a compose file (Inspect AI's docker sandbox convention, community task packs) run natively with zero adapter code.
- **Iterate cheaply until validated.** The manifest lands as `Proposed`; contract tests pin every safety validator against fixture compose files. Status flips to `Accepted` once a real workload runs end-to-end on a per-trial backend (ADR-0010's follow-up work).

## Considered Options

1. **Compose file as source-of-truth; `EnvironmentManifest` is a Pydantic wrapper adding engine-specific fields and safety validators.** *This ADR.*
2. **Field-by-field Pydantic schema that re-encodes compose semantics.** Every compose field (`services`, `ports`, `volumes`, `depends_on`, `health`, `resources`) becomes a Pydantic type owned by the engine. Rejected: pays a translation tax at every backend (Pydantic → compose → docker) and diverges from every neighbour in the agent-eval space that reads compose directly.
3. **Python-code environment definitions (METR task-standard's approach).** Task authors write a `TaskFamily` class with `install()` / `start()` / `teardown()` methods. Rejected: sacrifices the ability to statically analyse environments (which our safety-validator layer requires) and diverges from the compose ecosystem tooling.

## Decision

We adopt **Option 1**. `EnvironmentManifest` is:

```python
class EnvironmentManifest(BaseModel):
    model_config = {"extra": "forbid"}

    compose_file: Path
    """Path to the docker-compose file. Absolute if resolved by a task
    loader; relative paths are resolved against the current working
    directory at construction time."""

    runner_service: str = "default"
    """Which compose service is the agent runner. Must be declared in
    the compose file's `services:` mapping."""

    initial_state: dict[str, InitialStateRef] = {}
    """Fixture-copy operations, keyed by service name."""

    network_policy: NetworkPolicy = NetworkPolicy.NO_INTERNET
    """Network posture the provisioner is asked to enforce."""

    security_context_defaults: SecurityContext | None = None
    """Applied by the provisioner to every service that does not override
    the equivalent settings in the compose file."""
```

Load-time safety validators run against the loaded compose contents. Failing any of them raises `ValidationError` at construction:

- **Bind mount sources** — reject `..` segments and absolute paths (both short-form `SOURCE:TARGET` and long-form `{type: bind, source: ...}` syntaxes).
- **`network_mode: host`** — rejected outright. The manifest's `network_policy` is the only network-posture surface.
- **`privileged: true`** — rejected outright.
- **`cap_add`** — rejected outright. If a task genuinely needs added capabilities, they belong on the manifest's `SecurityContext.capabilities_add` — a location the provisioner can reason about and log.

Cross-field checks:

- `runner_service` must be a service declared in the compose file.
- Every key in `initial_state` must reference a declared compose service.

### `NetworkPolicy` — permission-string network model

Three literal states, following METR task-standard's permission-string convention:

| Value | Semantics |
|---|---|
| `no_internet` (default) | Services reach each other on the per-trial network only. No egress to the public internet; no reachability across per-trial projects. |
| `limited_internet` | Egress permitted for a provisioner-defined allowlist. No cross-trial reachability. The allowlist is a provisioner concern, not a schema concern. |
| `full_internet` | Unrestricted egress. Still no cross-trial reachability. |

Extending to a fourth mode (e.g. `dns_only`) is a permission-string addition; no consumer breaks.

### `SecurityContext` — defaults applied by the provisioner

`SecurityContext` declares per-container security policy. On the manifest it appears as `security_context_defaults` — the provisioner applies each declared field to every compose service that does not already set the equivalent. Task authors who want per-service policy set it in the compose file (`security_opt`, `user`, `read_only`, `cap_drop`, `cap_add` — subject to the safety validators).

### `InitialStateRef` — how a fixture applies to a service

```python
class InitialStateRef(BaseModel):
    from_: str          # relative path to the fixture (validated: no absolute, no `..`)
    kind: Literal["sql", "copy", "script"] = "copy"
```

- `sql` — piped through the service's SQL client.
- `copy` — written to a well-known path inside the service's container.
- `script` — executed inside the service's container.

Applied by the provisioner before `await_ready` returns.

### Two-file task authoring model

```yaml
# task.yaml
task_id: my_task
name: My Task
category: general
description: ...
adapter_type: native
system_prompt: ...
environment_manifest:
  compose_file: ./environment/compose.yaml
  runner_service: default
  network_policy: no_internet
  initial_state:
    db:
      from: ./fixtures/db-seed.sql
      kind: sql

# environment/compose.yaml   (pure Docker Compose)
services:
  db:
    image: postgres:16
    healthcheck: { test: ["CMD-SHELL", "pg_isready"] }
  default:
    image: tolokaforge/runner:0.5.0
    depends_on:
      db: { condition: service_healthy }
```

## Impact on existing tasks

### Per-trial isolation is the universal architectural goal

Every task in the engine — not just multi-service ones — moves to per-trial isolation. Single-container tasks get a one-service manifest; the underlying infrastructure changes even where the task's declared shape is minimal.

### What happens to existing tasks across the arc

- Tasks that do not declare an `environment_manifest` continue to run on the shared-stack path (`SharedStackRuntimeBackend`, unchanged). No behavioural change from this ADR.
- Tasks that opt into a manifest run through the per-trial provisioning path (ADR-0010 + `PerTrialRuntimeBackend`, follow-up PR). The manifest is validated at load time; unsafe configurations fail before any container starts.

### Does a task need a manifest to "comply"?

No. The manifest is opt-in. A task with no `environment_manifest` continues to work under the shared-stack semantics. Task authors adopt the manifest when they want per-trial isolation.

## Safety boundaries

- **Grading runs outside the trial container.** The grader (rubric judge, transcript checker, state-hash comparator) executes in the runner-side worker thread; the agent cannot reach the grader's code. This is a load-bearing safety property distinct from container isolation, and it is preserved unchanged by this ADR.
- **Safety validators are load-time, not runtime-time.** A manifest that would let a service escape the per-trial boundary (`network_mode: host`, `privileged: true`, `cap_add`, `..` in a bind mount source) fails to load. Provisioning cannot start.
- **The manifest declares; the provisioner enforces.** The manifest is the declaration surface. ADR-0010's `RuntimeBackend` provisioning contract requires the provisioner to honour every declared field, or to fail `provision` if it cannot. Silent degradation is a contract violation.

## Industry precedents studied

### Inspect AI (UK AI Safety Institute) — primary precedent

Inspect AI's docker sandbox provider consumes a `compose.yaml` file verbatim, resolves service topology from the compose file's `services:` mapping, and layers a small config layer for provider-specific fields (a `default` service convention, cleanup semantics). This ADR adopts the same shape: compose is the source of truth; the wrapper adds engine-specific fields.

### METR task-standard — permission-string network model

METR's `manifest.yaml` uses permission strings for network access (`no_internet` / `limited_internet` / `full_internet`) rather than a boolean or a specific-substrate literal. This ADR's `NetworkPolicy` follows that convention: extensible without breaking callers, and readable at a glance without needing to know the substrate.

### Testcontainers — the library that consumes the manifest

Testcontainers Python's `testcontainers.compose.DockerCompose` module consumes a compose file directly. Adopting compose as source-of-truth means the concrete `PerTrialRuntimeBackend` (ADR-0010 follow-up) is a thin adapter over Testcontainers — no Pydantic-to-compose translator to own.

### SWE-bench — layered image caching, not manifest

SWE-bench's harness uses a 3-tier image hierarchy (base → environment → instance) for build-time caching. That is a build-time optimisation, not a schema concern, and it composes with any manifest that declares images with pinned tags or digests.

## Consequences

### Positive

- Task authors write compose files — an artefact they already know. The engine adds a small typed wrapper on top; the total learning curve is bounded.
- Every safety-relevant configuration (`network_mode: host`, `privileged: true`, `cap_add`, bind-mount traversal) fails to load. Unsafe manifests never reach a provisioner.
- Compose files run through `docker compose config` for structural validation, through IDE integrations for authoring, and through `docker compose up` for standalone testing. Zero-cost interop with the surrounding ecosystem.
- The concrete `PerTrialRuntimeBackend` consumes the compose file directly via Testcontainers. No engine-owned Pydantic-to-compose translator.
- Alignment with Inspect AI's docker sandbox convention makes ecosystem interop trivial: an Inspect-authored compose sandbox spec runs through `EnvironmentManifest` with no adapter code.

### Negative / Trade-offs

- Task authors edit two files (`task.yaml` + `environment/compose.yaml`) instead of one. Acceptable — the alternative (one file mixing engine-specific config with compose config) is harder to reason about and diverges from ecosystem convention.
- Compose is docker-specific. A future Kubernetes backend translates the compose file to a Pod spec at provisioning time (via `kompose`, Testcontainers' k8s module, or a small owned translator). One industry-standard translator vs. two engine-owned translators; still the smaller cost.
- Author-time type safety on the compose file itself is looser than an all-Pydantic schema would give (compose YAML is a permissive spec). Mitigated by the safety validators, which catch the classes of misconfiguration that matter, and by `docker compose config`.

### Follow-ups

- **`PerTrialRuntimeBackend`** — the first concrete provisioner. Consumes `manifest.compose_file` directly via `testcontainers.compose.DockerCompose`.
- **Endpoint-resolution conventions** — `PerTrialRuntimeBackend` resolves `EnvEndpoints` from the compose file via convention: `runner_service` (default `"default"`) at gRPC port `50051` → `runner_url`; a compose service named `db` at port `5432` → `db_url`; a compose service named `rag` or `rag-service` at its declared port → `rag_url`. Task packs whose services deviate from these names or ports will get manifest-level overrides (`runner_port`, `db_service`, `db_port`, `rag_service`, `rag_port`) in a follow-up PR — the current shape prioritises simplicity for the common case.
- **`NetworkPolicy.LIMITED_INTERNET` allowlist mechanism** — provisioner-defined; separate ADR when the first workload requires it.
- **`K8sRuntimeBackend` design ADR** — filed when the K8s backend becomes concrete work.

## Rejected alternatives

- **Field-by-field Pydantic schema re-encoding compose.** Owning `ServiceSpec` / `PortSpec` / `VolumeMount` / `HealthProbe` / `Resources` / `DependsOn` as Pydantic types. Rejected — pays a translation tax at every backend, diverges from every neighbour that reads compose directly, and gives no safety guarantee that a compose-file wrapper cannot give equivalently.
- **METR-style Python code environment definitions.** Task authors write `TaskFamily` classes with lifecycle methods. Rejected — sacrifices static analysability, which the safety-validator layer requires, and diverges from compose tooling.
- **One-file authoring (compose file with `x-tolokaforge:` extension carrying engine config).** Considered; rejected because it mixes engine-specific config with compose config in a single artefact, obscuring which fields the engine controls and which are pure compose semantics.

## Scope notes

- **Status.** `Proposed`. Flips to `Accepted` when a real workload runs end-to-end on a per-trial backend (ADR-0010's follow-up).
- **Contract tests are canonical.** `tests/canonical/test_environment_manifest_contract.py` pins every safety validator against fixture compose files (`tests/canonical/fixtures/environment_manifest/*.yaml`). Any silent drift fails CI.
- **The manifest does not cache compose contents.** Every `manifest.load_compose()` re-reads from disk. Backends that need per-trial variants (per-project prefix, etc.) do so at provision time.
