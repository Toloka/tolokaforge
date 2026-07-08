# Runtime Architecture Roadmap

Status: living document.

The public roadmap for Tolokaforge's runtime architecture. Long-form
rationale for each decision lives in the [ADRs](adr/). Progress against
open work lives on [GitHub issues](https://github.com/Toloka/tolokaforge/issues)
and the project boards.

## Themes over time

The work is **one ordered sequence** — no parallel tracks. Each release
is independently shippable and builds on the previous one.

- **Boundaries & contracts.** Typed seams between the control plane, the
  trial plane, and the data plane. After this arc every plane has a
  runtime-checkable Protocol with at least two implementations. No
  behaviour change to users.
- **Multi-container environments + per-trial isolation.** Task-declared
  Docker-Compose stacks (via `EnvironmentManifest`) + a `RuntimeBackend`
  Protocol with per-trial materialisation as the first substrate.
- **Complex adapters on the new environment.** Prove the seams on
  workloads more complex than the built-in `native` and `terminal_bench`
  adapters — tasks whose environment needs a database + service +
  frontend as a multi-container manifest.
- **Distributed execution.** Runner reachable over the network with
  mTLS, health, retry; then a control-plane API + state store +
  backend-agnostic scheduler. The control plane stays backend-agnostic
  so adding an at-scale substrate later needs no control-plane change.
- **Runner as a consumable artifact.** The runner packaged as an
  independently-distributable Docker image with a documented, versioned
  gRPC contract. External harnesses can pull the runner, connect their
  own agent, and drive trials without depending on the orchestrator.
- **Open agent loop.** Extends the `Conductor` Protocol with streaming
  event emission as a baseline. Opt-in `ConductorControl` companion
  Protocol adds pause/resume checkpoints and external-message injection
  for interactive or live-observability use cases.
- **Extension-point documentation.** Every entry point in the
  plugin-first architecture (runtime backend, conductor, grader,
  adapter, tool, artifact writer, state store, secret provider,
  observability sink) gets a "how to plug in" guide.

**Later (directional, not committed).** The direction is set but scope
may shift as infrastructure needs evolve: an at-scale runtime backend
(Kubernetes / Agent Sandbox), autoscaling and centralized observability,
shared-cluster operation with quotas and per-team secrets, mid-trial
resume with warm pods, a durable workflow engine if reliability demands
it, and additional runtime backends (microVM, Modal, EC2).

## Release history

| Release   | Architectural scope (what ships)                                                                                                                                                                                                                                  | Status     | ADRs                       |
|-----------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------|----------------------------|
| **0.3.0** | Open `AdapterType` for plugin adapters; `TrialSpec` / `TrialResult` typed control↔trial seam; `TrialArtifactWriter` per-trial data-plane seam; `RunAggregateWriter` run-level data-plane seam; runner `tool_factory` decoupled from adapter layouts; rubric-judge KB search resolved **per-trial on the trial plane** (`TrialContextRuntime`) to mirror the agent's KB tool. | Shipped    | [0003](adr/0003-trial-spec-and-trial-result.md), [0004](adr/0004-trial-artifact-writer-seam.md), [0005](adr/0005-run-aggregate-writer-seam.md) |
| **0.3.1** | Typed `EnvEndpoints` on `TrialSpec`; `RuntimeBackend` Protocol lifting `SharedStackRuntimeBackend` behind a typed seam.                                                                                                                                                          | Shipped    | [0006](adr/0006-typed-env-endpoints.md), [0007](adr/0007-runtime-backend-protocol.md) |
| **0.4.0** | `Conductor` Protocol — per-trial executor seam; `TrialSpec.run_id` decoupled from filesystem; `TrialArtifactWriter` injection symmetry across the run.                                                                                                              | Shipped    | [0008](adr/0008-conductor-protocol.md) |
| **0.5.0** | `EnvironmentManifest` (Docker Compose as source of truth); `RuntimeBackend` provisioning contract; per-trial RPC methods lifted onto `RuntimeBackend`; `RunnerClient` promoted to a Protocol (concrete renamed to `GrpcRunnerClient`); seam-definition and data-declaration conventions codified.                                                                                                                                                    | Shipped    | [0009](adr/0009-environment-manifest.md), [0010](adr/0010-runtime-backend-provisioning-contract.md), [0011](adr/0011-seam-and-declaration-conventions.md), [0013](adr/0013-runtime-backend-per-trial-rpc-methods.md) |
| **0.6.0** | Diff-first default state view for the rubric judge; smaller judge prompts on tasks with large seeded state.                                                                                                                                                                                                                                                                                                                                          | Shipped    | —                          |
| **0.7.0** | `PerTrialRuntimeBackend` (docker-compose-per-trial via Testcontainers) + renamed backend pair (`SharedStackRuntimeBackend` / `PerTrialRuntimeBackend`); `TaskIsolation` on `EnvironmentManifest` with orchestrator-level fail-loud enforcement; `--runtime` CLI flag + loud-defaults banner. First trial-isolation-capable substrate.                                                                                                                | Shipped    | [0009](adr/0009-environment-manifest.md), [0010](adr/0010-runtime-backend-provisioning-contract.md) |
| **0.8.0** | `InProcessConductor.run()` decomposed into named phase methods; `shared_stack_runtime` → `runtime_backend` rename across conductor + orchestrator; new `TrialGrader` Protocol with `RunnerRPCTrialGrader` concrete impl — grading strategy extracted from conductor internals into a swappable seam. Neither Orchestrator nor Conductor grows. Wheel resolver materialises the engine wheel from any PEP 610 install origin instead of scraping a cache. | Shipped    | [0014](adr/0014-trial-grader-protocol.md) |
| **0.9.0** | `TrialExecutor` Protocol + `ProvisioningTrialExecutor` concrete impl — the per-trial substrate lifecycle bracket (`provision → conductor.run → teardown`) as its own component. Orchestrator dispatch swaps `conductor.run` → `trial_executor.execute`. `SharedStackRuntimeBackend.endpoints()` becomes honest via constructor injection. `TerminationReason.PROVISION_ERROR` + failure-attribution classifier. `--runtime per_trial` fully functional. | Shipped    | [0015](adr/0015-trial-executor-protocol.md) |
| 0.10.0    | Validation gate — pinned-version runner-image alias applied after the shared-stack build so task compose files can reference `tolokaforge-runner:<version>`. Migration of `coding_public_example_01` to declare `environment_manifest`; runs end-to-end on `--runtime per_trial` against a real workload. ADRs 0009 / 0010 / 0014 / 0015 flip to `Accepted`. Closes the boundaries-and-contracts arc.                                                                                          | In flight  | [0009](adr/0009-environment-manifest.md), [0010](adr/0010-runtime-backend-provisioning-contract.md), [0014](adr/0014-trial-grader-protocol.md), [0015](adr/0015-trial-executor-protocol.md) |
| 0.11.0    | Multi-container environment model — per-service isolation vocabulary (`ephemeral`/`shared`/`reset` with a fixed reset-primitive enum); typed inputs on `EnvironmentManifest`; content-addressed stack dedup at the `RuntimeBackend` layer so multiple tasks share running stacks by manifest equivalence rather than by explicit declaration (see [`ENVIRONMENT_COMPOSITION.md`](ENVIRONMENT_COMPOSITION.md)); first multi-container complex adapter on the resulting seams.                                                                                                                                                                                                                                                                                                                                | Planned    | tbd                        |
| 0.12.0    | Remote runner: orchestrator and runner on separate hosts; mTLS + health + retry.                                                                                                                                                                                                                                                                                                                                                                    | Planned    | tbd                        |
| 0.13.0    | Middle-ground isolation — mechanisms for sharing selected resources across runs (warm image caches, template DB clones, read-only data volumes, container pools) while keeping trial-mutable state isolated. Design informed by the per-service vocabulary shipped in 0.11.0.                                                                                                                                                                       | Planned    | tbd                        |
| 1.0.0+    | Control plane API + state store; backend-agnostic scheduler.                                                                                                                                                                                                                                                                                                                                                                                        | Planned    | tbd                        |
| tbd       | Runner as an independently-usable component — expose existing Protocols (`RuntimeBackend`, `TrialGrader`, `Conductor`) as entry-point extension groups; slim the runner Docker image so it installs a runner-only subset; ship a `tolokaforge agent` CLI mode with a stable subprocess contract so external harnesses can drive the runtime as an agent. Same package, same wheel; no multi-package split.                                          | Planned    | tbd                        |
| tbd       | Open agent loop — streaming event emission on `Conductor`; opt-in `ConductorControl` Protocol for pause/resume + external-message injection. Composes on top of the runner-as-independent-component work above.                                                                                                                                                                                                                                     | Planned    | tbd (ADR-0017 will land first) |
| tbd       | Extension-point documentation — a "how to plug in" guide per entry point in the plugin-first architecture.                                                                                                                                                                                                                                                                                                                                          | Planned    | —                          |

Versions past the current release are **targets, not commitments** —
scope may shift as each release lands. The ordering is fixed.

**Design investigations currently in flight** — early-stage work that
informs the rows above but hasn't crystallised into ADRs yet:

- **Multi-container isolation model** — per-service `ephemeral`/
  `shared`/`reset` vocabulary + reset primitives (postgres template-DB
  clone, filesystem workspace swap, sqlite truncate). Feeds v0.11.0.
- **Environment composition** — content-addressed stack dedup at the
  `RuntimeBackend` layer + typed inputs on `EnvironmentManifest` +
  the `<pack>/shared/` compose-file convention, so multiple tasks
  share a running stack by manifest equivalence rather than by
  explicit "shared environment" declaration. See
  [`ENVIRONMENT_COMPOSITION.md`](ENVIRONMENT_COMPOSITION.md); worked
  example at
  [`../../examples/native/example-microservices-pack/`](../../examples/native/example-microservices-pack/).
  Feeds v0.11.0.
- **Runner as an independently-usable component** — Protocol
  exposure via entry points + runner image slimming + `AgentAdapter`
  subprocess contract for external harnesses. Feeds the "runner as
  consumable" row above and the open-agent-loop row after it.

## How to follow progress

- **Decisions:** ADRs in [`adr/`](adr/) — each architectural seam lands
  as a Proposed ADR before code, and is marked Accepted once shipped.
- **Open work:** [GitHub issues](https://github.com/Toloka/tolokaforge/issues)
  tagged `architecture` and the project board.
- **Released changes:** [`CHANGELOG.md`](../../CHANGELOG.md) at repo
  root.

## Updating this doc

This file is updated **on release events only** — when a release closes
(move the row's status to `Shipped`, add a new `In flight` row for the
next version) or when scope shifts between releases. PR-level status
does not belong here; it lives on GitHub issues and the project board.
