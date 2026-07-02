# 0010. `RuntimeBackend` provisioning contract — how a backend consumes an environment manifest

- **Status:** Proposed
- **Date:** 2026-07-01
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

ADR-0007 lifted `SharedStackRuntimeBackend` behind a typed `RuntimeBackend` Protocol; ADR-0009 introduced `EnvironmentManifest` as the typed declaration of a task's multi-service environment. Two halves of the multicontainer arc are in place: a Protocol for **where** a trial runs, and a schema for **what** it looks like. The half missing is **how** a backend consumes the manifest — the lifecycle contract between "orchestrator hands the backend a `TrialSpec`" and "backend hands back the endpoints the trial's runner talks to".

Today's `RuntimeBackend` surface (`connect` / `close` / `health_check` / `cleanup_trial` + a `RunnerClient` attribute) predates the manifest. It has no method for "bring up this trial's environment", no notion of a per-trial handle, no lifecycle semantics for the trial-scoped services the manifest can declare. Without the missing contract, ADR-0009's safety declarations (`read_only`, `network: isolated`, `resources` caps, `security_context`, health probes) are theatre — no one is contractually obligated to honour them.

The gap is not just missing methods. It is the absence of a written-down agreement about what a provisioner is *required* to do with a manifest, what happens when it fails, who owns the lifecycle, and how selection is expressed. Writing that down before the first concrete provisioner ships is the whole point of this ADR.

## Architecture context — picture before prose

### The three seams already in place

```
                    Orchestrator (scheduler)
                            │
                            │  conductor.run(spec, task_config)
                            ▼
                       Conductor (ADR-0008)
                            │
                            │  ??? this ADR fills the ??? ???
                            ▼
                    RuntimeBackend (ADR-0007)
                            │
                            │  reads → EnvironmentManifest (ADR-0009)
                            ▼
                      trial environment
                     (containers, network,
                      services, endpoints)
```

The `???` is where ADR-0010 lives.

### Lifecycle after this ADR

```
For each trial:

  1. Orchestrator selects a RuntimeBackend from config          (once per run)
        │
        ▼
  2. Conductor.run() picks up the trial's spec + task_config
        │
        ├─▶ 3. backend.provision(spec)  ──▶  EnvHandle
        │      (containers up, network created, initial state applied)
        │
        ├─▶ 4. backend.await_ready(handle)
        │      (blocks until health probes pass; ProvisionError on timeout)
        │
        ├─▶ 5. backend.endpoints(handle) ──▶ EnvEndpoints
        │      (per-trial service URLs)
        │
        ├─▶ 6. Trial body runs — agent loop, runner RPC, grading
        │
        └─▶ 7. backend.teardown(handle) in finally
               (containers stopped, network removed; idempotent)

Concurrent trials each get their own EnvHandle → their own isolated stack.
```

## Decision Drivers

- **Fill the missing seam, not carve a new one.** The provisioner role is one face of `RuntimeBackend`, not a separate abstraction. A future reader should be able to point at the manifest, the Protocol, and one contract document and see how they compose — not chase three overlapping interfaces.
- **Lock the enforcement obligations.** ADR-0009's safety declarations are only meaningful if some contract requires a provisioner to honour them. This ADR is where that requirement lives.
- **Fail loud, not silent.** A provisioner that cannot enforce a declared property (`read_only`, `network: isolated`, resource caps, security context) must reject the manifest at `provision` time, not silently degrade to a weaker posture.
- **Local-first.** The `local` runtime backend must always work without a cluster or a remote surface. The contract's typing (opaque `EnvHandle`, in-process today) must leave room for future remote provisioning without forcing it now.
- **Backwards compatible with the shared-stack path.** Every existing task continues to run through `SharedStackRuntimeBackend` unchanged. The new methods have no-op or shared-stack semantics on that backend.
- **Same iterate-cheaply property as ADR-0009.** Land ADR + Protocol + canonical contract tests together; keep status `Proposed` until the first concrete consumer (`PerTrialRuntimeBackend`) validates the contract end-to-end.

## Considered Options

1. **Extend `RuntimeBackend` with four methods (`provision` / `await_ready` / `endpoints` / `teardown`), an opaque `EnvHandle`, and a typed `ProvisionError`.** Encode every safety-relevant enforcement obligation in the ADR's contract clauses. Ship the extension, contract tests, and no-op adapter on `SharedStackRuntimeBackend` as one PR. **This ADR.**
2. **Separate `Provisioner` Protocol distinct from `RuntimeBackend`.** Splits the abstraction into two: the runtime (lifecycle, RPC surface) vs. the provisioner (environment materialisation). Cleaner in theory; doubles the injection points and the Protocol-promotion work; no near-term backend legitimately implements one without the other.
3. **Inline the provisioning calls in `Conductor`.** The conductor already runs one trial end-to-end; it could hand-roll the compose lifecycle itself. Rejected: couples the conductor to the substrate; every future backend would need conductor changes; loses the "swappable execution surface" property ADR-0007 was written to establish.
4. **Defer until the first concrete backend needs it.** Ship `PerTrialRuntimeBackend` first, then extract the contract from what actually shipped. Rejected for the same reason ADR-0009 was written before its first consumer: the contract iterates cheaply against a Pydantic + tests surface, and expensively against a deployed backend.

## Decision

We adopt **Option 1**.

### Method surface on `RuntimeBackend`

Four new methods, added to the existing Protocol in `tolokaforge/core/runtime.py`:

```python
class RuntimeBackend(Protocol):
    # Existing (ADR-0007):
    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None: ...
    def close(self) -> None: ...
    def health_check(self) -> bool: ...
    def cleanup_trial(self, trial_id: str) -> dict[str, Any]: ...
    executor_client: RunnerClient

    # New (this ADR):
    def provision(self, spec: TrialSpec) -> EnvHandle: ...
    def await_ready(self, handle: EnvHandle) -> None: ...
    def endpoints(self, handle: EnvHandle) -> EnvEndpoints: ...
    def teardown(self, handle: EnvHandle) -> None: ...
```

- **`provision(spec)`** — reads `spec.task.environment_manifest`, materialises its services (or fails `ProvisionError`), returns an `EnvHandle` the backend understands. When `environment_manifest is None`, the backend materialises the shared-stack view (backwards-compat path).
- **`await_ready(handle)`** — blocks until every compose service in the handle passes its `healthcheck:` probe. Raises `ProvisionError` on probe timeout (before returning; not silently after).
- **`endpoints(handle)`** — resolves per-trial URLs for the runner + declared services. Returns a fully populated `EnvEndpoints` (ADR-0006). Does not block; readiness is `await_ready`'s job.
- **`teardown(handle)`** — stops containers, removes per-trial network, releases any lease the backend holds. **Idempotent** — calling on an already-torn-down handle is a no-op, not an error. **Best-effort** — logs but does not raise if a container was already gone.

### `EnvHandle` — opaque Protocol-typed value

```python
@runtime_checkable
class EnvHandle(Protocol):
    """Opaque per-trial handle returned by RuntimeBackend.provision.

    Each backend defines its own internal shape (compose project name,
    pod identity, remote-lease id). Callers should treat the handle as
    unopened; only the backend that issued it can interpret its fields.
    """
    trial_id: str
```

`trial_id` is the one field callers may read — it's the identity for logs and for `cleanup_trial`. Everything else is backend-private. The typing leaves room for future remote provisioning: a `RemoteEnvHandle` can be a small serializable dict (`{trial_id, provider, lease_id}`) without breaking the Protocol.

### `ProvisionError` — typed failure

```python
class ProvisionError(Exception):
    """Raised when RuntimeBackend.provision or await_ready cannot bring
    the trial environment up to the state the manifest declares.

    The provisioner is expected to have made a best-effort teardown of
    anything it partially materialised before raising. Callers may
    call teardown(handle) again — it is idempotent.
    """
    trial_id: str
    stage: Literal["provision", "await_ready"]
    reason: str
```

### Enforcement obligations

A `RuntimeBackend` implementation is contractually obligated to honour every declaration in the manifest and the compose file it points at. Failure to enforce any of them at `provision` time — not later, not silently — is a contract violation:

| Declared where | Provisioner obligation |
|---|---|
| `EnvironmentManifest.network_policy` = `no_internet` | No egress to the public internet; no reachability across per-trial projects. Enforce via substrate-native network isolation. |
| `EnvironmentManifest.network_policy` = `limited_internet` | Egress permitted for the provisioner-defined allowlist; no cross-trial reachability. |
| `EnvironmentManifest.network_policy` = `full_internet` | Unrestricted egress; still no cross-trial reachability. |
| `EnvironmentManifest.security_context_defaults` | Every declared field applied to each compose service that does not set the equivalent explicitly (`run_as_user`, `read_only_root_filesystem`, `no_new_privileges`, capability drops / adds). Fail `ProvisionError` if the substrate cannot enforce a declared field. |
| `EnvironmentManifest.initial_state` | Fixture applied per its `kind` (`sql` / `copy` / `script`) to the named compose service before `await_ready` returns. |
| Compose service `deploy.resources` / `mem_limit` / `cpus` | Container-level CPU / memory caps enforced as declared. |
| Compose service `volumes:` with `:ro` short-form or `read_only: true` long-form | Bind or named mount honours the read-only flag. |
| Compose service `healthcheck:` block | `await_ready` respects `start_period`, `interval`, `timeout`, `retries` before returning. |
| Compose service `depends_on` (short-form list or long-form mapping) | Startup ordering enforced; `condition: service_healthy` waits on the probe passing. |
| Compose service `image:` pinning (validated at manifest load-time) | Substrate honours the exact tag or digest; no automatic tag resolution. |

**Rule of thumb: if the substrate cannot enforce a declared property, `provision` raises `ProvisionError` — not later, not silently. No provisioner may silently drop, downgrade, or ignore a declaration.**

### Lifecycle ownership

- **`Conductor.run()`** owns per-trial calls: `provision(spec)` before the trial body, `endpoints(handle)` to resolve per-trial URLs to feed into the runner, `teardown(handle)` in a `finally` block (runs on both success and failure paths).
- **`Orchestrator`** owns the `RuntimeBackend` **instance** for the whole run: one backend per run, injected into the `Conductor` at construction time.
- **Runtime-backend selection** is a run-level, config-driven choice (see below), decided once at the start of `run()`.

This split makes the trial-level concern (provision/teardown per trial) local to the `Conductor` where it belongs, and keeps the backend instance stable across the run so per-run resources (client connections, warm pools) are not repeatedly built and torn down.

### Failure semantics

- **Partial-startup failure**: `provision` raises `ProvisionError(stage="provision")` after making a best-effort teardown of anything it materialised before the failure. The handle is not returned. The caller does not need to call `teardown` again — but if it does (defensive `finally`), `teardown` handles the already-torn-down state as a no-op.
- **Health-probe timeout**: `await_ready` raises `ProvisionError(stage="await_ready")`. The handle **is** valid (containers are up); the caller must call `teardown` to clean up.
- **`teardown` is idempotent**: subsequent calls on the same handle are no-ops. `teardown` may log substrate-side warnings ("container X was already stopped") but does not raise.
- **Orphan sweep on `connect`**: the backend may perform a best-effort sweep of leftover per-trial resources from a crashed previous run (matched by `trial_id` prefix in the backend's naming scheme). Best-effort — a failure to sweep does not fail `connect`.

### Selection mechanism

Runtime-backend selection is config-driven at run start:

```yaml
runtime:
  backend: local        # "local" | "shared"; default "shared" preserves today's behaviour
```

`shared` = `SharedStackRuntimeBackend` (today's shared-stack path). `local` = `PerTrialRuntimeBackend` (per-trial isolation; the concrete consumer landing in a follow-up PR). Additional backends slot in behind the same key without breaking existing configs.

The existing `auto_start_services` flag stays as-is; it controls whether the orchestrator brings services up at all, not which backend materialises them.

### Backwards compatibility

`SharedStackRuntimeBackend` implements the four new methods with **shared-stack semantics**:

- `provision(spec)` — no-op that returns a handle pointing at the run-wide shared stack. All trials in the run receive equivalent handles referencing the same stack.
- `await_ready(handle)` — no-op if the shared stack was already brought up at `connect` time.
- `endpoints(handle)` — returns the run-wide shared `EnvEndpoints` unchanged.
- `teardown(handle)` — no-op; the shared stack lives for the whole run and is torn down at `close`.

Existing tasks running through `SharedStackRuntimeBackend` see no behavioural change. The Protocol widens; nothing that already worked breaks.

`InMemoryRuntimeBackend` (the ADR-0007 test fixture) is extended with deterministic stub implementations of the four methods, adding call-log entries so orchestrator-level tests can assert lifecycle ordering without spinning up Docker.

### Explicitly out of scope

- **`stream_logs`**. The internal runtime-architecture sketch listed a `stream_logs(handle) -> Iterator[bytes]` method on `RuntimeBackend`. Deferred — no consumer today needs streaming (grading and artifact writing read final state, not live streams). Adds as a Protocol method when a consumer (e.g. a live-tail UI or a control-plane observability surface) exists.
- **Remote provisioning**. The `EnvHandle` typing leaves room for serializable handles, but no concrete `RemoteRuntimeBackend` exists yet and this ADR does not commit to one. That's a later ADR that will settle the on-the-wire handle shape.
- **Provisioner side-effect budgets** (CPU / memory quotas across concurrent trials). Individual trials honour their manifest's `Resources`; system-wide caps are a scheduler concern, not a provisioner one.
- **GPU / accelerator declarations**. Handled by the substrate today via the existing `Resources.cpu` / `memory` string surface; if declaring GPUs becomes a first-class need, it lands in a follow-up manifest schema update, not this ADR.

## Impact on existing tasks

**None in the PR that lands this ADR.** The Protocol widens; `SharedStackRuntimeBackend` adapts with no-op semantics that preserve the shared-stack path exactly. Every existing task continues to run unchanged.

The first behavioural change lands with the follow-up PR that ships `PerTrialRuntimeBackend` — and even then, tasks default to `SharedStackRuntimeBackend` (backward-compat) until a run's config opts into `runtime.backend: local`. Per-trial isolation is opt-in until the first workload validates it end-to-end.

## Industry precedents studied

The four-method surface (`provision` / `await_ready` / `endpoints` / `teardown`) is not novel — it is the widely-used shape of per-scope environment materialisation. Anchoring the ADR against these precedents makes clear what we are and are not inventing.

### Testcontainers — primary precedent

The Testcontainers family of libraries (https://testcontainers.com, Java/Python/Go/Node) is the closest direct precedent. Their per-container lifecycle is `start()` + `waitingFor(strategy)` + `getMappedPort(...)` + `stop()`; our per-trial surface is the same shape one abstraction up (a handle over a *set* of services, not a single container). Testcontainers' `WaitStrategy` is the pattern our `HealthProbe` (ADR-0009) compiles to at runtime; their `ContainerState` is the analog of our opaque `EnvHandle`.

### Inspect AI sandbox providers — same seam decision

Inspect AI (UK AI Safety Institute, https://inspect.aisi.org.uk/sandboxing.html) chose the same "one Protocol, many backends" seam we are choosing here: a single `SandboxEnvironment` interface implemented by a docker provider, a Kubernetes provider, and a local provider. Our `RuntimeBackend` plays the same role. Cross-checking against Inspect's provider set validated that four lifecycle methods are sufficient — no substrate they support requires a fifth.

### Docker Compose CLI — the substrate `PerTrialRuntimeBackend` will target

`docker compose up -d --wait` (provision + readiness), `docker compose port` (endpoint resolution), `docker compose down -v` (teardown). This is the substrate the first concrete backend compiles the manifest to; the per-trial-project naming convention (`{prefix}-{trial_id}`) is standard Compose usage.

### Kubernetes Pod lifecycle — pattern preserved, not backend committed

The four-phase pattern (create → readinessProbe → Service DNS → delete) is the same shape one substrate over. Called out here as *the pattern the contract preserves* — not a commitment that a Kubernetes backend is planned. If a cluster backend later materialises, **Kubernetes Agent Sandbox** (kubernetes-sigs SIG-Apps, https://agent-sandbox.sigs.k8s.io) is the natural forward target: it is explicitly designed for one multi-container Pod per sandbox with declarative health probes and per-invocation teardown.

### SWE-bench harness — per-trial isolation posture

Per-instance ephemeral container per trial, torn down at end. Establishes the isolation posture our `provision` / `teardown` obligations codify: no cross-trial reachability, no shared mutable state, one lifecycle per trial.

### HashiCorp Terraform providers — the staged-failure model

The per-substrate provider pattern is directly analogous; more specifically, Terraform's staged failure model (`plan` vs. `apply` vs. `refresh` errors) is the precedent for our `ProvisionError.stage: Literal["provision", "await_ready"]`. A caller distinguishing "couldn't bring it up" from "brought it up but it isn't healthy" is a well-known distinction; conflating them loses retry-policy nuance.

### Opaque-handle convention

`EnvHandle` follows a common cloud-SDK pattern: AWS SDK waiters return service-side identifiers the caller treats as opaque; the Kubernetes dynamic client returns `*unstructured.Unstructured` wrapping only the object identity; Testcontainers' `ContainerState` is the same idea. The typing does not commit to Python-object handles — a serializable dict handle (`{trial_id, provider, lease_id}`) is a valid future extension for a remote backend without breaking the Protocol.

## Consequences

### Positive

- Every manifest safety declaration now has a contract clause requiring a provisioner to honour it. `read_only`, `network`, `resources`, `security_context`, health probes — all move from "the schema says so" to "the backend is required to enforce so".
- The lifecycle picture is written down in one place: who calls `provision`, who calls `teardown`, when, and what happens on failure. Future readers do not re-derive it.
- The `EnvHandle` abstraction leaves room for remote provisioning without forcing it. The typing does not commit to Python-object handles.
- `SharedStackRuntimeBackend` continues to satisfy the widened Protocol; no existing task is disturbed by the change.
- The `Conductor` becomes ready to compose against any provisioning backend without further conductor-side changes. The next PR (`PerTrialRuntimeBackend`) is a pure Protocol implementation, not a call-site refactor.

### Negative / Trade-offs

- The `RuntimeBackend` Protocol grows. Every future implementation carries four more methods to fulfil. Acceptable: three of the four (`provision`, `endpoints`, `teardown`) are the minimum a per-trial substrate needs; the fourth (`await_ready`) could be folded into `provision` at the cost of blocking semantics being invisible in the type surface, which we chose against.
- `ProvisionError` is a new failure surface the orchestrator's retry machinery has to think about. Mitigation: it's the same shape as existing typed exceptions the runtime already handles (e.g. `RunnerClientError`); the retry policy needs one extra branch, not a rewrite.
- The "fail loud on unenforceable declarations" rule imposes work on backend authors — they cannot ship a partial implementation that silently degrades one property. This is intentional; a runtime that silently ignores `security_context` is worse than a runtime that refuses to run.

### Follow-ups

- **`PerTrialRuntimeBackend`** — the first concrete consumer that actually enforces the obligations. Ships with its own PR immediately after this ADR lands; also flips ADR-0010 status from `Proposed` to `Accepted` once end-to-end validation is green.
- **`stream_logs` method** — added when a consumer appears (live-tail, observability sink).
- **Remote provisioner ADR** — settles the on-the-wire `EnvHandle` shape for out-of-process backends. Blocked on a concrete remote workload materialising.
- **Orphan-sweep policy formalisation** — today's "best-effort on `connect`" is a reasonable default; a future ADR may tighten it if crash-resilience becomes a hard requirement.

## Rejected alternatives

- **Option 2 — separate `Provisioner` Protocol.** Two Protocols where one suffices; every future backend implements both anyway. Rejected as premature abstraction.
- **Option 3 — inline in `Conductor`.** Couples the conductor to the substrate; every backend requires a conductor edit. Defeats the "swappable execution surface" property ADR-0007 established.
- **Option 4 — defer until a backend needs it.** Loses the cheap-iteration property. The contract iterates cheaply against a Protocol + tests surface; against a deployed backend it iterates painfully.

## Scope notes

- **Method placement.** The four new methods live on `RuntimeBackend`, not on a helper class the backend composes with. Rationale: any provisioner is a backend from the orchestrator's perspective; putting the methods on the backend keeps the injection surface flat.
- **`connect` / `close` unchanged.** The existing lifecycle methods keep their meaning (bring the *backend* up / down for the run). `provision` / `teardown` are per-trial; they are strictly nested inside `connect` / `close` for the lifetime of the run.
- **No gRPC `.proto` change.** This ADR types the orchestrator ↔ backend surface, not the runner gRPC wire.
- **Contract tests are canonical.** `tests/canonical/test_runtime_backend_contract.py` grows to pin every new method's contract: handle round-trip, idempotent teardown, `ProvisionError` semantics for both `stage` values, no-op compat semantics on `SharedStackRuntimeBackend`. Any silent drift fails CI.
- **Status stays `Proposed` until end-to-end validation.** Same discipline ADR-0009 used: land the design + contract tests; flip to `Accepted` when the first concrete consumer (`PerTrialRuntimeBackend`) validates the contract against a real workload.
