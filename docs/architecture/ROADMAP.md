# Runtime Architecture Roadmap

Status: living document.

This is the public roadmap for Tolokaforge's runtime architecture evolution.
Long-form rationale for each decision lives in the [ADRs](adr/). Progress
against open work lives on [GitHub issues](https://github.com/Toloka/tolokaforge/issues)
and the project boards.

## Phase ladder

The work is **one ordered sequence** — no parallel tracks. Each phase is
independently shippable and builds on the previous one.

- **Phase 1 — Boundaries & contracts** *(no behaviour change)*
  Typed seams between control plane, trial plane, and data plane. After Phase 1
  each plane has a runtime-checkable Protocol with at least two
  implementations.
- **Phase 2 — First complex adapter on the new contracts** *(single-container)*
  Prove the contracts on a workload more complex than the built-in
  `native` and `terminal_bench` adapters.
- **Phase 3 — Multi-container environments + `local` runtime backend**
  Per-trial environment manifest + provisioner; `RuntimeBackend` interface
  with `local` (docker-compose-per-trial) as the first backend.
- **Phase 4 — Multi-container adapter** *(complex adapter on the new env)*
  Express adapters whose tasks need a database + service + frontend as a
  multi-container manifest, materialised by the runtime backend.
- **Phase 5 — Remote runner** *(orchestrator ≠ runner machine)*
  Runner reachable over the network with mTLS, health, and retry. Directly
  answers the "split orchestrator and runner" pain.
- **Phase 6 — Control plane + backend-agnostic scheduler**
  Control-plane API + state store + scheduler dispatching trials through the
  `RuntimeBackend` interface. The control plane is backend-agnostic, so
  adding an at-scale backend later needs no control-plane change.

**Later (directional, not committed).** Beyond Phase 6 the direction is
set but scope may shift as infrastructure needs evolve: an at-scale runtime
backend (Kubernetes / Agent Sandbox), autoscaling and centralized
observability, shared-cluster operation with quotas and per-team secrets,
mid-trial resume with warm pods, a durable workflow engine if reliability
demands it, and additional runtime backends (microVM, Modal, EC2).

## Release alignment

| Release   | Phases              | Architectural scope (what ships)                                                                                                                                                                                                                                  | Status     | ADRs                       |
|-----------|---------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------|----------------------------|
| **0.3.0** | Phase 1 (seed)      | Open `AdapterType` for plugin adapters; `TrialSpec` / `TrialResult` typed control↔trial seam; `TrialArtifactWriter` per-trial data-plane seam; `RunAggregateWriter` run-level data-plane seam; runner `tool_factory` decoupled from adapter layouts; rubric-judge KB search resolved **per-trial on the trial plane** (`TrialContextRuntime`) to mirror the agent's KB tool. | Shipped    | [0003](adr/0003-trial-spec-and-trial-result.md), [0004](adr/0004-trial-artifact-writer-seam.md), [0005](adr/0005-run-aggregate-writer-seam.md) |
| **0.3.1** | Phase 1 (mid)       | Typed `EnvEndpoints` on `TrialSpec`; `RuntimeBackend` Protocol lifting `SharedStackRuntimeBackend` behind a typed seam.                                                                                                                                                          | Shipped    | [0006](adr/0006-typed-env-endpoints.md), [0007](adr/0007-runtime-backend-protocol.md) |
| **0.4.0** | Phase 1 (close)     | `Conductor` Protocol — per-trial executor seam; `TrialSpec.run_id` decoupled from filesystem; `TrialArtifactWriter` injection symmetry across the run.                                                                                                              | Shipped    | [0008](adr/0008-conductor-protocol.md) |
| **0.5.0** | Phase 3 (schemas + protocols) | `EnvironmentManifest` (Docker Compose as source of truth); `RuntimeBackend` provisioning contract; per-trial RPC methods lifted onto `RuntimeBackend`; `RunnerClient` promoted to a Protocol (concrete renamed to `GrpcRunnerClient`); seam-definition and data-declaration conventions codified.                                                                                                                                                    | Shipped    | [0009](adr/0009-environment-manifest.md), [0010](adr/0010-runtime-backend-provisioning-contract.md), [0011](adr/0011-seam-and-declaration-conventions.md), [0013](adr/0013-runtime-backend-per-trial-rpc-methods.md) |
| **0.6.0** | Phase 3 (grading refinement) | Diff-first default state view for the rubric judge; smaller judge prompts on tasks with large seeded state.                                                                                                                                                                                                                                                                                                                                          | Shipped    | —                          |
| **0.7.0** | Phase 3 (concrete substrate) | `PerTrialRuntimeBackend` (docker-compose-per-trial via Testcontainers) + renamed backend pair (`SharedStackRuntimeBackend` / `PerTrialRuntimeBackend`); `TaskIsolation` on `EnvironmentManifest` with orchestrator-level fail-loud enforcement; `--runtime` CLI flag + loud-defaults banner. First trial-isolation-capable substrate.                                                                                                                | Shipped    | [0009](adr/0009-environment-manifest.md), [0010](adr/0010-runtime-backend-provisioning-contract.md) |
| **0.8.0** | Phase 3 (trial-plane seams) | `InProcessConductor.run()` decomposed into named phase methods; `shared_stack_runtime` → `runtime_backend` rename across conductor + orchestrator; new `TrialGrader` Protocol with `RunnerRPCTrialGrader` concrete impl — grading strategy extracted from conductor internals into a swappable seam. Neither Orchestrator nor Conductor grows.                                                                                                        | Shipped    | [0014](adr/0014-trial-grader-protocol.md) |
| **0.9.0** | Phase 3 (per-trial wiring) | `TrialExecutor` Protocol + `ProvisioningTrialExecutor` concrete impl — the per-trial substrate lifecycle bracket (`provision → conductor.run → teardown`) as its own component. Orchestrator dispatch swaps `conductor.run` → `trial_executor.execute`. `SharedStackRuntimeBackend.endpoints()` becomes honest via constructor injection. `TerminationReason.PROVISION_ERROR` + failure-attribution classifier. `--runtime per_trial` fully functional. | Shipped    | [0015](adr/0015-trial-executor-protocol.md) |
| 0.10.0    | Phase 3 (validation) | Validation gate — pinned-version runner-image alias applied after the shared-stack build so task compose files can reference `tolokaforge-runner:<version>`. Migration of `coding_public_example_01` to declare `environment_manifest`; runs end-to-end on `--runtime per_trial` against a real workload. ADRs 0009 / 0010 / 0014 / 0015 flip to `Accepted`. Phase 3 arc closes.                                                                                          | In flight  | [0009](adr/0009-environment-manifest.md), [0010](adr/0010-runtime-backend-provisioning-contract.md), [0014](adr/0014-trial-grader-protocol.md), [0015](adr/0015-trial-executor-protocol.md) |
| 0.11.0    | Phase 4             | Multi-container complex adapter — db + backend + frontend materialised via `PerTrialRuntimeBackend`.                                                                                                                                                                                                                                                                                                                                                | Planned    | tbd                        |
| 0.12.0    | Phase 5             | Remote runner: orchestrator and runner on separate hosts; mTLS + health + retry.                                                                                                                                                                                                                                                                                                                                                                    | Planned    | tbd                        |
| 1.0.0+    | Phase 6             | Control plane API + state store; backend-agnostic scheduler.                                                                                                                                                                                                                                                                                                                                                                                        | Planned    | tbd                        |

Versions past the current release are **targets, not commitments** — scope
may shift between phases as we land each one. The phase order is fixed.

## How to follow progress

- **Decisions:** ADRs in [`adr/`](adr/) — each architectural seam lands as a
  Proposed ADR before code, and is marked Accepted once shipped.
- **Open work:** [GitHub issues](https://github.com/Toloka/tolokaforge/issues)
  tagged `architecture` and the project board.
- **Released changes:** [`CHANGELOG.md`](../../CHANGELOG.md) at repo root.

## Updating this doc

This file is updated **on release events only** — when a release closes
(move the row's status to `Shipped`, add a new `In flight` row for the next
version) or when scope moves between phases. PR-level status does not
belong here; it lives on GitHub issues and the project board.
