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

- Add `EnvironmentManifest` and its supporting models (`ServiceSpec`, `HealthProbe`, `PortSpec`, `VolumeMount`, `Resources`, `InitialStateRef`, `DependsOn`, `SecurityContext`) to `tolokaforge/runner/models.py` — alongside `TaskDescription`, which carries the new field. All `extra="forbid"`. The models are re-exported from `tolokaforge/core/trial.py` for callers that work against the trial-spec surface; placing them next to `TaskDescription` avoids a circular import (the manifest is a field on `TaskDescription`).
- `EnvironmentManifest`'s top-level surface: `services: list[ServiceSpec]` (non-empty, first service = default / runner), optional `initial_state: dict[str, InitialStateRef]`, optional `resources: Resources | None` (manifest-level defaults).
- `ServiceSpec` carries a per-service `resources: Resources | None` override — provisioners fall back to the manifest-level defaults when a service declares none.
- `HealthProbe` carries `kind: Literal["tcp", "http"]` plus `port` plus optional `path` (required iff `kind == "http"`), plus `initial_delay_seconds` (startup grace window) alongside `interval_seconds` / `timeout_seconds` / `retries`. No raw `HEALTHCHECK CMD` strings — the typed-by-protocol shape is provisioner-agnostic.
- `Resources` uses **Kubernetes-style quantity strings** — `cpu: "2" | "500m"`, `memory: "4Gi" | "512Mi"`. This is a documented external standard we are borrowing the grammar from (so we don't invent our own parser); it does not commit us to running on Kubernetes.
- `VolumeMount` carries an explicit `kind: Literal["named", "bind"]` (default `"bind"`) so the provisioner does not have to infer named-volume vs path from `source`'s shape.
- `InitialStateRef` carries `kind: Literal["sql", "copy", "script"]` (default `"copy"`) so the provisioner knows how to apply the fixture — SQL pipe, file copy, or script exec — instead of inferring from a filename extension. `from_` is rejected when empty.
- `DependsOn` exists as a structured form for `depends_on` entries: `condition: Literal["service_started", "service_healthy"]`. `ServiceSpec.depends_on: list[str | DependsOn]` — string entries are shorthand for `DependsOn(service=name, condition="service_started")`.
- `ServiceSpec.image` is required and validated against a deny-list of floating tags (`latest`, `main`, `master`, `edge`, `stable`, `dev`, `develop`, `nightly`, `head`, case-insensitive) plus a parser that correctly handles registry-with-port image references (`registry.example.com:5000/foo` without a tag is rejected). `@sha256:` digests are accepted.
- `ServiceSpec.name` is a lowercase DNS label (`^[a-z]([-a-z0-9]*[a-z0-9])?$`) capped at 63 characters (RFC 1123).
- **`VolumeMount.source` (when `kind="bind"`) and `InitialStateRef.from_` are validated as safe relative paths** — non-empty, not absolute, with no `..` segments. A manifest cannot bind-mount the host's `/etc` into a trial container; it cannot reference a fixture outside the task pack root. Named-volume sources are unaffected (they are identifiers, not paths).
- **`EnvironmentManifest.network: Literal["isolated", "external"] = "isolated"`** declares the network posture the provisioner is asked to enforce. Default `isolated` means the per-trial network has no outbound path to the public internet or to other trials' projects; `external` is the explicit opt-in for tasks that legitimately need outbound access. The schema declares the posture; provisioners enforce it.
- **`ServiceSpec.security_context: SecurityContext | None`** declares per-container security policy — `run_as_user`, `run_as_group`, `read_only_root_filesystem`, `no_new_privileges`, `capabilities_drop`, `capabilities_add`. Same declare-in-schema / provisioner-enforces-later pattern used for `network`, `read_only`, `resources`. Defaults chosen deliberately: `no_new_privileges=True` and `capabilities_drop=["ALL"]` are the safer starting posture; other fields default `None` / `False` / empty so a service that does not declare a `security_context` sees no behaviour change from the runtime backend's default.
- `EnvironmentManifest` carries cross-service validators: `services` non-empty, names unique within the manifest, every `depends_on` reference (in either form) resolves to a service in the same manifest, every `initial_state` key resolves to a service in the same manifest.
- `tolokaforge/runner/models.py:TaskDescription.environment_manifest: EnvironmentManifest | None = None` — net-new optional field. No prior field is being superseded.
- `tests/canonical/test_environment_manifest_contract.py` pins the JSON wire shape (snapshot), the `extra="forbid"` round-trip, and every cross-field validator.
- ADR status stays `Proposed` until a real complex workload exercises the schema end-to-end. The follow-up arc flips it to `Accepted`.

The manifest carries no runtime behaviour in this PR — no provisioner reads it yet. That is intentional: lock the schema with tests before any backend consumer locks it into runtime decisions.

## Impact on existing tasks

**This PR changes nothing about today's run behaviour.** It adds a schema; no code path reads it. Full canonical + unit suites stay green.

The longer-term picture is worth being explicit about — it shapes how adapter packs plan their adoption.

### Per-trial isolation is the universal architectural goal

The current shared-stack model (one run-wide `ServiceStack`; trials are distinguished only by `trial_id` in URLs) is on the architectural deprecation list — it is a known coupling point regardless of how many services a task uses. Even single-container tasks share the runner across trials today, which leaves room for cross-trial state contamination and constrains how aggressively trials can run in parallel. The direction this arc is moving toward is **one isolated stack per trial for every task**, irrespective of topology size. Multi-service tasks are the forcing function; single-container tasks ride the same machinery.

That direction is delivered by the runtime backend that consumes this manifest, not by this PR.

### What happens to existing tasks across the arc

- **In this PR:** nothing. The shared-stack path runs every task exactly as on `main`.
- **When the per-trial runtime backend lands (later PR):** runtime-backend selection is config-driven. The shared-stack path remains the default; tasks that opt into the per-trial backend run with isolation; tasks that do not opt in keep their existing path.
- **Long term:** the per-trial path is the recommended target. Adapter packs adopt the manifest on their own schedule by adding an `environment_manifest` to their `TaskDescription` — even a one-service manifest is enough to give a single-container task its own isolated container per trial.

### Does a task need a manifest to "comply"?

- **In this PR and the near term:** no. The shared-stack path keeps working; the schema is optional and opt-in. Adapter packs adopt the manifest on their own schedule, not a flag day.
- **To gain per-trial isolation today:** declare a manifest. Even a one-service manifest is enough; the runtime backend that consumes it will give that task its own isolated stack per trial.
- **Longer term:** per-trial isolation may become the required path. Once it has been proven on real workloads and the per-trial backend is the default, the shared-stack path is a candidate for deprecation. **That decision is not made by this ADR.** A future ADR — with its own deprecation window, migration guide, and communication ahead of any breaking change — will decide whether and when "running with a manifest" becomes mandatory. Adapter packs treating manifest adoption as eventual rather than optional is the safer planning posture.

Today there is no mandatory migration and no engine-side breakage. The schema's design intentionally keeps the door open to making it required later, so anyone planning adapter-pack work knows that "adopt the manifest" is the direction of travel, not a permanent opt-in.

## Safety boundaries

The manifest is the typed declaration of the **boundary an agent's trial runs inside**. Three properties are worth stating explicitly so reviewers and adapter authors share the same mental model.

**Grading runs outside the trial container.** Every grader the engine ships (rubric judge, state-hash, assertions, transcript rules) executes in the runner / orchestrator process, never inside the trial's services. The agent loop inside the trial cannot reach the grader's code, the tests it evaluates, or the report it produces. The manifest does not change this — the grader is structurally beyond an agent's reach by where it runs, not by what the schema declares.

**The schema declares; the provisioner enforces.** `read_only`, `network`, and `resources` are properties the schema documents; the runtime backend that consumes the manifest is responsible for honouring them. The schema's job is to make the intended posture explicit and auditable; the provisioner's job is to materialise that posture in containers, networks, and policies. A manifest that declares `read_only: true` on a fixture mount is still only safe if the provisioner refuses to ignore it — that's why these are named follow-ups for the provisioner ADR, not just task-side declarations.

**Per-trial isolation bounds an agent's blast radius.** Today's shared-stack path partitions trials only by `trial_id` in URLs; per-trial isolation puts each trial in its own compose project with its own network and its own ephemeral volumes. The manifest's job is to make that boundary declarative and consistent across runtime backends so the same task runs with the same posture whether the provisioner is local docker-compose or a future substrate.

The schema-side guards in this ADR (path-traversal validation on bind sources and initial-state references, network-mode default of `isolated`, image-pinning, `extra="forbid"`) reduce the surface area through which a malformed or hostile manifest could weaken those boundaries. They do not replace the provisioner-side enforcement that lands later — but they catch the cheap failure modes (typos, careless authoring, drift) at task-load time instead of at trial-run time.

## Industry precedents studied

The schema is the result of an explicit review of how other agent-evaluation harnesses declare their environments. Three projects shaped the choices here; one is the primary precedent we copied from, one is a future-integration option we deliberately left room for, and one informed a single targeted choice (image pinning).

### Inspect AI (UK AI Safety Institute) — primary precedent

[inspect.aisi.org.uk/sandboxing](https://inspect.aisi.org.uk/sandboxing.html). The UK AISI's open evaluation framework is the clearest existing example of the two pillars this ADR formalises:

- **Sandbox-provider abstraction.** Inspect ships `docker` and `local` built in, plus `k8s`, Daytona, Modal, EC2, and Proxmox as separate provider packages. The eval runs unchanged across them — substrate is a swap, not a rewrite. This is the same shape our `RuntimeBackend` Protocol (ADR-0007) commits to.
- **Per-sample isolation.** "Each sample gets its own sandbox instance, even if the sandbox is defined at task level" — validates per-trial isolation as the right unit.
- **Compose-based environment convention.** Inspect's `docker` provider reads a `compose.yaml` where the **first listed service is the default** (the one the harness talks to), **others are addressed by name**, and shared volumes wire inter-container comms.

What we adopted directly: the compose convention (first-service-default, address-by-name), per-sample isolation as the model, and the protocol typing of health probes (`tcp` / `http`) instead of raw command strings. An operator who has used Inspect can read our manifest without learning anything new; an Inspect task could in principle be ported by renaming fields.

### Kubernetes Agent Sandbox — studied as a future integration

[agent-sandbox.sigs.k8s.io](https://agent-sandbox.sigs.k8s.io). A formal Kubernetes SIG-Apps subproject (launched late 2025) that standardises agent-execution primitives on Kubernetes. We studied it as the candidate path for any future at-scale backend; we did **not** commit to it, but we made schema choices that keep the integration cheap if/when it happens.

What Agent Sandbox offers:

- **A `Sandbox` CRD** — a declarative, controller-managed pod with stable identity and persistent storage. Replaces the hand-rolled "StatefulSet of size 1 + headless Service + PVC" pattern.
- **Warm pools** (`SandboxWarmPool` + `SandboxTemplate` + `SandboxClaim`) — pre-provisioned isolated pods claimed on demand, with reported sub-200 ms allocation latency.
- **Pluggable isolation** via Kubernetes's standard `runtimeClassName` — **gVisor** (userspace kernel) or **Kata** (per-pod micro-VM) for kernel/network isolation of untrusted agent + task code on shared infrastructure.

Why this ADR does not commit to it:

- **Pre-1.0.** The project is still maturing (v0.4.x as of writing); APIs may change; warm-pool design is still under upstream discussion. Adopting now means tracking a moving target.
- **Multi-container gap.** Core Sandbox models a **single container per sandbox**; a realistic trial often needs N services (db + backend + frontend + …). Multi-container topology is an open extension point — not free, would need composition work on our side.
- **Local must always work.** The `local` runtime backend has to work without a cluster; committing to a specific at-scale substrate is reversible only if it stays optional.

What we did instead — cheap insurance:

- `Resources.cpu` / `memory` use Kubernetes-style quantity strings (`"500m"`, `"4Gi"`). Borrowed grammar; works for compose `deploy.resources.limits` today; would drop straight into pod `resources.requests` if a K8s integration ever lands.
- `PortSpec` declares only the container port; the runtime backend assigns host-side mapping. Sandbox CRs have no host ports either — services are reached by name on the pod's network namespace. Same shape works both ways.
- Health probes are typed by protocol (`tcp` / `http`), the same shape Kubernetes readiness/liveness probes use.

Names that would need to land in a separate ADR if we ever pursue this path: `runtimeClassName`, `securityContext`, `NetworkPolicy`. They are out of scope here.

### SWE-bench — targeted influence on image pinning

[swebench.com](https://www.swebench.com/). SWE-bench's instance-image discipline (every instance image pinned to an immutable tag or digest, hierarchy of base → environment → instance images cached aggressively) is the precedent for the strict image-pinning rule on `ServiceSpec.image`. We borrowed the rule; the layered-cache pattern is a named follow-up (see below), not in this ADR.

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

#### Architectural-consistency follow-ups

Not specific to this manifest, but surfaced by the pattern-audit this ADR triggered — filed so the discipline they codify applies uniformly to future components:

- **[Architectural conventions ADR](https://github.com/Toloka/tolokaforge/issues/130).** Codify the two patterns Phase 1 established — seam-definition (Protocol + ≥2 impls + `InMemory*` fixture + contract test) and data-declaration (Pydantic + `extra="forbid"` + snapshot test) — so new components adopt the same shape rather than each contributor picking their own.
- **[Judge Protocol lift](https://github.com/Toloka/tolokaforge/issues/131).** The rubric judge is a one-implementation top-level function today; lifting it to a `Judge` Protocol (with `LLMJudge` + `InMemoryJudge`) matches the seam-definition pattern used by `RuntimeBackend`, `Conductor`, and the artifact writers. Unlocks deterministic replay, cross-check ensemble, and adversarial-content variants without ad-hoc forks. Coordination with the Judge maintainer; sequenced after their active follow-up PRs.

#### Deferred safety follow-ups

Named here so a future reader does not have to re-derive what was considered and consciously deferred from this ADR:

- **Per-trial secret scoping.** The current `SecretManager` is a runner-wide singleton bootstrapped from a single environment variable; every trial in a run sees the same secret pool. Per-trial secret scoping is a control-plane concern (which trial sees which credentials), not a manifest concern. Separate ADR when secret-leakage isolation becomes a hard requirement.
- **Grader / agent boundary inside the trial container.** Today's grader runs in the runner / orchestrator process — outside any trial container, with its own isolated `ToolRegistry` the agent has no path to reach. The schema therefore does not need to carve out a separate grading service. If a future runtime backend ever runs the grader beside the agent (e.g. inheriting a benchmark harness convention), the manifest will need an optional `grading_service` field with stricter mounts and a read-only artifact path the agent cannot write to. Not in scope today. Complementary architectural work — lifting the judge itself to a Protocol — is tracked separately (see [Judge Protocol lift](https://github.com/Toloka/tolokaforge/issues/131) in the architectural-consistency follow-ups above).
- **Trusted-output declaration / instruction-hierarchy hardening.** When the engine evaluates agents against adversarial-content scenarios (manifest-declared services that emit fake CI logs, fake issue comments, etc.), the manifest may need a way to mark which service outputs the agent should treat as authoritative vs untrusted. Out of scope until such evaluations exist in the engine.

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
