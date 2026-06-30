# 0009. `EnvironmentManifest` — typed schema for per-trial multicontainer environments

- **Status:** Proposed
- **Date:** 2026-06-30
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The engine's typed-seam arc is complete: every plane has a `@runtime_checkable` Protocol with at least two implementations. The next architectural arc is **per-trial environment isolation** — a task that needs `db + backend + frontend` (or even `db + rag`) should run in its own isolated stack, not share one stack across every trial of the run.

Today there is no typed declaration of what a task's world looks like. Tasks fall back to a small set of hard-coded stack templates (`core_stack`, `full_stack`) selected by tool declarations; the orchestrator brings the shared stack up once at the start of a run and tears it down at the end. That choice predates per-trial isolation and locks the engine to a single-substrate, shared-stack model.

We need a typed, declarative schema that:

- expresses any multi-service environment a task could need,
- does not bake in any one substrate's grammar (so non-local substrates remain a future option without re-design),
- is the same wire format every runtime backend reads.

That schema is the **environment manifest**. This ADR proposes it.

## Architecture context — picture before prose

### Today: one stack, shared across trials

```
┌──────────────────────────────────────────────┐
│            Run-wide shared stack             │
│  (one ServiceStack, lives for the whole run) │
│                                              │
│  ┌──────┐  ┌────┐  ┌─────┐  ┌──────────────┐ │
│  │runner│  │ db │  │ rag │  │  typesense   │ │
│  └──────┘  └────┘  └─────┘  └──────────────┘ │
└──────────────────────────────────────────────┘
            ▲   ▲   ▲   ▲   ▲
            │   │   │   │   │
        trials 1, 2, 3, 4, 5 (all share one stack)
```

### Next: one isolated stack per trial, declared by a manifest

```
   ┌─── trial 1 ────┐  ┌─── trial 2 ────┐  ┌─── trial 3 ────┐
   │ ┌────┐ ┌────┐  │  │ ┌────┐ ┌────┐  │  │ ┌────┐ ┌────┐  │
   │ │runr│ │ db │  │  │ │runr│ │ db │  │  │ │runr│ │ db │  │
   │ └────┘ └────┘  │  │ └────┘ └────┘  │  │ └────┘ └────┘  │
   └────────────────┘  └────────────────┘  └────────────────┘
            ▲                  ▲                  ▲
            │                  │                  │
            └──────── EnvironmentManifest ────────┘
                              │
                              ▼
        ┌─────────────────────────────────────────┐
        │  RuntimeBackend.provision(spec)         │
        │  ─── compiles manifest into substrate ─ │
        │                                         │
        │   • LocalRuntimeBackend → per-trial     │
        │     docker-compose project              │
        │   • DockerRuntime → shared stack        │
        │     (no-op compat path)                 │
        └─────────────────────────────────────────┘
```

The manifest is **the wire format between task declarations and runtime backends.**

## Decision Drivers

- **Borrow, don't reinvent.** The compose convention used by Inspect AI is an established shape with documented semantics; aligning with it saves design effort and gives operators a familiar surface.
- **Substrate-agnostic.** The schema does not bake in compose-only grammar. Today the only consumer is `LocalRuntimeBackend` (docker-compose). Other substrates can be added later without breaking the wire format — but the design does not commit to any specific non-local substrate, and adding one is an independent decision recorded in its own ADR.
- **Determinism precondition.** Images pinned; readiness gate explicit; initial state declared, not inferred.
- **Strict schema.** `extra="forbid"` everywhere so silent field additions cannot drift the wire format.
- **Validation-first.** Land the schema and its tests **before** any runtime backend consumes it, so the contract can iterate against a Pydantic model (cheap) rather than against a deployed backend (expensive).
- **No mandatory migration.** Existing tasks must keep running unchanged. The new field is optional and opt-in.

## Considered Options

1. **Typed Pydantic manifest borrowing Inspect AI's compose convention.** First service = default / runner; others addressed by name; health probes typed by protocol (`tcp` / `http`); `extra="forbid"` round-trip-pinned by a canonical contract test. **This ADR.**
2. **Roll our own schema from scratch.** Maximum freedom, but no precedent to align with; every operator who has used Inspect AI would have to relearn it.
3. **Use docker-compose YAML directly as the manifest.** Zero translation effort today, but locks the wire format to docker-compose grammar — any other substrate would need a parallel format.
4. **Defer until a runtime backend needs it.** The runtime backend cannot land without something to consume; deferring just reorders the same work into a more constrained position.

## Decision

We adopt **Option 1**.

Concretely:

- Add `EnvironmentManifest` and its supporting models (`ServiceSpec`, `HealthProbe`, `PortSpec`, `VolumeMount`, `Resources`, `InitialStateRef`, `DependsOn`) to `tolokaforge/runner/models.py` — alongside `TaskDescription`, which carries the new field. All `extra="forbid"`. The models are re-exported from `tolokaforge/core/trial.py` for callers that work against the trial-spec surface; placing them next to `TaskDescription` avoids a circular import (the manifest is a field on `TaskDescription`).
- `EnvironmentManifest`'s top-level surface: `services: list[ServiceSpec]` (non-empty, first service = default / runner), optional `initial_state: dict[str, InitialStateRef]`, optional `resources: Resources | None` (manifest-level defaults).
- `ServiceSpec` carries a per-service `resources: Resources | None` override — provisioners fall back to the manifest-level defaults when a service declares none.
- `HealthProbe` carries `kind: Literal["tcp", "http"]` plus `port` plus optional `path` (required iff `kind == "http"`), plus `initial_delay_seconds` (startup grace window) alongside `interval_seconds` / `timeout_seconds` / `retries`. No raw `HEALTHCHECK CMD` strings — the typed-by-protocol shape is provisioner-agnostic.
- `Resources` uses **Kubernetes-style quantity strings** — `cpu: "2" | "500m"`, `memory: "4Gi" | "512Mi"`. This is a documented external standard we are borrowing the grammar from (so we don't invent our own parser); it does not commit us to running on Kubernetes.
- `VolumeMount` carries an explicit `kind: Literal["named", "bind"]` (default `"bind"`) so the provisioner does not have to infer named-volume vs path from `source`'s shape.
- `InitialStateRef` carries `kind: Literal["sql", "copy", "script"]` (default `"copy"`) so the provisioner knows how to apply the fixture — SQL pipe, file copy, or script exec — instead of inferring from a filename extension. `from_` is rejected when empty.
- `DependsOn` exists as a structured form for `depends_on` entries: `condition: Literal["service_started", "service_healthy"]`. `ServiceSpec.depends_on: list[str | DependsOn]` — string entries are shorthand for `DependsOn(service=name, condition="service_started")`.
- `ServiceSpec.image` is required and validated against a deny-list of floating tags (`latest`, `main`, `master`, `edge`, `stable`, `dev`, `develop`, `nightly`, `head`, case-insensitive) plus a parser that correctly handles registry-with-port image references (`registry.example.com:5000/foo` without a tag is rejected). `@sha256:` digests are accepted.
- `ServiceSpec.name` is a lowercase DNS label (`^[a-z]([-a-z0-9]*[a-z0-9])?$`) capped at 63 characters (RFC 1123).
- `EnvironmentManifest` carries cross-service validators: `services` non-empty, names unique within the manifest, every `depends_on` reference (in either form) resolves to a service in the same manifest, every `initial_state` key resolves to a service in the same manifest.
- `tolokaforge/runner/models.py:TaskDescription.environment_manifest: EnvironmentManifest | None = None` — net-new optional field. No prior field is being superseded.
- `tests/canonical/test_environment_manifest_contract.py` pins the JSON wire shape (snapshot), the `extra="forbid"` round-trip, and every cross-field validator.
- ADR status stays `Proposed` until a real complex workload exercises the schema end-to-end. The follow-up arc flips it to `Accepted`.

The manifest carries no runtime behaviour in this PR — no provisioner reads it yet. That is intentional: lock the schema with tests before any backend consumer locks it into runtime decisions.

## Impact on existing tasks

**None — by design.**

- **Existing run path is unchanged.** No code path in this PR reads `environment_manifest`. The shared-stack path (`core_stack` / `full_stack` templates) keeps running every task exactly as it does on `main`.
- **No mandatory migration.** `environment_manifest` is `Optional` with default `None`. Tasks that do not declare a manifest continue through the existing shared-stack path.
- **Single-container tasks need nothing.** A task that runs in one container (terminal-bench, native single-service tasks, etc.) is unaffected. If a single-container task ever wants per-trial isolation, it can declare a manifest with one service — but it does not have to.
- **Multi-service tasks (today's `full_stack` consumers) need nothing yet.** They keep using the shared stack. When the runtime backend that consumes the manifest lands (a later PR), each consumer adapter opts in on its own schedule by adding an `environment_manifest` to its `TaskDescription`. There is no flag day, no mass migration, and no breakage.

## Industry-standard grounding

### Inspect AI (UK AI Safety Institute)

[https://inspect.aisi.org.uk/sandboxing.html](https://inspect.aisi.org.uk/sandboxing.html). Inspect's sandbox-provider model decouples the eval harness from where each sample runs. The compose convention used by Inspect's `docker` provider is the schema's primary precedent:

- The first listed service is the **default** that the harness talks to; other services are addressed by name.
- Per-sample isolation: each sample gets its own sandbox instance, even when the manifest is defined at task level.
- The same manifest compiles cleanly under different provider implementations.

Adopting Inspect's compose convention means an operator who has used Inspect can read our manifest immediately, and an Inspect task could in principle be ported by remapping field names — no new mental model.

## Consequences

### Positive

- The boundary between task-side declaration and runtime-side execution is now typed.
- The schema's strict validation (`extra="forbid"`, image pinning, cross-field resolvers) catches malformed manifests at task-load time, not at trial-run time.
- A canonical contract test pins the JSON wire shape: any silent field addition fails CI before it ships.
- The shape is familiar to anyone who has used Inspect AI — no novel mental model.
- Existing tasks are not impacted. The new field is optional and opt-in; the shared-stack path is preserved.

### Negative / Trade-offs

- The manifest carries no runtime behaviour in this PR — the value is purely contract definition until a provisioner consumes it. Acceptable: the alternative (ship schema + provisioner together) doubles the diff and removes the "iterate schema cheaply" property.
- `extra="forbid"` is intentionally strict. A new field needs a coordinated change: model + validator + canonical snapshot. That is the cost of catching silent drift at CI time.
- Inspect-AI-compatible field names (`services`, `image`, `depends_on`) are inherited rather than re-invented; some are more verbose than the shortest possible alternative. We accept the verbosity in exchange for the familiarity.

### Follow-ups

- **`RuntimeBackend` Protocol extension** (the provisioning surface that consumes the manifest). Separate PR.
- **`LocalRuntimeBackend`** — the first concrete consumer.
- **Layered image hierarchy / build cache.** Build-time optimisation, not a schema concern. Surfaces as `ServiceSpec.build` once we have a layered base-image story.
- **Streaming log surface.** Belongs on `RuntimeBackend.stream_logs`, not on the manifest.
- **Flip this ADR's status to `Accepted`** once a complex workload validates the schema end-to-end.

## Rejected alternatives

- **Option 2 — roll our own schema.** No precedent alignment; every operator pays a relearning tax.
- **Option 3 — docker-compose YAML directly.** Locks the wire format to one substrate's grammar; no other consumer could ever read the same document.
- **Option 4 — defer.** Just reorders the work. The runtime backend cannot land without something to consume.

## Scope notes

- **Net-new field.** `TaskDescription.environment_manifest` is brand new — there is no prior `docker_stack_requirements` field on `TaskDescription` to supersede. The shared-stack path (`core_stack` / `full_stack` templates selected by tool declarations) is preserved as the default until a backend honours the new field.
- **Optional field, opt-in adoption.** `environment_manifest: EnvironmentManifest | None = None`. Tasks without a manifest continue to run on the existing shared-stack path. Adapter packs adopt the manifest on their own schedule.
- **No runtime behaviour change.** No code path constructs or consumes the manifest in this PR. The contract test exercises serialization, deserialization, and every validator — that is the scope of "Proposed" status.
- **Health probes are typed-by-protocol, not raw command strings.** A `command: list[str]` field would compile only to one healthcheck flavour. Typed `kind: tcp|http` + `port` + optional `path` is provisioner-agnostic.
- **Initial state references are paths.** `InitialStateRef.from_` is a string path interpreted relative to the task pack root. The provisioner is responsible for resolving the path and applying the fixture; the manifest just names it and declares the kind of application (sql / copy / script).
